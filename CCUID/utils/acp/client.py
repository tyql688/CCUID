from __future__ import annotations

import asyncio
from typing import Any

from acp import RequestPermissionResponse
from acp.schema import (
    DeniedOutcome,
    AllowedOutcome,
    ToolCallUpdate,
    PermissionOption,
)

from .policy import PermissionMode, decide_auto
from .content import build_event
from ...cc_config.cc_config import CCUIDConfig


class ACPClient:
    """One ACPClient per ACPSession. 持有：
    - inbound event queue (agent → us)
    - per-session sid (ASK 模式时给 SessionRegistry 注册 pending future 用)"""

    def __init__(self, queue: asyncio.Queue[Any], sid: str) -> None:
        self._queue = queue
        self._sid = sid
        # `cc 下次允许` 的一次性 yolo flag。backend.prompt() 进入前 set；
        # request_permission 看这个值决定走默认 policy 还是无脑 allow_always。
        self.auto_approve_this_prompt: bool = False

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:  # noqa: ARG002
        await self._queue.put(update)

    async def request_permission(
        self,
        options: list[PermissionOption],
        *,
        session_id: str,  # noqa: ARG002 — required by ACP Client protocol
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
        for ACPBackend, so importing it at module load would cycle."""
        from ..session import REGISTRY

        await self._queue.put(build_event("ask", tool_call, options, matched=True))
        future = REGISTRY.register_pending(self._sid, options, tool_call.kind, tool_call.title)
        timeout = int(CCUIDConfig.get_config("PromptApproveTimeoutSec").data)
        try:
            option_id = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            REGISTRY.cancel_pending(self._sid, future)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id),
        )
