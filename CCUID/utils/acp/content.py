from __future__ import annotations

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

from .events import PermissionEvent, PermissionLocation, PermissionOptionView
from .policy import PermissionMode


def summarize_content(
    content: list[ContentToolCallContent | FileEditToolCallContent | TerminalToolCallContent] | None,
) -> str | None:
    """Compress ToolCallUpdate.content into a one-line summary. 不截断——完整文本里
    的换行折成空格就行。"""
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
                parts.append(f"text: {inner.text.replace(chr(10), ' ').strip()}")
            elif isinstance(inner, ImageContentBlock):
                parts.append(f"image ({inner.mime_type})")
            elif isinstance(inner, AudioContentBlock):
                parts.append(f"audio ({inner.mime_type})")
            elif isinstance(inner, ResourceContentBlock):
                parts.append(f"resource link: {inner.uri}")
            elif isinstance(inner, EmbeddedResourceContentBlock):
                parts.append("embedded resource")
            else:
                raise AssertionError(f"unhandled ContentToolCallContent inner: {type(inner).__name__}")
        else:
            raise AssertionError(f"unhandled ToolCallUpdate.content member: {type(item).__name__}")
    return " · ".join(parts)


def build_event(
    decision: PermissionMode,
    tool_call: ToolCallUpdate,
    options: list[PermissionOption],
    matched: bool,
) -> PermissionEvent:
    """Pack a typed ACP request into a frozen PermissionEvent. Single source
    of truth so both auto and ask paths render with the same level of detail."""
    locations = tool_call.locations if tool_call.locations is not None else []
    location_views = tuple(PermissionLocation(path=loc.path, line=loc.line) for loc in locations)
    option_views = tuple(PermissionOptionView(kind=opt.kind, name=opt.name, option_id=opt.option_id) for opt in options)
    return PermissionEvent(
        decision=decision,
        tool_kind=tool_call.kind,
        tool_title=tool_call.title,
        matched=matched,
        locations=location_views,
        content_summary=summarize_content(tool_call.content),
        options=option_views,
    )
