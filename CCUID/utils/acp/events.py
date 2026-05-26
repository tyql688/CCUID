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
    """Surfaces an agent permission request to the user.

    `decision` records the *intent* (whichever PermissionMode was active);
    `matched` records whether the agent actually offered a matching option.
    Remaining fields mirror ACP `RequestPermissionRequest.tool_call` / options
    so the approval card can show full context."""

    decision: PermissionMode
    tool_kind: ToolKind | None
    tool_title: str | None
    matched: bool = True
    locations: tuple[PermissionLocation, ...] = ()
    content_summary: str | None = None
    options: tuple[PermissionOptionView, ...] = ()
