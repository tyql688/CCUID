from __future__ import annotations

import os
import time
import shutil
import asyncio
import contextlib
from typing import Any, Protocol, cast
from collections import deque
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from acp import (
    PROTOCOL_VERSION,
    connect_to_agent,
)
from acp.schema import (
    Usage,
    SessionInfo,
    UsageUpdate,
    Implementation,
    PromptResponse,
    AvailableCommand,
    AgentCapabilities,
    CurrentModeUpdate,
    ImageContentBlock,
    SessionInfoUpdate,
    ClientCapabilities,
    ConfigOptionUpdate,
    NewSessionResponse,
    LoadSessionResponse,
    ResumeSessionResponse,
    AvailableCommandsUpdate,
    SessionConfigOptionSelect,
    SessionConfigOptionBoolean,
    SetSessionConfigOptionResponse,
)
from gsuid_core.logger import logger

from .state import _extract_modes, _extract_models, _extract_mode_config, _extract_model_config
from .client import ACPClient, PermissionApprovalStore
from .orphans import record_spawn, record_teardown
from ..engines import EngineSpec
from ...version import VERSION
from ..database import CCUIDSessionModel

_LIMIT = 50 * 1024 * 1024
_TERMINATE_TIMEOUT = 3
_STDERR_TAIL_LINES = 50
_STDERR_LINE_MAX_CHARS = 2000
_TRAILING_UPDATE_DRAIN_TIMEOUT = 0.05
_RPC_MUTATION_TIMEOUT = 30
_CONNECTION_CLOSE_TIMEOUT = 5
_SPAWN_FAIL_THRESHOLD = 3
_SPAWN_COOLDOWN_SEC = 60
_PROXY_URL_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
# ACP 握手 (initialize + new/load_session) 总超时：子进程 stdin/stdout 卡死时不让 prompt 永久挂，也兜 npx 冷启动。
_HANDSHAKE_TIMEOUT_SEC = 60
_SessionStateResponse = NewSessionResponse | LoadSessionResponse | ResumeSessionResponse
_ResponseState = _SessionStateResponse | SetSessionConfigOptionResponse | ConfigOptionUpdate


class _ClosableTransport(Protocol):
    def close(self) -> None: ...


class _ProcessTransportOwner(Protocol):
    _transport: _ClosableTransport | None


class BackendError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class PromptUsage:
    """ACP 累积 usage 快照。任一字段 None 表示 agent 没给（各 provider 上报能力不同）。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_read_tokens: int | None = None
    cached_write_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None
    ctx_used: int | None = None
    ctx_size: int | None = None
    cost_amount: float | None = None
    cost_currency: str | None = None

    @property
    def has_any_data(self) -> bool:
        return any(
            v is not None
            for v in (
                self.input_tokens,
                self.output_tokens,
                self.cached_read_tokens,
                self.cached_write_tokens,
                self.thought_tokens,
                self.total_tokens,
                self.ctx_used,
                self.ctx_size,
                self.cost_amount,
                self.cost_currency,
            )
        )


@dataclass(slots=True)
class ACPSession:
    proc: asyncio.subprocess.Process
    conn: Any
    acp_sid: str
    workdir: str
    queue: asyncio.Queue[Any]
    stderr_task: asyncio.Task[None]
    watch_task: asyncio.Task[None]
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    agent_capabilities: AgentCapabilities | None = None
    # new/load_session 响应里的当前模型；None = agent 没声明 models（老 adapter），渲染层据此决定是否展示。
    model_id: str | None = None
    model_name: str | None = None
    # new/load_session 给的整张模型目录 (model_id, name)；`cc 模型` 用，session 期间稳定不重拉。
    available_models: tuple[tuple[str, str], ...] = ()
    config_options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...] = ()
    current_mode_id: str | None = None
    available_modes: tuple[tuple[str, str, str | None], ...] = ()
    available_commands: tuple[AvailableCommand, ...] = ()
    session_title: str | None = None
    session_updated_at: str | None = None
    # backend.prompt 流式 sniff；Usage 是 cumulative，跨 prompt 直接覆盖
    last_usage_update: UsageUpdate | None = None
    last_prompt_usage: Usage | None = None
    # per-prompt 推理耗时（_run 起跑 → PromptResponse），含权限审批等待
    last_prompt_elapsed: float | None = None
    rpc_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def format_tail(tail: deque[str]) -> str:
    if not tail:
        return ""
    return "\nstderr tail:\n" + "\n".join(tail)


def _cap_stderr_line(line: str) -> str:
    if len(line) <= _STDERR_LINE_MAX_CHARS:
        return line
    return line[: _STDERR_LINE_MAX_CHARS - 1] + "…"


def _resolve_launcher(cmd: tuple[str, ...]) -> tuple[str, ...]:
    """cmd[0] 解析成绝对路径再交给 subprocess：Windows 的 CreateProcessW 不走 PATHEXT，
    传 `npx` 会 WinError 2（实际是 `npx.cmd`）；shutil.which 懂 PATHEXT，全平台靠它兜底。"""
    if not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return cmd  # 留给 spawn 自己抛，错误信息会包含原始 cmd[0]
    return (resolved, *cmd[1:])


def _same_workdir(a: str, b: str) -> bool:
    return os.path.abspath(os.path.expanduser(a)) == os.path.abspath(os.path.expanduser(b))


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

    for key in _PROXY_URL_ENV_KEYS:
        env[key] = proxy_url

    no_proxy = CCUIDConfig.get_config("AgentNoProxy").data.strip()
    if no_proxy == "":
        return
    for key in _NO_PROXY_ENV_KEYS:
        env[key] = no_proxy


def _build_spawn_env(engine: EngineSpec) -> dict[str, str]:
    """Claude wrapper 默认 spawn 自带的旧版 cli.js（模型列表跟终端不一致）；本地有 `claude`
    binary 时用 CLAUDE_CODE_EXECUTABLE 指过去，走终端那一份。"""
    env = dict(os.environ)
    _apply_agent_proxy_env(env, engine.name)
    if engine.name == "claude" and "CLAUDE_CODE_EXECUTABLE" not in env:
        system_claude = shutil.which("claude")
        if system_claude:
            env["CLAUDE_CODE_EXECUTABLE"] = system_claude
            logger.info(f"[CCUID/{engine.name}] CLAUDE_CODE_EXECUTABLE={system_claude}")
    return env


def _supports_resume(caps: AgentCapabilities | None) -> bool:
    return bool(caps and caps.session_capabilities and caps.session_capabilities.resume is not None)


def _supports_close(caps: AgentCapabilities | None) -> bool:
    return bool(caps and caps.session_capabilities and caps.session_capabilities.close is not None)


def _supports_list(caps: AgentCapabilities | None) -> bool:
    return bool(caps and caps.session_capabilities and caps.session_capabilities.list is not None)


def _supports_load(caps: AgentCapabilities | None) -> bool:
    return bool(caps and caps.load_session)


def _supports_image_prompt(caps: AgentCapabilities | None) -> bool:
    return bool(caps and caps.prompt_capabilities and caps.prompt_capabilities.image)


async def _close_stdin(proc: asyncio.subprocess.Process) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    with contextlib.suppress(Exception):
        stdin.close()
        await asyncio.wait_for(stdin.wait_closed(), timeout=_CONNECTION_CLOSE_TIMEOUT)


async def _terminate_process(proc: asyncio.subprocess.Process, *, engine_name: str) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT)
    except TimeoutError:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT)
    except ProcessLookupError:
        pass
    except Exception as err:
        logger.warning(f"[CCUID/{engine_name}] terminate failed pid={proc.pid}: {err!r}")


async def _close_process_transport(proc: asyncio.subprocess.Process) -> None:
    try:
        transport = cast(_ProcessTransportOwner, cast(object, proc))._transport
    except AttributeError:
        return
    if transport is None:
        return
    with contextlib.suppress(Exception):
        transport.close()
    await asyncio.sleep(0)


def _apply_response_state(
    s: ACPSession,
    response: _ResponseState | None,
) -> None:
    if response is None:
        return
    if not isinstance(response, SetSessionConfigOptionResponse | ConfigOptionUpdate):
        if response.models is not None:
            s.model_id, s.model_name, s.available_models = _extract_models(response.models)
        if response.modes is not None:
            s.current_mode_id, s.available_modes = _extract_modes(response.modes)
    config_options = response.config_options
    if config_options is not None:
        s.config_options = tuple(config_options)
        cfg_id, cfg_name, cfg_available, _ = _extract_model_config(s.config_options)
        if cfg_id is not None:
            s.model_id = cfg_id
            s.model_name = cfg_name
            s.available_models = cfg_available
        cfg_mode_id, cfg_available_modes, _ = _extract_mode_config(s.config_options)
        if cfg_mode_id is not None:
            s.current_mode_id = cfg_mode_id
            s.available_modes = cfg_available_modes


def _reset_prompt_state(s: ACPSession) -> None:
    s.last_usage_update = None
    s.last_prompt_usage = None
    s.last_prompt_elapsed = None


class ACPBackend:
    def __init__(self, engine: EngineSpec, approvals: PermissionApprovalStore) -> None:
        self.engine = engine
        self._approvals = approvals
        self._sess: dict[str, ACPSession] = {}
        self._starting: dict[str, asyncio.Task[ACPSession]] = {}
        self._lock = asyncio.Lock()
        self._spawn_failures = 0
        self._cooldown_until = 0.0
        self._watch_tasks: set[asyncio.Task[None]] = set()

    def get_native_session_id(self, sid: str) -> str | None:
        s = self._sess.get(sid)
        return s.acp_sid if s else None

    def get_model(self, sid: str) -> tuple[str | None, str | None]:
        """Both `None` when the agent didn't advertise a model in new/load."""
        s = self._sess.get(sid)
        if s is None:
            return None, None
        return s.model_id, s.model_name

    def list_models(self, sid: str) -> tuple[str | None, tuple[tuple[str, str], ...]]:
        """返回 (当前 model_id, 全部 (id,name) 对)。session 没起就 (None, ())。"""
        s = self._sess.get(sid)
        if s is None:
            return None, ()
        return s.model_id, s.available_models

    def list_modes(self, sid: str) -> tuple[str | None, tuple[tuple[str, str, str | None], ...]]:
        s = self._sess.get(sid)
        if s is None:
            return None, ()
        return s.current_mode_id, s.available_modes

    def list_commands(self, sid: str) -> tuple[AvailableCommand, ...]:
        s = self._sess.get(sid)
        return s.available_commands if s else ()

    def get_session_info(self, sid: str) -> tuple[str | None, str | None]:
        s = self._sess.get(sid)
        if s is None:
            return None, None
        return s.session_title, s.session_updated_at

    def is_session_busy(self, sid: str) -> bool:
        s = self._sess.get(sid)
        return bool(s and s.rpc_lock.locked())

    def snapshot_elapsed(self, sid: str) -> float | None:
        """per-prompt agent 推理耗时，PromptResponse 之前返回 None。"""
        s = self._sess.get(sid)
        return s.last_prompt_elapsed if s else None

    def snapshot_usage(self, sid: str) -> PromptUsage | None:
        s = self._sess.get(sid)
        if s is None:
            return None
        update = s.last_usage_update
        usage = s.last_prompt_usage
        if update is None and usage is None:
            return None
        cost = update.cost if update is not None else None
        snap = PromptUsage(
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            cached_read_tokens=usage.cached_read_tokens if usage is not None else None,
            cached_write_tokens=usage.cached_write_tokens if usage is not None else None,
            thought_tokens=usage.thought_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
            ctx_used=update.used if update is not None else None,
            ctx_size=update.size if update is not None else None,
            cost_amount=cost.amount if cost is not None else None,
            cost_currency=cost.currency if cost is not None else None,
        )
        return snap if snap.has_any_data else None

    async def set_model(self, sid: str, model_id: str) -> tuple[str, str] | None:
        """切到目录内的 model_id。SetSessionModelResponse 是空响应，本地直接更新缓存的
        (model_id, name)。目录里没有这条返回 None，让上层报 not found。"""
        s = self._sess.get(sid)
        if s is None:
            return None
        if s.rpc_lock.locked():
            raise BackendError("session 正在执行，结束后再切换 model")
        match = next(((mid, name) for mid, name in s.available_models if mid == model_id), None)
        if match is None:
            return None
        async with s.rpc_lock:
            if self._sess.get(sid) is not s or s.proc.returncode is not None:
                return None
            _, _, _, config_id = _extract_model_config(s.config_options)
            if config_id is not None:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    resp = await s.conn.set_config_option(config_id=config_id, value=model_id, session_id=s.acp_sid)
                if resp is not None:
                    s.config_options = tuple(resp.config_options)
                    _apply_response_state(s, resp)
                else:
                    s.model_id, s.model_name = match
                return match
            async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                await s.conn.set_session_model(model_id=model_id, session_id=s.acp_sid)
            s.model_id, s.model_name = match
        return match

    async def set_mode(self, sid: str, mode_id: str) -> tuple[str, str, str | None] | None:
        s = self._sess.get(sid)
        if s is None:
            return None
        if s.rpc_lock.locked():
            raise BackendError("session 正在执行，结束后再切换 mode")
        match = next((mode for mode in s.available_modes if mode[0] == mode_id), None)
        if match is None:
            return None
        async with s.rpc_lock:
            if self._sess.get(sid) is not s or s.proc.returncode is not None:
                return None
            _, _, config_id = _extract_mode_config(s.config_options)
            if config_id is not None:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    resp = await s.conn.set_config_option(config_id=config_id, value=mode_id, session_id=s.acp_sid)
                if resp is not None:
                    s.config_options = tuple(resp.config_options)
                    _apply_response_state(s, resp)
                else:
                    s.current_mode_id = mode_id
                return match
            async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                await s.conn.set_session_mode(mode_id=mode_id, session_id=s.acp_sid)
            s.current_mode_id = mode_id
        return match

    async def list_agent_sessions(
        self,
        workdir: str,
        *,
        cursor: str | None = None,
        cwd: str | None = None,
    ) -> tuple[bool, tuple[SessionInfo, ...], str | None]:
        """Return (supported, sessions, next_cursor). 会优先复用活跃连接；
        没有活跃连接时短暂启动 agent，只 initialize + session/list，不创建新 session。"""
        live_item = next(
            (
                (sid, s)
                for sid, s in self._sess.items()
                if s.proc.returncode is None and not s.rpc_lock.locked() and _same_workdir(s.workdir, workdir)
            ),
            None,
        )
        if live_item is not None:
            live_sid, live = live_item
            if not _supports_list(live.agent_capabilities):
                return False, (), None
            async with live.rpc_lock:
                if self._sess.get(live_sid) is live and live.proc.returncode is None:
                    async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                        resp = await live.conn.list_sessions(cwd=cwd, cursor=cursor)
                    return True, tuple(resp.sessions), resp.next_cursor

        proc: asyncio.subprocess.Process | None = None
        conn: Any | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        try:
            os.makedirs(workdir, exist_ok=True)
            cmd = _resolve_launcher(self.engine.cmd)
            logger.info(f"[CCUID/{self.engine.name}] list_sessions via {' '.join(cmd)} cwd={workdir}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                limit=_LIMIT,
                env=_build_spawn_env(self.engine),
            )
            record_spawn(proc.pid, self.engine.name)
            stderr_task = asyncio.create_task(self._pump_stderr(proc, stderr_tail))
            assert proc.stdin is not None and proc.stdout is not None
            queue: asyncio.Queue[Any] = asyncio.Queue()
            conn = connect_to_agent(
                ACPClient(queue, f"list-{self.engine.name}", self._approvals),
                proc.stdin,
                proc.stdout,
                use_unstable_protocol=True,
            )
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                init = await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="CCUID", version=VERSION),
                )
                if not _supports_list(init.agent_capabilities):
                    return False, (), None
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    resp = await conn.list_sessions(cwd=cwd, cursor=cursor)
                return True, tuple(resp.sessions), resp.next_cursor
        except Exception as e:
            raise BackendError(f"获取 {self.engine.name} session 列表失败: {e}{format_tail(stderr_tail)}") from e
        finally:
            await self._cleanup_unregistered_process(conn, proc, stderr_task)

    async def prompt(
        self,
        sid: str,
        workdir: str,
        blocks: list[Any],
        resume_id: str | None = None,
    ) -> AsyncIterator[Any]:
        s = await self._ensure(sid, workdir, resume_id)
        if any(isinstance(block, ImageContentBlock) for block in blocks) and not _supports_image_prompt(
            s.agent_capabilities
        ):
            raise BackendError(f"{self.engine.name} 当前未声明支持图片输入")
        if s.rpc_lock.locked():
            raise BackendError("session 正在切换配置，稍后再发送")

        async def _run() -> None:
            try:
                resp = await s.conn.prompt(prompt=blocks, session_id=s.acp_sid)
                await s.queue.put(resp)
            except BaseException as e:  # noqa: BLE001
                await s.queue.put(e)

        def _cache_item(item: Any) -> None:
            if isinstance(item, UsageUpdate):
                s.last_usage_update = item
            elif isinstance(item, PromptResponse):
                s.last_prompt_elapsed = time.monotonic() - t0
                if item.usage is not None:
                    s.last_prompt_usage = item.usage
            elif isinstance(item, ConfigOptionUpdate):
                s.config_options = tuple(item.config_options)
                _apply_response_state(s, item)
            elif isinstance(item, CurrentModeUpdate):
                s.current_mode_id = item.current_mode_id
            elif isinstance(item, AvailableCommandsUpdate):
                s.available_commands = tuple(item.available_commands)
            elif isinstance(item, SessionInfoUpdate):
                fields = item.model_fields_set
                if "title" in fields:
                    s.session_title = item.title
                if "updated_at" in fields:
                    s.session_updated_at = item.updated_at

        async def _drain_trailing_updates() -> None:
            deadline = time.monotonic() + _TRAILING_UPDATE_DRAIN_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    item = await asyncio.wait_for(s.queue.get(), timeout=remaining)
                except TimeoutError:
                    return
                if item is None or isinstance(item, BaseException):
                    continue
                _cache_item(item)

        async with s.rpc_lock:
            if self._sess.get(sid) is not s or s.proc.returncode is not None:
                raise BackendError("session closed")
            # elapsed 计时：_run 起跑为起点，PromptResponse 落入循环为终点；mid-stream 保持 None。
            # usage 必须按 prompt 清零，否则本轮 agent 未返回 usage 时会误显示上一轮统计。
            _reset_prompt_state(s)
            t0 = time.monotonic()
            # 同 session 跨 prompt 复用同一条 queue；上一轮 cancel 收尾可能残留 session_update，
            # 进新一轮前清掉，否则被新 prompt 的 loop 误当自己的输出（症状：新提问返回上次答案）。
            while not s.queue.empty():
                s.queue.get_nowait()
            task = asyncio.create_task(_run())
            try:
                while True:
                    item = await s.queue.get()
                    if item is None:
                        # 子进程退出：stderr_tail 含真实退出原因，附给用户排查
                        raise BackendError(f"ACP {self.engine.name} 退出{format_tail(s.stderr_tail)}")
                    if isinstance(item, BaseException):
                        # prompt 阶段错误（如 codex TLS 重连）：不附 stderr，那坨 noise 进日志够排查了，错误卡只给主因
                        raise BackendError(str(item)) from item
                    # sniff before yield —— footer/render 各自消费
                    _cache_item(item)
                    yield item
                    if isinstance(item, PromptResponse):
                        await _drain_trailing_updates()
                        return
            finally:
                # 必须 cancel + await：只 cancel 不 await，_run 会继续跑一小段（直到 await prompt 抛
                # CancelledError 再 push 异常到 queue），而此时 prompt_lock 已释放，下一轮会看到残留事件。
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def cancel(self, sid: str) -> None:
        s = self._sess.get(sid)
        if s and s.proc.returncode is None:
            try:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    await s.conn.cancel(session_id=s.acp_sid)
            except Exception as err:
                logger.warning(f"[CCUID/{self.engine.name}] cancel failed sid={sid}: {err!r}")

    async def close(self, sid: str) -> None:
        async with self._lock:
            s = self._sess.pop(sid, None)
            starting = self._starting.pop(sid, None)
        if starting is not None:
            await self._cancel_starting(starting)
        if s:
            await self._teardown(s)

    async def close_all(self) -> None:
        async with self._lock:
            sess = list(self._sess.values())
            self._sess.clear()
            starting = list(self._starting.values())
            self._starting.clear()
        for task in starting:
            await self._cancel_starting(task)
        for s in sess:
            await self._teardown(s)

    async def _ensure(self, sid: str, workdir: str, resume_id: str | None) -> ACPSession:
        while True:
            stale: ACPSession | None = None
            start_task: asyncio.Task[ACPSession] | None = None
            async with self._lock:
                s = self._sess.get(sid)
                if s is not None:
                    if s.proc.returncode is None:
                        return s
                    stale = self._sess.pop(sid)
                else:
                    start_task = self._starting.get(sid)
                    if start_task is None:
                        now = time.time()
                        if now < self._cooldown_until:
                            raise BackendError(
                                f"ACP {self.engine.name} 启动熔断中，{int(self._cooldown_until - now)}s 后重试"
                            )
                        start_task = asyncio.create_task(
                            self._start_session(sid, workdir, resume_id),
                            name=f"CCUID-start-{self.engine.name}-{sid}",
                        )
                        self._starting[sid] = start_task

            if stale is not None:
                await self._teardown(stale)
                continue

            assert start_task is not None
            try:
                started = await start_task
            except asyncio.CancelledError:
                await self._forget_starting(sid, start_task)
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                raise BackendError("session closed") from None
            except Exception:
                await self._forget_starting(sid, start_task)
                raise

            registered = False
            try:
                async with self._lock:
                    live = self._sess.get(sid)
                    if live is not None and live.proc.returncode is None:
                        registered = live is started
                        return live
                    if self._starting.get(sid) is not start_task:
                        raise BackendError("session closed")
                    self._starting.pop(sid, None)
                    self._sess[sid] = started
                    registered = True
                    return started
            finally:
                if not registered:
                    owner = await self._forget_starting(sid, start_task)
                    if owner:
                        await self._teardown(started)

    async def _forget_starting(self, sid: str, task: asyncio.Task[ACPSession]) -> bool:
        async with self._lock:
            if self._starting.get(sid) is task:
                self._starting.pop(sid, None)
                return True
        return False

    async def _cancel_starting(self, task: asyncio.Task[ACPSession]) -> None:
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            s = await task
            await self._teardown(s)

    async def _cleanup_unregistered_process(
        self,
        conn: Any | None,
        proc: asyncio.subprocess.Process | None,
        stderr_task: asyncio.Task[None] | None,
    ) -> None:
        if conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), timeout=_CONNECTION_CLOSE_TIMEOUT)
        if proc is not None:
            await _close_stdin(proc)
            await _terminate_process(proc, engine_name=self.engine.name)
            record_teardown(proc.pid)
        if stderr_task is not None:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
        if proc is not None:
            await _close_process_transport(proc)

    def _record_start_failure(self) -> None:
        self._spawn_failures += 1
        if self._spawn_failures < _SPAWN_FAIL_THRESHOLD:
            return
        self._cooldown_until = time.time() + _SPAWN_COOLDOWN_SEC
        logger.warning(
            f"[CCUID/{self.engine.name}] 连续 {self._spawn_failures} 次启动失败，熔断 {_SPAWN_COOLDOWN_SEC}s"
        )

    async def _start_session(self, sid: str, workdir: str, resume_id: str | None) -> ACPSession:
        os.makedirs(workdir, exist_ok=True)
        cmd = _resolve_launcher(self.engine.cmd)
        logger.info(f"[CCUID/{self.engine.name}] {' '.join(cmd)} cwd={workdir}")

        proc: asyncio.subprocess.Process | None = None
        conn: Any | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        client = ACPClient(queue, sid, self._approvals)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                limit=_LIMIT,
                env=_build_spawn_env(self.engine),
            )
            # 登记到 spawned_pids.json：gscore 被强杀后，下次启动 reap_orphans() 按 PID 清掉孤儿，避免泄露。
            record_spawn(proc.pid, self.engine.name)
            stderr_task = asyncio.create_task(self._pump_stderr(proc, stderr_tail))
            assert proc.stdin is not None and proc.stdout is not None
            conn = connect_to_agent(client, proc.stdin, proc.stdout, use_unstable_protocol=True)
            acp_sid: str
            agent_capabilities: AgentCapabilities | None = None
            session_response: _SessionStateResponse | None = None
            # 子进程 stdout 卡死会让握手永久挂；一个总超时罩住 initialize + new/load_session，超时由外层 except 清进程。
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                init = await conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="CCUID", version=VERSION),
                )
                agent_capabilities = init.agent_capabilities
                if init.protocol_version != PROTOCOL_VERSION:
                    logger.warning(f"[CCUID/{self.engine.name}] protocol {init.protocol_version} != {PROTOCOL_VERSION}")
                if resume_id:
                    if _supports_resume(agent_capabilities):
                        try:
                            session_response = await conn.resume_session(cwd=workdir, session_id=resume_id)
                            acp_sid = resume_id
                        except Exception as resume_err:
                            logger.warning(
                                f"[CCUID/{self.engine.name}] resume_session 失败，尝试 load_session: {resume_err}"
                            )
                            session_response = None
                            acp_sid = ""
                    else:
                        session_response = None
                        acp_sid = ""
                    if session_response is None and _supports_load(agent_capabilities):
                        try:
                            session_response = await conn.load_session(cwd=workdir, session_id=resume_id)
                            acp_sid = resume_id
                        except Exception as load_err:
                            logger.warning(f"[CCUID/{self.engine.name}] load_session 失败，创建新 session: {load_err}")
                            session_response = None
                            acp_sid = ""
                    if session_response is None:
                        if not _supports_resume(agent_capabilities) and not _supports_load(agent_capabilities):
                            logger.info(f"[CCUID/{self.engine.name}] 未声明 resume/load，创建新 session")
                        new_resp = await conn.new_session(cwd=workdir)
                        acp_sid = new_resp.session_id
                        session_response = new_resp
                else:
                    new_resp = await conn.new_session(cwd=workdir)
                    acp_sid = new_resp.session_id
                    session_response = new_resp
            assert session_response is not None
            model_id, model_name, available_models = _extract_models(session_response.models)
            current_mode_id, available_modes = _extract_modes(session_response.modes)
            config_options = (
                tuple(session_response.config_options) if session_response.config_options is not None else ()
            )
            cfg_model_id, cfg_model_name, cfg_available_models, cfg_model_config_id = _extract_model_config(
                config_options
            )
            if cfg_model_id is not None:
                model_id = cfg_model_id
                model_name = cfg_model_name
                available_models = cfg_available_models
            cfg_mode_id, cfg_available_modes, _ = _extract_mode_config(config_options)
            if cfg_mode_id is not None:
                current_mode_id = cfg_mode_id
                available_modes = cfg_available_modes
            # reapply 失败 / id 已不在 available 时 drop 记录，回 default 不再重试。
            sticky = await CCUIDSessionModel.fetch(sid)
            if sticky is not None and sticky != model_id:
                sticky_name = next((n for mid, n in available_models if mid == sticky), None)
                if sticky_name is None:
                    await CCUIDSessionModel.drop(sid)
                else:
                    try:
                        async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                            if cfg_model_config_id is not None:
                                resp = await conn.set_config_option(
                                    config_id=cfg_model_config_id,
                                    value=sticky,
                                    session_id=acp_sid,
                                )
                                if resp is not None:
                                    config_options = tuple(resp.config_options)
                            else:
                                await conn.set_session_model(model_id=sticky, session_id=acp_sid)
                        model_id, model_name = sticky, sticky_name
                    except Exception as sticky_err:
                        logger.warning(f"[CCUID/{self.engine.name}] sticky {sticky} reapply: {sticky_err}")
                        await CCUIDSessionModel.drop(sid)
        except asyncio.CancelledError:
            await self._cleanup_unregistered_process(conn, proc, stderr_task)
            raise
        except Exception as e:
            await self._cleanup_unregistered_process(conn, proc, stderr_task)
            self._record_start_failure()
            raise BackendError(f"启动 {self.engine.name} 失败: {e}{format_tail(stderr_tail)}") from e

        self._spawn_failures = 0
        self._cooldown_until = 0.0
        # 保引用，避免 GC 提前回收（Python 3.11+ 会发 RuntimeWarning）。
        watch_task = asyncio.create_task(self._watch_exit(proc, queue))
        self._watch_tasks.add(watch_task)
        watch_task.add_done_callback(self._watch_tasks.discard)
        return ACPSession(
            proc=proc,
            conn=conn,
            acp_sid=acp_sid,
            workdir=workdir,
            queue=queue,
            stderr_task=stderr_task,
            watch_task=watch_task,
            stderr_tail=stderr_tail,
            agent_capabilities=agent_capabilities,
            model_id=model_id,
            model_name=model_name,
            available_models=available_models,
            config_options=config_options,
            current_mode_id=current_mode_id,
            available_modes=available_modes,
        )

    async def _teardown(self, s: ACPSession) -> None:
        if _supports_close(s.agent_capabilities):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    s.conn.close_session(session_id=s.acp_sid),
                    timeout=_CONNECTION_CLOSE_TIMEOUT,
                )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(s.conn.close(), timeout=_CONNECTION_CLOSE_TIMEOUT)
        await _close_stdin(s.proc)
        await _terminate_process(s.proc, engine_name=self.engine.name)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(s.watch_task, timeout=_CONNECTION_CLOSE_TIMEOUT)
        if not s.stderr_task.done():
            s.stderr_task.cancel()
        with contextlib.suppress(BaseException):
            await s.stderr_task
        await _close_process_transport(s.proc)
        record_teardown(s.proc.pid)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process, tail: deque[str]) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    line = _cap_stderr_line(line)
                    tail.append(line)
                    logger.warning(f"[CCUID/{self.engine.name}] {line}")

    async def _watch_exit(self, proc: asyncio.subprocess.Process, queue: asyncio.Queue[Any]) -> None:
        try:
            await proc.wait()
        finally:
            await queue.put(None)
