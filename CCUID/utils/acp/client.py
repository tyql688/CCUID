from __future__ import annotations

import asyncio
from typing import Any

from acp import Agent, Client, RequestPermissionResponse
from acp.schema import (
    EnvVariable,
    DeniedOutcome,
    AllowedOutcome,
    ToolCallUpdate,
    PermissionOption,
    KillTerminalResponse,
    ReadTextFileResponse,
    WriteTextFileResponse,
    CreateTerminalResponse,
    TerminalOutputResponse,
    ReleaseTerminalResponse,
    WaitForTerminalExitResponse,
)

from .policy import PermissionMode, decide_auto
from .content import build_event
from ...cc_config.cc_config import CCUIDConfig


class ACPClient(Client):
    """One ACPClient per ACPSession. 持有：
    - inbound event queue (agent → us)
    - per-session sid (ASK 模式时给 SessionRegistry 注册 pending future 用)"""

    def __init__(self, queue: asyncio.Queue[Any], sid: str) -> None:
        self._queue = queue
        self._sid = sid
        # `cc 下次允许` 的一次性 yolo flag。backend.prompt() 进入前 set；
        # request_permission 看这个值决定走默认 policy 还是无脑 allow_always。
        self.auto_approve_this_prompt: bool = False

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        await self._queue.put(update)

    async def request_permission(
        self,
        options: list[PermissionOption],
        *,
        session_id: str,
        tool_call: ToolCallUpdate,
        **_: Any,
    ) -> RequestPermissionResponse:
        """Branch on PermissionMode. `ask` 时挂 future 等用户审批；自动模式直
        接选 agent 提供的对应 PermissionOption.kind。

        `auto_approve_this_prompt`=True 时无视 config，本轮 prompt 期间所有
        permission 都按 allow_always 走（`cc 下次允许` 的一次性 yolo）。"""
        if self.auto_approve_this_prompt:
            policy: PermissionMode = "allow_always"
        else:
            policy = CCUIDConfig.get_config("PermissionPolicy").data
        if policy == "ask":
            return await self._ask(options, tool_call)
        decision = decide_auto(options, policy)
        await self._queue.put(build_event(decision.decision, tool_call, options, decision.matched))
        return decision.response

    async def _ask(
        self,
        options: list[PermissionOption],
        tool_call: ToolCallUpdate,
    ) -> RequestPermissionResponse:
        """Lazy import SessionRegistry — `session.py` imports the acp package
        for ACPBackend, so importing it at module load would cycle.

        `try/finally` 必须包 `cancel_pending`：原先只 catch TimeoutError 会让
        CancelledError（session restart / LRU evict / shutdown 触发）直接传播，
        future 永远留在 REGISTRY._pending 里造成泄漏。cancel_pending 内部 by
        identity 找 future，幂等——take_pending 已 pop 的情况下也安全。
        """
        from ..session import REGISTRY

        await self._queue.put(build_event("ask", tool_call, options, matched=True))
        future = REGISTRY.register_pending(self._sid, options, tool_call.kind, tool_call.title)
        timeout = int(CCUIDConfig.get_config("PromptApproveTimeoutSec").data)
        try:
            option_id = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            REGISTRY.cancel_pending(self._sid, future)
        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id),
        )

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **_: Any,
    ) -> WriteTextFileResponse | None:
        raise NotImplementedError("CCUID client fs/write_text_file capability is disabled")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **_: Any,
    ) -> ReadTextFileResponse:
        raise NotImplementedError("CCUID client fs/read_text_file capability is disabled")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **_: Any,
    ) -> CreateTerminalResponse:
        raise NotImplementedError("CCUID client terminal capability is disabled")

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **_: Any,
    ) -> TerminalOutputResponse:
        raise NotImplementedError("CCUID client terminal capability is disabled")

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_: Any,
    ) -> ReleaseTerminalResponse | None:
        raise NotImplementedError("CCUID client terminal capability is disabled")

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **_: Any,
    ) -> WaitForTerminalExitResponse:
        raise NotImplementedError("CCUID client terminal capability is disabled")

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_: Any,
    ) -> KillTerminalResponse | None:
        raise NotImplementedError("CCUID client terminal capability is disabled")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(f"CCUID client extension method is disabled: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise NotImplementedError(f"CCUID client extension notification is disabled: {method}")

    def on_connect(self, conn: Agent) -> None:
        return None
