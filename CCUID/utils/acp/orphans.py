from __future__ import annotations

import json

import psutil

from gsuid_core.logger import logger

from ..resource.RESOURCE_PATH import WORKDIR_ROOT

# ~/.ccuid/spawned_pids.json —— 跟 session workdir 同根，方便观察。
# 内容：{ "<pid>": "<cmdline 期望子串，用来防 PID 复用误杀>" }
_PID_FILE = WORKDIR_ROOT / "spawned_pids.json"
_TERMINATE_GRACE_SEC = 3.0


def _load() -> dict[str, str]:
    if not _PID_FILE.exists():
        return {}
    raw = _PID_FILE.read_text()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, str]) -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(json.dumps(data))


def record_spawn(pid: int, cmd_hint: str) -> None:
    """spawn ACP 子进程后立即调；cmd_hint 是 cmdline 期望包含的子串，
    启动时 reap 校验用，防 PID 复用误杀别的程序。"""
    data = _load()
    data[str(pid)] = cmd_hint
    _save(data)


def record_teardown(pid: int) -> None:
    """正常 teardown 后调，从文件移除该 PID。"""
    data = _load()
    if data.pop(str(pid), None) is not None:
        _save(data)


def reap_orphans() -> int:
    """gscore 启动时调一次。把上次 gscore 被强杀 / crash 留下、cmdline 仍匹配
    的 ACP 孤儿全部 kill 掉。返回 kill 成功的数量。"""
    data = _load()
    if not data:
        return 0
    killed = 0
    for pid_str, hint in data.items():
        pid = int(pid_str)
        proc = _try_get_proc(pid, hint)
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=_TERMINATE_GRACE_SEC)
        except psutil.TimeoutExpired:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        killed += 1
        logger.info(f"[CCUID] reap 孤儿 ACP 子进程 pid={pid} hint={hint!r}")
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
