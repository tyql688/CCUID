from __future__ import annotations

import base64
from typing import Literal
from dataclasses import dataclass

from acp.schema import (
    ToolCallUpdate,
    PermissionOption,
    TextContentBlock,
    AudioContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    ContentToolCallContent,
    FileEditToolCallContent,
    TerminalToolCallContent,
    EmbeddedResourceContentBlock,
)

from .events import PermissionEvent
from .policy import PermissionMode

PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


@dataclass(slots=True, frozen=True, kw_only=True)
class PermissionDisplay:
    label: str
    state: Literal["pending", "allowed", "denied"]
    unmatched_text: str | None = None


def text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def image_block(raw: bytes, mime_type: str) -> ImageContentBlock:
    return ImageContentBlock(type="image", data=base64.b64encode(raw).decode("ascii"), mime_type=mime_type)


def clean_permission_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    text = summary.strip()
    if text.startswith("text: "):
        text = text[6:].strip()
    if text.startswith("Not in allowlist:"):
        detail = text.removeprefix("Not in allowlist:").strip()
        text = f"不在允许列表：{detail}" if detail else "不在允许列表"
    return text


def permission_display(decision: PermissionMode, *, matched: bool) -> PermissionDisplay:
    if decision == "ask":
        return PermissionDisplay(label="待审核", state="pending")
    if not matched:
        return PermissionDisplay(label="已取消", state="denied", unmatched_text=f"策略 {decision} 没有匹配选项，已取消")
    if decision == "allow_once":
        return PermissionDisplay(label="已自动允许", state="allowed")
    if decision == "allow_always":
        return PermissionDisplay(label="已自动永久允许", state="allowed")
    if decision == "reject_once":
        return PermissionDisplay(label="已自动拒绝", state="denied")
    raise ValueError(f"unhandled PermissionMode: {decision!r}")


def summarize_content(
    content: list[ContentToolCallContent | FileEditToolCallContent | TerminalToolCallContent] | None,
) -> str | None:
    """Convert ToolCallUpdate.content into a display summary without dropping user-visible text."""
    if not content:
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, FileEditToolCallContent):
            old_lines = item.old_text.count("\n") + 1 if item.old_text else 0
            new_lines = item.new_text.count("\n") + 1
            parts.append(f"diff {item.path} -{old_lines}/+{new_lines}")
        elif isinstance(item, TerminalToolCallContent):
            parts.append(f"terminal #{item.terminal_id}")
        elif isinstance(item, ContentToolCallContent):
            inner = item.content
            if isinstance(inner, TextContentBlock):
                parts.append(inner.text.strip())
            elif isinstance(inner, ImageContentBlock):
                parts.append(f"image ({inner.mime_type})")
            elif isinstance(inner, AudioContentBlock):
                parts.append(f"audio ({inner.mime_type})")
            elif isinstance(inner, ResourceContentBlock):
                parts.append(f"resource link: {inner.uri}")
            elif isinstance(inner, EmbeddedResourceContentBlock):
                parts.append("embedded resource")
            else:
                raise TypeError(f"unhandled ContentToolCallContent inner: {type(inner).__name__}")
        else:
            raise TypeError(f"unhandled ToolCallUpdate.content member: {type(item).__name__}")
    return "\n\n".join(parts)


def build_event(
    decision: PermissionMode,
    session_id: str,
    tool_call: ToolCallUpdate,
    options: list[PermissionOption],
    matched: bool,
) -> PermissionEvent:
    """Pack a typed ACP request into PermissionEvent. Single source
    of truth so both auto and ask paths render with the same level of detail."""
    return PermissionEvent(
        decision=decision,
        session_id=session_id,
        tool_call=tool_call,
        matched=matched,
        options=options,
    )
