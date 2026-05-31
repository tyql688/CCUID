from __future__ import annotations

from typing import Literal
from dataclasses import dataclass

from acp.schema import (
    DeniedOutcome,
    AllowedOutcome,
    ToolCallUpdate,
    PermissionOption,
    RequestPermissionRequest,
    RequestPermissionResponse,
)

# `ask` 是 CCUID 内部值（挂起 RPC 等用户审批）；其余三个 1:1 映射 ACP PermissionOptionKind。
# `reject_always` 没列入：实测 adapter 从不下发，列了反而让用户选了走 cancelled 混淆
PermissionMode = Literal["ask", "allow_once", "allow_always", "reject_once"]


@dataclass(slots=True, frozen=True)
class AutoDecision:
    response: RequestPermissionResponse
    decision: PermissionMode
    matched: bool


class PermissionEvent(RequestPermissionRequest):
    """向用户呈现一个 agent 权限请求。

    `decision`=当时生效的 PermissionMode（意图）；`matched`=agent 是否真的给了匹配选项。
    其余字段直接继承 ACP `RequestPermissionRequest`。
    """

    decision: PermissionMode
    matched: bool = True


def decide_auto(options: list[PermissionOption], policy: PermissionMode) -> AutoDecision:
    """Resolve a non-`ask` policy by finding the matching PermissionOption。
    agent 没下发对应 kind 时返回 cancelled——不编造、不挑相近。"""
    if policy == "ask":
        raise ValueError("decide_auto must not be called for ask")
    for opt in options:
        if opt.kind == policy:
            return AutoDecision(
                RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=opt.option_id)),
                policy,
                matched=True,
            )
    return AutoDecision(
        RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")),
        policy,
        matched=False,
    )


def build_event(
    decision: PermissionMode,
    session_id: str,
    tool_call: ToolCallUpdate,
    options: list[PermissionOption],
    matched: bool,
) -> PermissionEvent:
    """Pack a typed ACP request into PermissionEvent."""
    return PermissionEvent(
        decision=decision,
        session_id=session_id,
        tool_call=tool_call,
        matched=matched,
        options=options,
    )
