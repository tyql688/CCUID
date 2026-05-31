from __future__ import annotations

from typing import Literal
from dataclasses import field, dataclass

from acp.schema import (
    UsageUpdate,
    ToolCallStart,
    AgentPlanUpdate,
    TextContentBlock,
    ToolCallProgress,
    UserMessageChunk,
    AgentMessageChunk,
    AgentThoughtChunk,
    CurrentModeUpdate,
    ImageContentBlock,
    SessionInfoUpdate,
    ConfigOptionUpdate,
    AvailableCommandsUpdate,
)

from gsuid_core.logger import logger

from ..render import ChatBlock
from .permission import permission_display, clean_permission_summary
from ..acp.backend import PromptUsage
from .tool_content import summarize_tool_content
from ..acp.permission import PermissionEvent

# 故意不消费的事件：落这里静默 drop，不计入"陌生类型"告警。
# UsageUpdate / AgentThoughtChunk 有显式 return None 分支，不进此表；
# ToolCallProgress 有条件分支但会落穿，故仍需登记。
_KNOWN_UNUSED_EVENTS: tuple[type, ...] = (
    SessionInfoUpdate,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    ToolCallProgress,
    UserMessageChunk,
)
_warned_event_types: set[str] = set()

StreamKind = Literal["agent", "think"]
StreamFragment = tuple[StreamKind, str]
ToolDisplayMode = Literal["off", "brief", "full"]


@dataclass(slots=True)
class _RenderBuffer:
    pending: list[ChatBlock] = field(default_factory=list)
    agent_chunks: list[str] = field(default_factory=list)
    thought_chunks: list[str] = field(default_factory=list)
    tool_history: dict[str, ChatBlock] = field(default_factory=dict)

    def _flush_agent(self) -> None:
        text = "".join(self.agent_chunks).strip()
        if text:
            self.pending.append(ChatBlock("agent_md", text))
        self.agent_chunks.clear()

    def _flush_thought(self) -> None:
        text = "".join(self.thought_chunks).strip()
        if text:
            self.pending.append(ChatBlock("think", text))
        self.thought_chunks.clear()

    def flush_streams(self) -> None:
        self._flush_thought()
        self._flush_agent()

    def append_fragment(self, kind: StreamKind, text: str) -> None:
        if kind == "agent":
            self._flush_thought()
            self.agent_chunks.append(text)
            return
        self._flush_agent()
        self.thought_chunks.append(text)

    def append_block(self, block: ChatBlock) -> None:
        self.flush_streams()
        block = _merge_with_tool_history(self.tool_history, block)
        _append_or_replace_tool(self.pending, block)

    def pop_pending(self) -> list[ChatBlock]:
        self.flush_streams()
        blocks = self.pending
        self.pending = []
        return blocks


def _chunk_text(c: object) -> str:
    return c.text if isinstance(c, TextContentBlock) else ""


def _fmt_plan(ev: AgentPlanUpdate) -> str:
    rows = [f"- {e.status}: {e.content}" for e in ev.entries]
    return "**Plan:**\n" + "\n".join(rows)


def _permission_block(ev: PermissionEvent) -> ChatBlock:
    return ChatBlock(
        "permission",
        body="",
        meta={
            "decision": ev.decision,
            "kind": ev.tool_call.kind,
            "title": ev.tool_call.title,
            "matched": ev.matched,
            "locations": tuple(ev.tool_call.locations) if ev.tool_call.locations is not None else (),
            "content_summary": summarize_tool_content(ev.tool_call.content),
            "options": ev.options,
        },
    )


def _tool_meta_text(block: ChatBlock, key: str) -> str | None:
    value = block.meta.get(key)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _tool_title(value: str | None, kind: str) -> str | None:
    if not isinstance(value, str):
        return None
    title = value.strip()
    if not title or (kind == "other" and title.lower() == "other"):
        return None
    return title


def _compose_tool_body(title: str | None, summary: str | None) -> str:
    return "\n".join(part for part in (title, summary) if part)


def _tool_call_id(block: ChatBlock) -> str | None:
    value = block.meta.get("tool_call_id")
    return value if isinstance(value, str) and value else None


def _merge_tool_block(previous: ChatBlock, block: ChatBlock) -> ChatBlock:
    old_kind = previous.meta.get("kind")
    new_kind = block.meta.get("kind")
    kind = old_kind if new_kind == "other" and isinstance(old_kind, str) else new_kind
    if not isinstance(kind, str):
        kind = "other"
    title = _tool_meta_text(block, "title") or _tool_meta_text(previous, "title")
    summary = _tool_meta_text(block, "summary")
    body = _compose_tool_body(title, summary) or block.body or previous.body
    meta = {**previous.meta, **block.meta, "kind": kind, "title": title, "summary": summary}
    return ChatBlock("tool", body, meta=meta)


def _merge_with_tool_history(history: dict[str, ChatBlock], block: ChatBlock) -> ChatBlock:
    tid = _tool_call_id(block) if block.kind == "tool" else None
    if tid is None:
        return block
    previous = history.get(tid)
    merged = _merge_tool_block(previous, block) if previous is not None else block
    history[tid] = merged
    return merged


def _classify(
    ev: object,
    show_thinking: bool,
    tool_display: ToolDisplayMode,
) -> ChatBlock | StreamFragment | None:
    """Single source of truth for ACP-event → CCUID-output mapping."""
    show_tools = tool_display != "off"
    if isinstance(ev, AgentMessageChunk):
        if isinstance(ev.content, ImageContentBlock):
            return ChatBlock("agent_image", "", meta={"data": ev.content.data, "mime_type": ev.content.mime_type})
        text = _chunk_text(ev.content)
        return ("agent", text) if text else None
    if isinstance(ev, AgentThoughtChunk) and show_thinking:
        text = _chunk_text(ev.content)
        return ("think", text) if text else None
    if isinstance(ev, ToolCallStart) and show_tools:
        kind = ev.kind if ev.kind is not None else "other"
        title = _tool_title(ev.title, kind)
        summary = summarize_tool_content(ev.content)
        body = _compose_tool_body(title, summary)
        if not body and kind == "other":
            return None
        return ChatBlock(
            "tool",
            body or kind,
            meta={"kind": kind, "tool_call_id": ev.tool_call_id, "title": title, "summary": summary},
        )
    if isinstance(ev, ToolCallProgress) and show_tools:
        if ev.status == "failed":
            err = ev.raw_output.get("error") if isinstance(ev.raw_output, dict) else None
            return ChatBlock("tool_failed", str(err) if err else "failed")
        if tool_display == "full" and ev.content:
            summary = summarize_tool_content(ev.content)
            if summary:
                kind = ev.kind if ev.kind is not None else "other"
                title = _tool_title(ev.title, kind)
                return ChatBlock(
                    "tool",
                    _compose_tool_body(title, summary),
                    meta={"kind": kind, "tool_call_id": ev.tool_call_id, "title": title, "summary": summary},
                )
    if isinstance(ev, AgentPlanUpdate) and show_tools:
        return ChatBlock("plan", _fmt_plan(ev))
    if isinstance(ev, CurrentModeUpdate) and show_tools:
        return ChatBlock("mode", ev.current_mode_id)
    if isinstance(ev, PermissionEvent):
        return _permission_block(ev)
    if isinstance(ev, UsageUpdate):
        return None
    if isinstance(ev, AgentThoughtChunk):
        return None
    if not isinstance(ev, _KNOWN_UNUSED_EVENTS):
        name = type(ev).__name__
        if name not in _warned_event_types:
            _warned_event_types.add(name)
            logger.debug(f"[CCUID] unhandled ACP event type: {name}")
    return None


def _append_or_replace_tool(buf: list[ChatBlock], block: ChatBlock) -> None:
    """Replace duplicate updates for the same ACP toolCallId."""
    tid = _tool_call_id(block) if block.kind == "tool" else None
    if tid is not None:
        for i in range(len(buf) - 1, -1, -1):
            b = buf[i]
            if b.kind == "tool" and b.meta.get("tool_call_id") == tid:
                buf[i] = _merge_tool_block(b, block)
                return
    buf.append(block)


def blocks_to_text_parts(blocks: list[ChatBlock]) -> list[str]:
    """Flatten blocks into discrete text strings for the text/forward path."""
    out: list[str] = []
    for block in blocks:
        if block.kind == "agent_md":
            out.append(block.body)
        elif block.kind == "think":
            out.append(f"think: {block.body}")
        elif block.kind == "tool":
            kind = block.meta["kind"]
            out.append(block.body if kind == "other" else f"{kind}: {block.body}")
        elif block.kind == "tool_failed":
            out.append(f"tool failed: {block.body}")
        elif block.kind == "plan":
            out.append(block.body)
        elif block.kind == "mode":
            out.append(f"mode: {block.body}")
        elif block.kind == "error":
            out.append(block.body)
        elif block.kind == "usage_footer":
            out.append(block.body)
        elif block.kind == "permission":
            decision = block.meta["decision"]
            tool_kind = block.meta["kind"]
            tool_title = block.meta["title"]
            matched = block.meta["matched"]
            locations = block.meta["locations"]
            content_summary = clean_permission_summary(block.meta["content_summary"])
            display = permission_display(decision, matched=matched)
            parts = [display.label]
            if tool_kind is not None:
                parts.append(f"[{tool_kind}]")
            line = " · ".join(parts)
            extras: list[str] = []
            if tool_title is not None:
                extras.append(f"操作：{tool_title}")
            if display.unmatched_text is not None:
                extras.append(f"结果：{display.unmatched_text}")
            if locations:
                extras.append(
                    "位置："
                    + ", ".join(f"{loc.path}{f':{loc.line}' if loc.line is not None else ''}" for loc in locations)
                )
            if content_summary is not None:
                extras.append(f"原因：{content_summary}")
            if extras:
                line += "\n" + "\n".join(extras)
            out.append(line)
    return out


def _block_render_size(block: ChatBlock) -> int:
    """估算 block 渲染后字符数，`_should_image` 跟阈值比较。"""
    if block.kind != "permission":
        return len(block.body)
    size = 0
    title = block.meta["title"]
    if title is not None:
        size += len(title)
    summary = clean_permission_summary(block.meta["content_summary"])
    if summary is not None:
        size += len(summary)
    for loc in block.meta["locations"]:
        size += len(loc.path) + 8
    return size


def _format_usage_footer(usage: PromptUsage) -> str:
    value = usage.usage
    if value is None:
        return ""
    parts: list[str] = []
    parts.append(f"input {value.input_tokens}")
    parts.append(f"output {value.output_tokens}")
    if value.cached_read_tokens is not None:
        parts.append(f"cached_read {value.cached_read_tokens}")
    if value.cached_write_tokens is not None:
        parts.append(f"cached_write {value.cached_write_tokens}")
    return " · ".join(parts)
