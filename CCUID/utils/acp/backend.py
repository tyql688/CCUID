from __future__ import annotations

import os
import time
import shutil
import asyncio
import contextlib
from typing import Any
from collections import deque
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from acp import (
    PROTOCOL_VERSION,
    connect_to_agent,
)
from acp.schema import (
    Usage,
    UsageUpdate,
    Implementation,
    PromptResponse,
    SessionModelState,
    ClientCapabilities,
)
from gsuid_core.logger import logger

from .client import ACPClient
from .orphans import record_spawn, record_teardown
from ..engines import EngineSpec
from ...version import VERSION
from ..database import CCUIDSessionModel

_LIMIT = 50 * 1024 * 1024
_TERMINATE_TIMEOUT = 3
_STDERR_TAIL_LINES = 50
_STDERR_DRAIN_TIMEOUT = 0.2
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
# ACP 握手 (initialize + new_session / load_session) 总超时。
# 单子进程 stdin/stdout 卡死时不让 prompt 永久挂；npx 冷启动也要兜得住。
_HANDSHAKE_TIMEOUT_SEC = 60


class BackendError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class PromptUsage:
    """ACP 累积 usage 快照。任一字段 None 表示 agent 没给——provider 能力矩阵见 rules.md。"""

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
                self.total_tokens,
                self.ctx_used,
                self.cost_amount,
            )
        )


@dataclass(slots=True)
class ACPSession:
    proc: asyncio.subprocess.Process
    conn: Any
    acp_sid: str
    queue: asyncio.Queue[Any]
    client: ACPClient
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    # current_model_id / display set from new_session / load_session response.
    # `None` 表示 agent 没声明 models 字段(老 adapter)；渲染层据此决定是否展示。
    model_id: str | None = None
    model_name: str | None = None
    # agent 在 new/load_session 响应里同时给出整张目录 (model_id, name) 对。
    # `cc 模型` 命令拿这个列；不变就别重拉，session 期间稳定。
    available_models: tuple[tuple[str, str], ...] = ()
    # backend.prompt 流式 sniff；spec 上 Usage 是 cumulative，跨 prompt 直接覆盖
    last_usage_update: UsageUpdate | None = None
    last_prompt_usage: Usage | None = None
    # per-prompt agent 推理耗时（_run 起跑 → PromptResponse），含权限审批等待
    last_prompt_elapsed: float | None = None


def format_tail(tail: deque[str]) -> str:
    if not tail:
        return ""
    return "\nstderr tail:\n" + "\n".join(tail)


def _resolve_launcher(cmd: tuple[str, ...]) -> tuple[str, ...]:
    """把 cmd[0] 解析成绝对路径再交给 subprocess。Windows 上 asyncio 的
    CreateProcessW 不走 PATHEXT，传 `npx` 会 WinError 2 找不到（实际是 `npx.cmd`）；
    shutil.which 自己懂 PATHEXT，所以无论平台都靠它兜底。绝对路径直接通过。"""
    if not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return cmd  # 留给 spawn 自己抛，错误信息会包含原始 cmd[0]
    return (resolved, *cmd[1:])


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
    """Claude wrapper 默认 spawn 自带的旧版 cli.js（模型列表跟终端不一致）；
    检测到本地有 `claude` binary 时通过 CLAUDE_CODE_EXECUTABLE 让它走终端那一份。"""
    env = dict(os.environ)
    _apply_agent_proxy_env(env, engine.name)
    if engine.name == "claude" and "CLAUDE_CODE_EXECUTABLE" not in env:
        system_claude = shutil.which("claude")
        if system_claude:
            env["CLAUDE_CODE_EXECUTABLE"] = system_claude
            logger.info(f"[CCUID/{engine.name}] CLAUDE_CODE_EXECUTABLE={system_claude}")
    return env


def _extract_models(
    state: SessionModelState | None,
) -> tuple[str | None, str | None, tuple[tuple[str, str], ...]]:
    """label 直接用 selected.name——agent 给什么就显示什么。"""
    if state is None:
        return None, None, ()
    available = tuple((m.model_id, m.name) for m in state.available_models)
    cur_name = next((name for mid, name in available if mid == state.current_model_id), None)
    return state.current_model_id, cur_name, available


class ACPBackend:
    def __init__(self, engine: EngineSpec) -> None:
        self.engine = engine
        self._sess: dict[str, ACPSession] = {}
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
        match = next(((mid, name) for mid, name in s.available_models if mid == model_id), None)
        if match is None:
            return None
        await s.conn.set_session_model(model_id=model_id, session_id=s.acp_sid)
        s.model_id, s.model_name = match
        return match

    async def prompt(
        self,
        sid: str,
        workdir: str,
        blocks: list[Any],
        resume_id: str | None = None,
        auto_approve: bool = False,
    ) -> AsyncIterator[Any]:
        s = await self._ensure(sid, workdir, resume_id)
        # 同 session 跨 prompt 复用同一条 queue (ACPClient.session_update 全往这推)；
        # 上一轮 cancel 收尾时可能还有 session_update 落在 queue 里，进新一轮前清掉，
        # 否则会被新 prompt 的 loop 当成自己的输出（症状：新提问返回上次的答案）。
        while not s.queue.empty():
            s.queue.get_nowait()

        # 一次性 yolo flag：本次 prompt 期间所有 permission 自动 allow_always。
        # ACPClient.request_permission 看这个值；prompt 结束时清掉。
        s.client.auto_approve_this_prompt = auto_approve

        async def _run() -> None:
            try:
                resp = await s.conn.prompt(prompt=blocks, session_id=s.acp_sid)
                await s.queue.put(resp)
            except BaseException as e:  # noqa: BLE001
                await s.queue.put(e)

        # B 方案 elapsed 起点：_run 起跑（agent 收到 prompt RPC）。终点在 PromptResponse 落入循环时。
        # mid-stream 期间 last_prompt_elapsed 保持上轮值 / None；render 端只在最终 flush 取值。
        t0 = time.monotonic()
        s.last_prompt_elapsed = None
        task = asyncio.create_task(_run())
        try:
            while True:
                item = await s.queue.get()
                if item is None:
                    # 子进程退出：stderr_tail 含真实退出原因，附给用户排查
                    raise BackendError(f"ACP {self.engine.name} 退出{format_tail(s.stderr_tail)}")
                if isinstance(item, BaseException):
                    # prompt 阶段错误（如 codex 的 TLS reconnect 重试）：不附 stderr，
                    # 那一坨 noise 进 gscore 日志已经够开发者排查，用户错误卡只看主因
                    raise BackendError(str(item)) from item
                # sniff before yield —— footer/render 各自消费
                if isinstance(item, UsageUpdate):
                    s.last_usage_update = item
                elif isinstance(item, PromptResponse):
                    s.last_prompt_elapsed = time.monotonic() - t0
                    if item.usage is not None:
                        s.last_prompt_usage = item.usage
                yield item
                if isinstance(item, PromptResponse):
                    return
        finally:
            # 必须 cancel + await：只 cancel 不 await 时 _run 还会继续 running 一小段
            # （直到 await prompt 抛 CancelledError 再 push 异常到 queue），那段时间
            # 我们已经 release prompt_lock，下一轮 prompt 进来会看到残留事件。
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def cancel(self, sid: str) -> None:
        s = self._sess.get(sid)
        if s and s.proc.returncode is None:
            await s.conn.cancel(session_id=s.acp_sid)

    async def close(self, sid: str) -> None:
        async with self._lock:
            s = self._sess.pop(sid, None)
        if s:
            await self._teardown(s)

    async def close_all(self) -> None:
        async with self._lock:
            sess = list(self._sess.values())
            self._sess.clear()
        for s in sess:
            await self._teardown(s)

    async def _ensure(self, sid: str, workdir: str, resume_id: str | None) -> ACPSession:
        async with self._lock:
            s = self._sess.get(sid)
            if s and s.proc.returncode is None:
                return s

            now = time.time()
            if now < self._cooldown_until:
                raise BackendError(f"ACP {self.engine.name} 启动熔断中，{int(self._cooldown_until - now)}s 后重试")

            os.makedirs(workdir, exist_ok=True)
            cmd = _resolve_launcher(self.engine.cmd)
            logger.info(f"[CCUID/{self.engine.name}] {' '.join(cmd)} cwd={workdir}")

            spawn_env = _build_spawn_env(self.engine)

            proc: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
            queue: asyncio.Queue[Any] = asyncio.Queue()
            client = ACPClient(queue, sid)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    limit=_LIMIT,
                    env=spawn_env,
                )
                # 立刻登记到 ~/.ccuid/spawned_pids.json：gscore 万一被强杀，下次
                # 启动通过 reap_orphans() 把这个 PID 找出来 kill 掉，避免内存泄露。
                record_spawn(proc.pid, self.engine.name)
                stderr_task = asyncio.create_task(self._pump_stderr(proc, stderr_tail))
                assert proc.stdin is not None and proc.stdout is not None
                conn = connect_to_agent(client, proc.stdin, proc.stdout)
                acp_sid: str
                # NewSessionResponse / LoadSessionResponse 都带 typed
                # `models: SessionModelState | None`，直接取，render 用来在
                # header 渲染真实模型名。
                models_state: SessionModelState | None
                # 子进程 stdout 卡死时整个握手会永久挂；统一一个总超时把
                # initialize + new/load_session 都罩住，超时由外层 except 兜底清进程。
                async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                    init = await conn.initialize(
                        protocol_version=PROTOCOL_VERSION,
                        client_capabilities=ClientCapabilities(),
                        client_info=Implementation(name="CCUID", version=VERSION),
                    )
                    if init.protocol_version != PROTOCOL_VERSION:
                        logger.warning(
                            f"[CCUID/{self.engine.name}] protocol {init.protocol_version} != {PROTOCOL_VERSION}"
                        )
                    if resume_id:
                        try:
                            load_resp = await conn.load_session(cwd=workdir, session_id=resume_id)
                            acp_sid = resume_id
                            models_state = load_resp.models
                        except Exception as load_err:
                            logger.warning(
                                f"[CCUID/{self.engine.name}] load_session 失败，fallback new_session: {load_err}"
                            )
                            new_resp = await conn.new_session(cwd=workdir)
                            acp_sid = new_resp.session_id
                            models_state = new_resp.models
                    else:
                        new_resp = await conn.new_session(cwd=workdir)
                        acp_sid = new_resp.session_id
                        models_state = new_resp.models
                model_id, model_name, available_models = _extract_models(models_state)
                # reapply 失败 / id 已不在 available 时 drop 记录，回 default 不再重试。
                sticky = await CCUIDSessionModel.fetch(sid)
                if sticky is not None and sticky != model_id:
                    sticky_name = next((n for mid, n in available_models if mid == sticky), None)
                    if sticky_name is None:
                        await CCUIDSessionModel.drop(sid)
                    else:
                        try:
                            async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SEC):
                                await conn.set_session_model(model_id=sticky, session_id=acp_sid)
                            model_id, model_name = sticky, sticky_name
                        except Exception as sticky_err:
                            logger.warning(f"[CCUID/{self.engine.name}] sticky {sticky} reapply: {sticky_err}")
                            await CCUIDSessionModel.drop(sid)
            except Exception as e:
                if proc is not None:
                    if proc.returncode is None:
                        with contextlib.suppress(Exception):
                            proc.terminate()
                    record_teardown(proc.pid)
                if stderr_task is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stderr_task, timeout=_STDERR_DRAIN_TIMEOUT)
                self._spawn_failures += 1
                if self._spawn_failures >= _SPAWN_FAIL_THRESHOLD:
                    self._cooldown_until = time.time() + _SPAWN_COOLDOWN_SEC
                    logger.warning(
                        f"[CCUID/{self.engine.name}] 连续 {self._spawn_failures} 次启动失败，熔断 {_SPAWN_COOLDOWN_SEC}s"
                    )
                raise BackendError(f"启动 {self.engine.name} 失败: {e}{format_tail(stderr_tail)}") from e

            self._spawn_failures = 0
            self._cooldown_until = 0.0
            # 保引用，避免 GC 提前回收（Python 3.11+ 会发 RuntimeWarning）。
            watch_task = asyncio.create_task(self._watch_exit(proc, queue))
            self._watch_tasks.add(watch_task)
            watch_task.add_done_callback(self._watch_tasks.discard)
            s = ACPSession(
                proc=proc,
                conn=conn,
                acp_sid=acp_sid,
                queue=queue,
                client=client,
                stderr_tail=stderr_tail,
                model_id=model_id,
                model_name=model_name,
                available_models=available_models,
            )
            self._sess[sid] = s
            return s

    async def _teardown(self, s: ACPSession) -> None:
        with contextlib.suppress(Exception):
            await s.conn.close()
        if s.proc.returncode is None:
            try:
                s.proc.terminate()
                await asyncio.wait_for(s.proc.wait(), timeout=_TERMINATE_TIMEOUT)
            except TimeoutError:
                s.proc.kill()
            except Exception as err:
                logger.warning(f"[CCUID/{self.engine.name}] teardown failed pid={s.proc.pid}: {err!r}")
        record_teardown(s.proc.pid)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process, tail: deque[str]) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    tail.append(line)
                    logger.warning(f"[CCUID/{self.engine.name}] {line}")

    async def _watch_exit(self, proc: asyncio.subprocess.Process, queue: asyncio.Queue[Any]) -> None:
        try:
            await proc.wait()
        finally:
            await queue.put(None)
