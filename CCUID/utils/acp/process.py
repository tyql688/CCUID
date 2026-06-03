from __future__ import annotations

import os
import sys
import shutil
import signal
import asyncio
import contextlib
from typing import Protocol, cast
from pathlib import Path
from collections import deque
from dataclasses import dataclass

import psutil

from gsuid_core.logger import logger

from .orphans import (
    record_spawn,
    taskkill_tree,
    kill_proc_tree,
    record_teardown,
    descendant_procs,
)
from ..engines import EngineSpec

STREAM_LIMIT_BYTES = 50 * 1024 * 1024
TERMINATE_TIMEOUT_SEC = 3
STDERR_TAIL_LINES = 50
CONNECTION_CLOSE_TIMEOUT_SEC = 5
PROXY_URL_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")


class ClosableTransport(Protocol):
    def close(self) -> None: ...


class ProcessTransportOwner(Protocol):
    _transport: ClosableTransport | None


class AsyncClosable(Protocol):
    async def close(self) -> None: ...


@dataclass(slots=True, frozen=True)
class SpawnedProcess:
    proc: asyncio.subprocess.Process
    stderr_task: asyncio.Task[None]


def format_tail(tail: deque[str]) -> str:
    if not tail:
        return ""
    return "\nstderr tail:\n" + "\n".join(tail)


def resolve_launcher(cmd: tuple[str, ...]) -> tuple[str, ...]:
    if not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return cmd
    return (resolved, *cmd[1:])


def _ensure_workdir(workdir: str) -> None:
    path = Path(workdir)
    if path.is_symlink():
        raise RuntimeError(f"workdir 是符号链接，拒绝启动: {workdir}")
    if path.exists():
        if not path.is_dir():
            raise RuntimeError(f"workdir 不是目录，拒绝启动: {workdir}")
        return
    path.mkdir(parents=True, exist_ok=True)


def _agent_uses_proxy(engine_name: str) -> bool:
    from ...cc_config.cc_config import CCUIDConfig

    agents = CCUIDConfig.get_config("AgentProxyAgents").data
    enabled = {agent.strip().lower() for agent in agents if agent.strip()}
    return "all" in enabled or engine_name in enabled


def _apply_agent_proxy_env(env: dict[str, str], engine_name: str) -> None:
    from ...cc_config.cc_config import CCUIDConfig

    if not CCUIDConfig.get_config("AgentProxyMode").data:
        return
    if not _agent_uses_proxy(engine_name):
        return

    proxy_url = CCUIDConfig.get_config("AgentProxyUrl").data.strip()
    if proxy_url == "":
        return
    for key in PROXY_URL_ENV_KEYS:
        env[key] = proxy_url

    no_proxy = CCUIDConfig.get_config("AgentNoProxy").data.strip()
    if no_proxy == "":
        return
    for key in NO_PROXY_ENV_KEYS:
        env[key] = no_proxy


def build_spawn_env(engine: EngineSpec) -> dict[str, str]:
    env = dict(os.environ)
    _apply_agent_proxy_env(env, engine.name)
    if engine.name == "claude" and "CLAUDE_CODE_EXECUTABLE" not in env:
        system_claude = shutil.which("claude")
        if system_claude:
            env["CLAUDE_CODE_EXECUTABLE"] = system_claude
            logger.debug(f"[CCUID/{engine.name}] CLAUDE_CODE_EXECUTABLE={system_claude}")
    return env


async def close_stdin(proc: asyncio.subprocess.Process) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    with contextlib.suppress(Exception):
        stdin.close()
        await asyncio.wait_for(stdin.wait_closed(), timeout=CONNECTION_CLOSE_TIMEOUT_SEC)


def _alive_survivors(descendants: list[psutil.Process]) -> list[psutil.Process]:
    # killpg 之后仍活着的：setsid 逃出组的，或 killpg 报 EPERM 没收掉的同组成员——逐个 per-pid 兜底。
    # 僵尸（已死待回收）排除，免得 wait_procs 空等满超时。
    out: list[psutil.Process] = []
    for proc in descendants:
        with contextlib.suppress(Exception):
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                out.append(proc)
    return out


async def _terminate_process_posix(proc: asyncio.subprocess.Process, *, engine_name: str) -> None:
    # 趁 leader 活着抓子树：killpg 不可用(pgid 落在 gscore 组)时整体兜底，或给 setsid 逃逸者补刀。
    descendants = descendant_procs(proc.pid)
    pgid = proc.pid  # session leader 的 pgid 恒等于自身 pid（已实测）
    # 灾难护栏：start_new_session 没生效时 pgid 可能 == gscore 自身组，killpg 会连 gscore 一起杀。
    if pgid <= 0 or pgid == os.getpgid(0):
        logger.error(f"[CCUID/{engine_name}] pgid={pgid} 落在 gscore 组，改用 psutil 兜底（不 killpg）")
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
        if descendants:
            await asyncio.to_thread(kill_proc_tree, descendants)
        return
    if proc.returncode is None:
        with contextlib.suppress(OSError):  # ESRCH(组空) / EPERM(僵尸成员) 都吞，不让它崩 teardown
            os.killpg(pgid, signal.SIGTERM)
        # 轮询整组退出，给（除忽略 SIGTERM 外的）成员优雅窗口；组空即提前结束。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TERMINATE_TIMEOUT_SEC
        while loop.time() < deadline:
            try:
                os.killpg(pgid, 0)  # 组里还有活着的成员？
            except OSError:
                break  # ESRCH/EPERM：停止轮询，直接进 SIGKILL + psutil 兜底
            await asyncio.sleep(0.1)
    # 先 reap leader：别留僵尸 leader（macOS 对僵尸 leader 所在组 killpg 会 EPERM）。
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
    # 整组补 SIGKILL（忽略 SIGTERM / 比 leader 活得久的同组成员）：无条件打，修「leader 优雅退出但子进程残留」。
    # ESRCH(组空) / EPERM 都吞——收不掉的交给下面 psutil per-pid 兜底，绝不让 killpg 崩 teardown。
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    survivors = _alive_survivors(descendants)
    if survivors:
        reaped = await asyncio.to_thread(kill_proc_tree, survivors)
        if reaped:
            logger.debug(f"[CCUID/{engine_name}] 收尾 {reaped} 个残留子树进程")


async def terminate_process(proc: asyncio.subprocess.Process, *, engine_name: str) -> None:
    # POSIX：killpg 整组收（内核原子，覆盖 teardown 后才 fork 的子进程）。
    if sys.platform != "win32":
        await _terminate_process_posix(proc, engine_name=engine_name)
        return
    # Windows：没有 kill 级联、stdin-EOF 又穿不过 cmd.exe shim，只 terminate launcher 会把
    # cmd.exe→node→claude→MCP 整棵留成孤儿（实测每次 teardown 漏一棵）。趁树还活着 taskkill /F /T
    # 按 ppid 重新枚举 live 树整棵收割（捕获 psutil 快照之后才生的子进程），再走 psutil 快照兜底。
    descendants = descendant_procs(proc.pid)
    reaped_tree = False
    if proc.returncode is None:
        reaped_tree = (await taskkill_tree(proc.pid)) in (0, 128)
    if proc.returncode is None and not reaped_tree:
        # taskkill 不在 PATH / 失败：回退到只 terminate launcher，子树靠下面 psutil 快照兜底。
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    if proc.returncode is None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
    if descendants:
        reaped = await asyncio.to_thread(kill_proc_tree, descendants)
        if reaped:
            logger.debug(f"[CCUID/{engine_name}] 收尾 {reaped} 个 ACP 子树进程")


async def close_process_transport(proc: asyncio.subprocess.Process) -> None:
    try:
        transport = cast(ProcessTransportOwner, cast(object, proc))._transport
    except AttributeError:
        return
    if transport is None:
        return
    with contextlib.suppress(Exception):
        transport.close()
    await asyncio.sleep(0)


def stdio(
    proc: asyncio.subprocess.Process,
    *,
    engine_name: str,
) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError(f"{engine_name} 子进程 stdio 不可用")
    return proc.stdin, proc.stdout


async def pump_stderr(proc: asyncio.subprocess.Process, tail: deque[str], *, engine_name: str) -> None:
    if proc.stderr is None:
        return
    with contextlib.suppress(Exception):
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                tail.append(line)
                logger.debug(f"[CCUID/{engine_name}] {line}")


async def spawn_process(
    engine: EngineSpec,
    workdir: str,
    stderr_tail: deque[str],
    *,
    log_prefix: str = "",
) -> SpawnedProcess:
    _ensure_workdir(workdir)
    cmd = resolve_launcher(engine.cmd)
    prefix = f"{log_prefix} " if log_prefix else ""
    logger.debug(f"[CCUID/{engine.name}] {prefix}{' '.join(cmd)} cwd={workdir}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        limit=STREAM_LIMIT_BYTES,
        env=build_spawn_env(engine),
        # POSIX: setsid ⇒ launcher 成会话/进程组 leader（pgid==proc.pid，已实测），teardown 用
        # killpg 整组收（内核原子，覆盖 teardown 开始后才 fork 的子进程）。该参数在 Windows 上
        # 是 POSIX-only no-op（等同默认 False），spawn 行为不变。
        start_new_session=sys.platform != "win32",
    )
    # session leader 的 pgid 恒等于自身 pid，无需 getpgid 系统调用（也避开 leader 瞬退的 race）。
    record_spawn(proc.pid, engine.name, pgid=(None if sys.platform == "win32" else proc.pid))
    stderr_task = asyncio.create_task(
        pump_stderr(proc, stderr_tail, engine_name=engine.name),
        name=f"CCUID-stderr-{engine.name}-{proc.pid}",
    )
    return SpawnedProcess(proc=proc, stderr_task=stderr_task)


async def cleanup_unregistered_process(
    *,
    conn: AsyncClosable | None,
    proc: asyncio.subprocess.Process | None,
    stderr_task: asyncio.Task[None] | None,
    engine_name: str,
) -> None:
    if conn is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.close(), timeout=CONNECTION_CLOSE_TIMEOUT_SEC)
    if proc is not None:
        await close_stdin(proc)
        await terminate_process(proc, engine_name=engine_name)
        record_teardown(proc.pid)
    if stderr_task is not None:
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(BaseException):
            await stderr_task
    if proc is not None:
        await close_process_transport(proc)
