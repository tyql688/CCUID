from __future__ import annotations

from acp.schema import (
    TextContentBlock,
    AudioContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    ContentToolCallContent,
    FileEditToolCallContent,
    TerminalToolCallContent,
    EmbeddedResourceContentBlock,
)


def summarize_tool_content(
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
