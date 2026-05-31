from __future__ import annotations

import os
import shutil
import asyncio
import contextlib
from typing import Protocol, cast
from pathlib import Path
from collections import deque
from dataclasses import dataclass

from gsuid_core.logger import logger

from .orphans import record_spawn, record_teardown
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


async def terminate_process(proc: asyncio.subprocess.Process, *, engine_name: str) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
    except TimeoutError:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_TIMEOUT_SEC)
    except ProcessLookupError:
        return
    except Exception as err:
        logger.warning(f"[CCUID/{engine_name}] terminate failed pid={proc.pid}: {err!r}")


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
    )
    record_spawn(proc.pid, engine.name)
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
