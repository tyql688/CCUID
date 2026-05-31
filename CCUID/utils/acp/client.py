from __future__ import annotations

import asyncio
from typing import NoReturn, Protocol

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
    RequestPermissionRequest,
    WaitForTerminalExitResponse,
)

from .permission import PermissionMode, build_event, decide_auto
from .tool_state import ToolCallState
from ...cc_config.cc_config import CCUIDConfig


def _disabled(feature: str) -> NoReturn:
    raise NotImplementedError(f"CCUID client {feature} capability is disabled")


class PermissionApprovalStore(Protocol):
    def register_pending(
        self,
        sid: str,
        request: RequestPermissionRequest,
    ) -> asyncio.Future[str | None]: ...

    def cancel_pending(self, sid: str, future: asyncio.Future[str | None]) -> None: ...


class ACPClient(Client):
    """One ACPClient per ACPSession. 持有：
    - inbound event queue (agent → us)
    - per-session sid (ASK 模式时给 SessionRegistry 注册 pending future 用)"""

    def __init__(self, queue: asyncio.Queue[object], sid: str, approvals: PermissionApprovalStore) -> None:
        self._queue = queue
        self._sid = sid
        self._approvals = approvals
        self._tool_state = ToolCallState()

    async def session_update(self, session_id: str, update: object, **_: object) -> None:
        await self._queue.put(self._tool_state.normalize(update))

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **_: object,
    ) -> RequestPermissionResponse:
        """按 PermissionMode 分流：`ask` 挂 future 等用户审批，自动模式直接选对应 kind 的 PermissionOption。"""
        tool_call = self._tool_state.normalize_update(tool_call)
        policy: PermissionMode = CCUIDConfig.get_config("PermissionPolicy").data
        if policy == "ask":
            return await self._ask(session_id, options, tool_call)
        decision = decide_auto(options, policy)
        await self._queue.put(build_event(decision.decision, session_id, tool_call, options, decision.matched))
        return decision.response

    async def _ask(
        self,
        session_id: str,
        options: list[PermissionOption],
        tool_call: ToolCallUpdate,
    ) -> RequestPermissionResponse:
        """`try/finally` 必须包 `cancel_pending`：否则 CancelledError（restart/LRU evict/shutdown）传播时
        future 会永远留在 _pending 泄漏。cancel_pending 按 identity 找、幂等，take_pending 已 pop 也安全。
        """
        event = build_event("ask", session_id, tool_call, options, matched=True)
        await self._queue.put(event)
        future = self._approvals.register_pending(self._sid, event)
        timeout = int(CCUIDConfig.get_config("PromptApproveTimeoutSec").data)
        try:
            option_id = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            self._approvals.cancel_pending(self._sid, future)
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
        **_: object,
    ) -> WriteTextFileResponse | None:
        _disabled("fs/write_text_file")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **_: object,
    ) -> ReadTextFileResponse:
        _disabled("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **_: object,
    ) -> CreateTerminalResponse:
        _disabled("terminal")

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **_: object,
    ) -> TerminalOutputResponse:
        _disabled("terminal")

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_: object,
    ) -> ReleaseTerminalResponse | None:
        _disabled("terminal")

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **_: object,
    ) -> WaitForTerminalExitResponse:
        _disabled("terminal")

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **_: object,
    ) -> KillTerminalResponse | None:
        _disabled("terminal")

    async def ext_method(self, method: str, params: dict[str, object]) -> dict[str, object]:
        _disabled(f"extension method: {method}")

    async def ext_notification(self, method: str, params: dict[str, object]) -> None:
        _disabled(f"extension notification: {method}")

    def on_connect(self, conn: Agent) -> None:
        return None
