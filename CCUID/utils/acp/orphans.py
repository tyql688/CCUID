from __future__ import annotations

import os
import sys
import json
import shutil
import signal
import asyncio
import contextlib
import subprocess
from dataclasses import dataclass

import psutil

from gsuid_core.logger import logger

from ..resource.RESOURCE_PATH import WORKDIR_ROOT

# ~/.ccuid/spawned_pids.json —— 跟 session workdir 同根，方便观察。
# 新格式：{ "<pid>": {"hint": "<cmdline 期望子串>", "pgid": <int|null>} }
# 兼容旧格式：{ "<pid>": "<hint>" }（升级首启不丢旧孤儿）。
# pgid 仅 POSIX 记录：leader 死后子进程仍共享其 pgid，reap 时可 killpg 整组兜回。
_PID_FILE = WORKDIR_ROOT / "spawned_pids.json"
_TERMINATE_GRACE_SEC = 3.0


@dataclass(frozen=True, slots=True)
class _Entry:
    hint: str
    pgid: int | None


def _load() -> dict[str, _Entry]:
    if not _PID_FILE.exists():
        return {}
    try:
        raw = _PID_FILE.read_text()
    except OSError:
        logger.warning(f"[CCUID] read orphan pid file failed: {_PID_FILE}")
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[CCUID] orphan pid file is invalid json, ignored: {_PID_FILE}")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, _Entry] = {}
    for pid, val in data.items():
        if not isinstance(pid, str):
            continue
        if isinstance(val, str):  # 旧格式：纯 hint 字符串
            out[pid] = _Entry(hint=val, pgid=None)
        elif isinstance(val, dict):
            hint = val.get("hint")
            pgid = val.get("pgid")
            if isinstance(hint, str) and (pgid is None or isinstance(pgid, int)):
                out[pid] = _Entry(hint=hint, pgid=pgid)
    return out


def _save(data: dict[str, _Entry]) -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PID_FILE.with_suffix(f"{_PID_FILE.suffix}.tmp")
    payload = {pid: {"hint": e.hint, "pgid": e.pgid} for pid, e in data.items()}
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, _PID_FILE)


def descendant_procs(pid: int) -> list[psutil.Process]:
    """趁父进程还活着抓它的整棵子孙进程。父进程死后子树会被 reparent 抓不到——
    所以必须在 terminate 前抓。POSIX 用作 killpg 之外 setsid-escapee 的补刀；
    Windows 用作 taskkill 的兜底。"""
    try:
        return psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def kill_proc_tree(procs: list[psutil.Process]) -> int:
    """terminate 再 kill 一批进程，返回收掉的数量。"""
    if not procs:
        return 0
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=_TERMINATE_GRACE_SEC)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    return len(procs)


async def taskkill_tree(pid: int) -> int:
    """Windows: `taskkill /F /T` 按 ppid 重新枚举 live 树整棵收割（捕获 psutil 快照之后才
    生的子进程）。必须趁树还活着调。POSIX 直接返回 -1（no-op）。
    rc: 0=成功；128=进程已不在（等同成功）。"""
    if sys.platform != "win32":
        return -1
    exe = shutil.which("taskkill")
    if exe is None:
        return -1
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_TERMINATE_GRACE_SEC)
        rc = proc.returncode if proc.returncode is not None else -1
        if rc not in (0, 128):
            logger.debug(f"[CCUID] taskkill /T pid={pid} rc={rc} {err!r}")
        return rc
    except (TimeoutError, OSError) as e:
        logger.warning(f"[CCUID] taskkill /T pid={pid} failed: {e!r}")
        return -1


def record_spawn(pid: int, cmd_hint: str, *, pgid: int | None = None) -> None:
    """spawn ACP 子进程后立即调；cmd_hint 是 cmdline 期望包含的子串（reap 防 PID 复用误杀）；
    pgid 仅 POSIX 传（= leader pid）。"""
    data = _load()
    data[str(pid)] = _Entry(hint=cmd_hint, pgid=pgid)
    _save(data)


def record_teardown(pid: int) -> None:
    """正常 teardown 后调，从文件移除该 PID。"""
    data = _load()
    if data.pop(str(pid), None) is not None:
        _save(data)


def _group_has_member(pgid: int, hint: str) -> bool:
    """组里还有 cmdline 含 hint 的活进程 → 确认是我们的孤儿组（leader 已死时防 pgid 复用误杀）。"""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            if os.getpgid(proc.info["pid"]) == pgid and hint in " ".join(proc.info["cmdline"] or []):
                return True
    return False


def reap_orphans() -> int:
    """gscore 启动时调一次。把上次 gscore 被强杀 / crash 留下、cmdline 仍匹配的 ACP
    孤儿整棵 kill 掉。返回处理的数量。"""
    data = _load()
    if not data:
        return 0
    posix = sys.platform != "win32"
    own_pgid = os.getpgid(0) if posix else -1
    killed = 0
    for pid_str, entry in data.items():
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        proc = _try_get_proc(pid, entry.hint)  # leader 还活着且确是我们的？
        handled = False
        # POSIX 整组 killpg：leader 死活都试——只要组里还有我们的成员。覆盖「leader 已死、
        # 子进程仍共享其 pgid」（旧版要求 leader 存活，直接漏掉这种孤儿）。_group_has_member 防 pgid 复用误杀。
        if posix and entry.pgid is not None and entry.pgid > 0 and entry.pgid != own_pgid:
            if proc is not None or _group_has_member(entry.pgid, entry.hint):
                with contextlib.suppress(OSError):  # ESRCH/EPERM 都吞，余下交给 psutil 兜底
                    os.killpg(entry.pgid, signal.SIGKILL)
                handled = True
        if proc is not None:
            descendants = descendant_procs(pid)
            if not posix:
                # Windows：taskkill /F /T 整树（按 ppid 走，需活着的 launcher pid）。
                taskkill = shutil.which("taskkill")
                if taskkill is not None:
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            [taskkill, "/F", "/T", "/PID", str(pid)],
                            timeout=_TERMINATE_GRACE_SEC,
                            capture_output=True,
                        )
            try:
                proc.terminate()
                proc.wait(timeout=_TERMINATE_GRACE_SEC)
            except psutil.TimeoutExpired:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            kill_proc_tree(descendants)
            handled = True
        if handled:
            killed += 1
            logger.debug(f"[CCUID] reap 孤儿 ACP pid={pid} pgid={entry.pgid} hint={entry.hint!r}")
    _save({})
    return killed


def _try_get_proc(pid: int, hint: str) -> psutil.Process | None:
    """PID 还在跑 + cmdline 含期望子串 → 返回 Process，否则 None。"""
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return proc if hint in cmdline else None
