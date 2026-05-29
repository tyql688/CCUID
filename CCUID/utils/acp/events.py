from __future__ import annotations

from dataclasses import dataclass

from acp.schema import ToolKind

from .policy import PermissionMode


@dataclass(slots=True, frozen=True)
class PermissionLocation:
    """ACP `ToolCallLocation` mirror — the file/line the agent is about to touch."""

    path: str
    line: int | None


@dataclass(slots=True, frozen=True)
class PermissionOptionView:
    """ACP `PermissionOption` minus the meta noise. `kind` is a
    `acp.schema.PermissionOptionKind` literal (verbatim string)."""

    kind: str
    name: str
    option_id: str


@dataclass(slots=True, frozen=True)
class PermissionEvent:
    """向用户呈现一个 agent 权限请求。

    `decision`=当时生效的 PermissionMode（意图）；`matched`=agent 是否真的给了匹配选项。
    其余字段镜像 ACP `RequestPermissionRequest.tool_call` / options，供审批卡展示完整上下文。"""

    decision: PermissionMode
    tool_kind: ToolKind | None
    tool_title: str | None
    matched: bool = True
    locations: tuple[PermissionLocation, ...] = ()
    content_summary: str | None = None
    options: tuple[PermissionOptionView, ...] = ()
