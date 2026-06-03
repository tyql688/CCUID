from __future__ import annotations

import time
import asyncio
import contextlib
from typing import TYPE_CHECKING, TypeVar
from collections import deque
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable, AsyncIterator

from acp import (
    PROTOCOL_VERSION,
    connect_to_agent,
)
from acp.schema import (
    Usage,
    ModelInfo,
    SessionInfo,
    SessionMode,
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
from ..paths import same_path
from .client import ACPClient, PermissionApprovalStore
from .prompt import PromptBlock
from .orphans import record_teardown
from .process import (
    STDERR_TAIL_LINES,
    CONNECTION_CLOSE_TIMEOUT_SEC,
    stdio,
    close_stdin,
    format_tail,
    spawn_process,
    terminate_process,
    close_process_transport,
    cleanup_unregistered_process,
)
from ..engines import EngineSpec
from ...version import VERSION
from ..database import CCUIDSessionModel

if TYPE_CHECKING:
    from acp.client.connection import ClientSideConnection

_TRAILING_UPDATE_DRAIN_TIMEOUT = 0.05
_RPC_MUTATION_TIMEOUT = 30
_SPAWN_FAIL_THRESHOLD = 3
_SPAWN_COOLDOWN_SEC = 60
# ACP 握手 (initialize + new/load_session) 总超时：子进程 stdin/stdout 卡死时不让 prompt 永久挂，也兜 npx 冷启动。
_HANDSHAKE_TIMEOUT_SEC = 60
_ConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean
_SessionStateResponse = NewSessionResponse | LoadSessionResponse | ResumeSessionResponse
_ResponseState = _SessionStateResponse | SetSessionConfigOptionResponse | ConfigOptionUpdate
_T = TypeVar("_T")


class BackendError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class PromptUsage:
    """ACP usage 快照：token 用 PromptResponse.usage，ctx/cost 用 UsageUpdate。"""

    usage: Usage | None = None
    update: UsageUpdate | None = None


@dataclass(slots=True)
class ACPSession:
    proc: asyncio.subprocess.Process
    conn: ClientSideConnection
    acp_sid: str
    workdir: str
    queue: asyncio.Queue[object]
    stderr_task: asyncio.Task[None]
    watch_task: asyncio.Task[None]
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    agent_capabilities: AgentCapabilities | None = None
    # new/load_session 响应里的当前模型；None = agent 没声明 models（老 adapter），渲染层据此决定是否展示。
    model_id: str | None = None
    model_name: str | None = None
    # new/load_session 给的整张模型目录；`cc 模型` 用，session 期间稳定不重拉。
    available_models: tuple[ModelInfo, ...] = ()
    config_options: tuple[_ConfigOption, ...] = ()
    current_mode_id: str | None = None
    available_modes: tuple[SessionMode, ...] = ()
    available_commands: tuple[AvailableCommand, ...] = ()
    session_title: str | None = None
    session_updated_at: str | None = None
    # backend.prompt 流式 sniff；Usage 是 cumulative，跨 prompt 直接覆盖
    last_usage_update: UsageUpdate | None = None
    last_prompt_usage: Usage | None = None
    # per-prompt 推理耗时（_run 起跑 → PromptResponse），含权限审批等待
    last_prompt_elapsed: float | None = None
    rpc_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True, kw_only=True)
class _SessionPresentation:
    model_id: str | None
    model_name: str | None
    available_models: tuple[ModelInfo, ...]
    config_options: tuple[_ConfigOption, ...]
    current_mode_id: str | None
    available_modes: tuple[SessionMode, ...]
    model_config_id: str | None


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


def _cache_stream_state(s: ACPSession, item: object, *, started_at: float | None = None) -> None:
    if isinstance(item, UsageUpdate):
        s.last_usage_update = item
    elif isinstance(item, PromptResponse):
        if started_at is not None:
            s.last_prompt_elapsed = time.monotonic() - started_at
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


def _presentation_from_response(response: _SessionStateResponse) -> _SessionPresentation:
    model_id, model_name, available_models = _extract_models(response.models)
    current_mode_id, available_modes = _extract_modes(response.modes)
    config_options = tuple(response.config_options) if response.config_options is not None else ()

    cfg_model_id, cfg_model_name, cfg_available_models, cfg_model_config_id = _extract_model_config(config_options)
    if cfg_model_id is not None:
        model_id = cfg_model_id
        model_name = cfg_model_name
        available_models = cfg_available_models

    cfg_mode_id, cfg_available_modes, _ = _extract_mode_config(config_options)
    if cfg_mode_id is not None:
        current_mode_id = cfg_mode_id
        available_modes = cfg_available_modes

    return _SessionPresentation(
        model_id=model_id,
        model_name=model_name,
        available_models=available_models,
        config_options=config_options,
        current_mode_id=current_mode_id,
        available_modes=available_modes,
        model_config_id=cfg_model_config_id,
    )


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

    def list_models(self, sid: str) -> tuple[str | None, tuple[ModelInfo, ...]]:
        """返回 (当前 model_id, 全部 ModelInfo)。session 没起就 (None, ())。"""
        s = self._sess.get(sid)
        if s is None:
            return None, ()
        return s.model_id, s.available_models

    def list_modes(self, sid: str) -> tuple[str | None, tuple[SessionMode, ...]]:
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
        return PromptUsage(usage=usage, update=update)

    async def _with_idle_session(
        self,
        sid: str,
        label: str,
        action: Callable[[ACPSession], Awaitable[_T | None]],
    ) -> _T | None:
        s = self._sess.get(sid)
        if s is None:
            return None
        if s.rpc_lock.locked():
            raise BackendError(f"session 正在执行，结束后再切换 {label}")
        try:
            async with s.rpc_lock:
                if self._sess.get(sid) is not s or s.proc.returncode is not None:
                    return None
                return await action(s)
        except TimeoutError as e:
            raise BackendError(f"切换 {label} 超时") from e
        except Exception as e:
            raise BackendError(f"切换 {label} 失败: {e}") from e

    async def set_model(self, sid: str, model_id: str) -> ModelInfo | None:
        """切到目录内的 model_id。SetSessionModelResponse 是空响应，本地直接更新缓存的
        (model_id, name)。目录里没有这条返回 None，让上层报 not found。"""

        async def switch(session: ACPSession) -> ModelInfo | None:
            match = next((model for model in session.available_models if model.model_id == model_id), None)
            if match is None:
                return None
            _, _, _, config_id = _extract_model_config(session.config_options)
            if config_id is not None:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    resp = await session.conn.set_config_option(
                        config_id=config_id,
                        value=model_id,
                        session_id=session.acp_sid,
                    )
                if resp is not None:
                    session.config_options = tuple(resp.config_options)
                    _apply_response_state(session, resp)
                else:
                    session.model_id, session.model_name = match.model_id, match.name
                return match
            async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                await session.conn.set_session_model(model_id=model_id, session_id=session.acp_sid)
            session.model_id, session.model_name = match.model_id, match.name
            return match

        return await self._with_idle_session(sid, "model", switch)

    async def set_mode(self, sid: str, mode_id: str) -> SessionMode | None:
        async def switch(session: ACPSession) -> SessionMode | None:
            match = next((mode for mode in session.available_modes if mode.id == mode_id), None)
            if match is None:
                return None
            _, _, config_id = _extract_mode_config(session.config_options)
            if config_id is not None:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    resp = await session.conn.set_config_option(
                        config_id=config_id,
                        value=mode_id,
                        session_id=session.acp_sid,
                    )
                if resp is not None:
                    session.config_options = tuple(resp.config_options)
                    _apply_response_state(session, resp)
                else:
                    session.current_mode_id = mode_id
                return match
            async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                await session.conn.set_session_mode(mode_id=mode_id, session_id=session.acp_sid)
            session.current_mode_id = mode_id
            return match

        return await self._with_idle_session(sid, "mode", switch)

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
                if s.proc.returncode is None and not s.rpc_lock.locked() and same_path(s.workdir, workdir)
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
        conn: ClientSideConnection | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        try:
            spawned = await spawn_process(
                self.engine,
                workdir,
                stderr_tail,
                log_prefix="list_sessions via",
            )
            proc = spawned.proc
            stderr_task = spawned.stderr_task
            queue: asyncio.Queue[object] = asyncio.Queue()
            conn = connect_to_agent(
                ACPClient(queue, f"list-{self.engine.name}", self._approvals),
                *stdio(proc, engine_name=self.engine.name),
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
            await cleanup_unregistered_process(
                conn=conn,
                proc=proc,
                stderr_task=stderr_task,
                engine_name=self.engine.name,
            )

    async def prompt(
        self,
        sid: str,
        workdir: str,
        blocks: list[PromptBlock],
        resume_id: str | None = None,
    ) -> AsyncIterator[object]:
        s = await self._ensure(sid, workdir, resume_id)
        if any(isinstance(block, ImageContentBlock) for block in blocks) and not _supports_image_prompt(
            s.agent_capabilities
        ):
            raise BackendError(f"{self.engine.name} 当前未声明支持图片输入")
        if s.rpc_lock.locked():
            raise BackendError("session 正在切换配置，稍后再发送")

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
            task = asyncio.create_task(self._request_prompt(s, blocks))
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
                    _cache_stream_state(s, item, started_at=t0)
                    yield item
                    if isinstance(item, PromptResponse):
                        await self._drain_trailing_updates(s)
                        return
            finally:
                # 必须 cancel + await：只 cancel 不 await，_run 会继续跑一小段（直到 await prompt 抛
                # CancelledError 再 push 异常到 queue），而此时 prompt_lock 已释放，下一轮会看到残留事件。
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def _request_prompt(self, s: ACPSession, blocks: list[PromptBlock]) -> None:
        try:
            resp = await s.conn.prompt(prompt=blocks, session_id=s.acp_sid)
            await s.queue.put(resp)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            await s.queue.put(e)

    async def _drain_trailing_updates(self, s: ACPSession) -> None:
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
            _cache_stream_state(s, item)

    async def cancel(self, sid: str) -> None:
        s = self._sess.get(sid)
        if s and s.proc.returncode is None:
            try:
                async with asyncio.timeout(_RPC_MUTATION_TIMEOUT):
                    await s.conn.cancel(session_id=s.acp_sid)
            except Exception as err:
                logger.debug(f"[CCUID/{self.engine.name}] cancel failed sid={sid}: {err!r}")

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
                        now = time.monotonic()
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

            if start_task is None:
                raise BackendError("session start task missing")
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

    def _record_start_failure(self) -> None:
        self._spawn_failures += 1
        if self._spawn_failures < _SPAWN_FAIL_THRESHOLD:
            return
        self._cooldown_until = time.monotonic() + _SPAWN_COOLDOWN_SEC
        logger.warning(
            f"[CCUID/{self.engine.name}] 连续 {self._spawn_failures} 次启动失败，熔断 {_SPAWN_COOLDOWN_SEC}s"
        )

    async def _initialize_connection(self, conn: ClientSideConnection) -> AgentCapabilities | None:
        init = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="CCUID", version=VERSION),
        )
        if init.protocol_version != PROTOCOL_VERSION:
            logger.warning(f"[CCUID/{self.engine.name}] protocol {init.protocol_version} != {PROTOCOL_VERSION}")
        return init.agent_capabilities

    async def _open_session(
        self,
        conn: ClientSideConnection,
        *,
        workdir: str,
        resume_id: str | None,
        agent_capabilities: AgentCapabilities | None,
    ) -> tuple[str, _SessionStateResponse]:
        if resume_id is None:
            new_resp = await conn.new_session(cwd=workdir)
            return new_resp.session_id, new_resp

        if _supports_resume(agent_capabilities):
            try:
                return resume_id, await conn.resume_session(cwd=workdir, session_id=resume_id)
            except Exception as resume_err:
                logger.debug(f"[CCUID/{self.engine.name}] resume_session 失败，尝试 load_session: {resume_err}")

        if _supports_load(agent_capabilities):
            try:
                return resume_id, await conn.load_session(cwd=workdir, session_id=resume_id)
            except Exception as load_err:
                logger.debug(f"[CCUID/{self.engine.name}] load_session 失败，创建新 session: {load_err}")

        if not _supports_resume(agent_capabilities) and not _supports_load(agent_capabilities):
            logger.debug(f"[CCUID/{self.engine.name}] 未声明 resume/load，创建新 session")
        new_resp = await conn.new_session(cwd=workdir)
        return new_resp.session_id, new_resp

    async def _reapply_sticky_model(
        self,
        conn: ClientSideConnection,
        *,
        sid: str,
        acp_sid: str,
        state: _SessionPresentation,
    ) -> None:
        sticky = await CCUIDSessionModel.fetch(sid)
        if sticky is None or sticky == state.model_id:
            return

        sticky_name = next((model.name for model in state.available_models if model.model_id == sticky), None)
        if sticky_name is None:
            await CCUIDSessionModel.drop(sid)
            return

        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                if state.model_config_id is not None:
                    resp = await conn.set_config_option(
                        config_id=state.model_config_id,
                        value=sticky,
                        session_id=acp_sid,
                    )
                    if resp is not None:
                        state.config_options = tuple(resp.config_options)
                else:
                    await conn.set_session_model(model_id=sticky, session_id=acp_sid)
            state.model_id, state.model_name = sticky, sticky_name
        except Exception as sticky_err:
            logger.debug(f"[CCUID/{self.engine.name}] sticky {sticky} reapply: {sticky_err}")
            await CCUIDSessionModel.drop(sid)

    async def _start_session(self, sid: str, workdir: str, resume_id: str | None) -> ACPSession:
        proc: asyncio.subprocess.Process | None = None
        conn: ClientSideConnection | None = None
        stderr_task: asyncio.Task[None] | None = None
        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        queue: asyncio.Queue[object] = asyncio.Queue()
        client = ACPClient(queue, sid, self._approvals)
        try:
            spawned = await spawn_process(self.engine, workdir, stderr_tail)
            proc = spawned.proc
            stderr_task = spawned.stderr_task
            conn = connect_to_agent(client, *stdio(proc, engine_name=self.engine.name), use_unstable_protocol=True)
            # 子进程 stdout 卡死会让握手永久挂；一个总超时罩住 initialize + new/load_session，超时由外层 except 清进程。
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                agent_capabilities = await self._initialize_connection(conn)
                acp_sid, session_response = await self._open_session(
                    conn,
                    workdir=workdir,
                    resume_id=resume_id,
                    agent_capabilities=agent_capabilities,
                )
            state = _presentation_from_response(session_response)
            # reapply 失败 / id 已不在 available 时 drop 记录，回 default 不再重试。
            await self._reapply_sticky_model(conn, sid=sid, acp_sid=acp_sid, state=state)
        except asyncio.CancelledError:
            await cleanup_unregistered_process(
                conn=conn,
                proc=proc,
                stderr_task=stderr_task,
                engine_name=self.engine.name,
            )
            raise
        except Exception as e:
            await cleanup_unregistered_process(
                conn=conn,
                proc=proc,
                stderr_task=stderr_task,
                engine_name=self.engine.name,
            )
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
            model_id=state.model_id,
            model_name=state.model_name,
            available_models=state.available_models,
            config_options=state.config_options,
            current_mode_id=state.current_mode_id,
            available_modes=state.available_modes,
        )

    async def _teardown(self, s: ACPSession) -> None:
        if _supports_close(s.agent_capabilities):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    s.conn.close_session(session_id=s.acp_sid),
                    timeout=CONNECTION_CLOSE_TIMEOUT_SEC,
                )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(s.conn.close(), timeout=CONNECTION_CLOSE_TIMEOUT_SEC)
        await close_stdin(s.proc)
        await terminate_process(s.proc, engine_name=self.engine.name)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(s.watch_task, timeout=CONNECTION_CLOSE_TIMEOUT_SEC)
        if not s.stderr_task.done():
            s.stderr_task.cancel()
        with contextlib.suppress(BaseException):
            await s.stderr_task
        await close_process_transport(s.proc)
        record_teardown(s.proc.pid)

    async def _watch_exit(self, proc: asyncio.subprocess.Process, queue: asyncio.Queue[object]) -> None:
        try:
            await proc.wait()
        finally:
            await queue.put(None)
