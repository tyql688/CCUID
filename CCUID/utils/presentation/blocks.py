from __future__ import annotations

from typing import Literal
from dataclasses import field, dataclass
from urllib.parse import unquote, urlsplit

from acp.schema import (
    PlanEntry,
    UsageUpdate,
    ToolCallStart,
    PlanUpdateFile,
    AgentPlanUpdate,
    PlanUpdateItems,
    TextContentBlock,
    ToolCallProgress,
    UserMessageChunk,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    CurrentModeUpdate,
    ImageContentBlock,
    SessionInfoUpdate,
    ConfigOptionUpdate,
    PlanUpdateMarkdown,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AvailableCommandsUpdate,
)

from gsuid_core.logger import logger

from ..render import ChatBlock, PlanBlock, ToolBlock, MediaBlock, PermissionBlock
from .permission import permission_display, clean_permission_summary
from ..acp.backend import PromptUsage
from .tool_content import summarize_tool_content
from ..acp.permission import PermissionEvent

# 故意不消费的事件，静默 drop 不计入"陌生类型"告警；有显式 return None 分支的不用登记，
# ToolCallProgress 有条件分支会落穿，故仍需登记
_KNOWN_UNUSED_EVENTS: tuple[type, ...] = (
    AgentPlanUpdate,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    SessionInfoUpdate,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    ToolCallProgress,
    UserMessageChunk,
)
_warned_event_types: set[str] = set()

StreamKind = Literal["agent", "think"]
StreamFragment = tuple[StreamKind, str, str | None]
ToolDisplayMode = Literal["off", "brief", "full"]


@dataclass(slots=True)
class _RenderBuffer:
    pending: list[ChatBlock] = field(default_factory=list)
    stream_chunks: list[str] = field(default_factory=list)
    stream_kind: StreamKind | None = None
    stream_message_id: str | None = None
    tool_history: dict[str, ToolBlock] = field(default_factory=dict)
    delivered_plan_ids: set[str | None] = field(default_factory=set)

    def flush_streams(self) -> None:
        text = "".join(self.stream_chunks).strip()
        if text:
            block_kind = "agent_md" if self.stream_kind == "agent" else "think"
            self.pending.append(ChatBlock(block_kind, text))
        self.stream_chunks.clear()
        self.stream_kind = None
        self.stream_message_id = None

    def append_fragment(self, kind: StreamKind, text: str, message_id: str | None) -> None:
        if self.stream_chunks and (self.stream_kind != kind or self.stream_message_id != message_id):
            self.flush_streams()
        if not self.stream_chunks:
            self.stream_kind = kind
            self.stream_message_id = message_id
        self.stream_chunks.append(text)

    def append_block(self, block: ChatBlock) -> None:
        self.flush_streams()
        if isinstance(block, ToolBlock):
            block = _merge_with_tool_history(self.tool_history, block)
        if isinstance(block, PlanBlock):
            self._apply_plan(block)
            return
        _append_or_replace_tool(self.pending, block)

    def _apply_plan(self, block: PlanBlock) -> None:
        if block.kind == "plan":
            _append_or_replace_plan(self.pending, block)
            return
        removed_delivered = block.plan_id in self.delivered_plan_ids
        _remove_plan(self.pending, block.plan_id)
        self.delivered_plan_ids.discard(block.plan_id)
        if removed_delivered:
            self.pending.append(block)

    def pop_pending(self) -> list[ChatBlock]:
        self.flush_streams()
        blocks = self.pending
        for block in blocks:
            if isinstance(block, PlanBlock) and block.kind == "plan":
                self.delivered_plan_ids.add(block.plan_id)
        self.pending = []
        return blocks


def _chunk_text(c: object) -> str:
    return c.text if isinstance(c, TextContentBlock) else ""


def _fmt_plan_entries(entries: list[PlanEntry]) -> str:
    rows = [f"- {entry.status} · {entry.priority}: {entry.content}" for entry in entries]
    return "\n".join(rows)


def _fmt_plan_content(ev: AgentPlanContentUpdate) -> PlanBlock:
    plan = ev.plan
    if isinstance(plan, PlanUpdateItems):
        body = f"**Plan `{plan.plan_id}`:**\n" + _fmt_plan_entries(plan.entries)
    elif isinstance(plan, PlanUpdateMarkdown):
        body = f"**Plan `{plan.plan_id}`:**\n\n{plan.content}"
    elif isinstance(plan, PlanUpdateFile):
        parsed = urlsplit(plan.uri)
        if parsed.scheme == "file":
            location = unquote(parsed.path)
        elif parsed.scheme:
            location = f"<{plan.uri}>"
        else:
            location = f"`{plan.uri}`"
        body = f"**Plan `{plan.plan_id}` file:** {location}"
    else:
        raise TypeError(f"unsupported ACP plan update: {type(plan).__name__}")
    return PlanBlock("plan", body, plan_id=plan.plan_id)


def _permission_block(ev: PermissionEvent) -> PermissionBlock:
    return PermissionBlock(
        "permission",
        "",
        decision=ev.decision,
        tool_kind=ev.tool_call.kind,
        title=ev.tool_call.title,
        matched=ev.matched,
        locations=tuple(ev.tool_call.locations) if ev.tool_call.locations is not None else (),
        content_summary=summarize_tool_content(ev.tool_call.content),
        options=ev.options,
    )


def _stripped(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return None if text == "" else text


def _tool_title(value: str | None, kind: str) -> str | None:
    title = _stripped(value)
    if title is None or (kind == "other" and title.lower() == "other"):
        return None
    return title


def _compose_tool_body(title: str | None, summary: str | None) -> str:
    return "\n".join(part for part in (title, summary) if part)


def _merge_tool_block(previous: ToolBlock, block: ToolBlock) -> ToolBlock:
    kind = previous.tool_kind if block.tool_kind == "other" else block.tool_kind
    title = _stripped(block.title)
    if title is None:
        title = _stripped(previous.title)
    summary = _stripped(block.summary)
    body = _compose_tool_body(title, summary)
    if body == "":
        body = block.body
    if body == "":
        body = previous.body
    return ToolBlock("tool", body, tool_kind=kind, tool_call_id=previous.tool_call_id, title=title, summary=summary)


def _merge_with_tool_history(history: dict[str, ToolBlock], block: ToolBlock) -> ToolBlock:
    tid = block.tool_call_id
    if not tid:
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
            return MediaBlock("agent_image", "", data=ev.content.data, mime_type=ev.content.mime_type)
        if isinstance(ev.content, AudioContentBlock):
            return MediaBlock("agent_audio", "", data=ev.content.data, mime_type=ev.content.mime_type)
        text = _chunk_text(ev.content)
        return ("agent", text, ev.message_id) if text else None
    if isinstance(ev, AgentThoughtChunk) and show_thinking:
        text = _chunk_text(ev.content)
        return ("think", text, ev.message_id) if text else None
    if isinstance(ev, ToolCallStart) and show_tools:
        kind = ev.kind if ev.kind is not None else "other"
        title = _tool_title(ev.title, kind)
        summary = summarize_tool_content(ev.content)
        body = _compose_tool_body(title, summary)
        if not body and kind == "other":
            return None
        return ToolBlock(
            "tool",
            body if body != "" else kind,
            tool_kind=kind,
            tool_call_id=ev.tool_call_id,
            title=title,
            summary=summary,
        )
    if isinstance(ev, ToolCallProgress) and show_tools:
        if ev.status == "failed":
            if isinstance(ev.raw_output, dict) and "error" in ev.raw_output:
                return ChatBlock("tool_failed", str(ev.raw_output["error"]))
            return ChatBlock("tool_failed", "failed")
        if tool_display == "full" and ev.content:
            summary = summarize_tool_content(ev.content)
            if summary:
                kind = ev.kind if ev.kind is not None else "other"
                title = _tool_title(ev.title, kind)
                return ToolBlock(
                    "tool",
                    _compose_tool_body(title, summary),
                    tool_kind=kind,
                    tool_call_id=ev.tool_call_id,
                    title=title,
                    summary=summary,
                )
    if isinstance(ev, AgentPlanUpdate) and show_tools:
        return PlanBlock("plan", "**Plan:**\n" + _fmt_plan_entries(ev.entries), plan_id=None)
    if isinstance(ev, AgentPlanContentUpdate) and show_tools:
        return _fmt_plan_content(ev)
    if isinstance(ev, AgentPlanRemovedUpdate) and show_tools:
        return PlanBlock("plan_removed", f"**Plan removed:** `{ev.plan_id}`", plan_id=ev.plan_id)
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
    if isinstance(block, ToolBlock) and block.tool_call_id:
        for i in range(len(buf) - 1, -1, -1):
            b = buf[i]
            if isinstance(b, ToolBlock) and b.tool_call_id == block.tool_call_id:
                buf[i] = _merge_tool_block(b, block)
                return
    buf.append(block)


def _is_plan(block: ChatBlock, plan_id: str | None) -> bool:
    return isinstance(block, PlanBlock) and block.kind == "plan" and block.plan_id == plan_id


def _append_or_replace_plan(buf: list[ChatBlock], block: PlanBlock) -> None:
    for index in range(len(buf) - 1, -1, -1):
        if _is_plan(buf[index], block.plan_id):
            buf[index] = block
            return
    buf.append(block)


def _remove_plan(buf: list[ChatBlock], plan_id: str | None) -> None:
    buf[:] = [block for block in buf if not _is_plan(block, plan_id)]


def blocks_to_text_parts(blocks: list[ChatBlock]) -> list[str]:
    """Flatten blocks into discrete text strings for the text/forward path."""
    out: list[str] = []
    for block in blocks:
        if isinstance(block, ToolBlock):
            kind = block.tool_kind
            out.append(block.body if kind == "other" else f"{kind}: {block.body}")
        elif isinstance(block, PermissionBlock):
            out.append(_permission_text(block))
        elif block.kind == "agent_md":
            out.append(block.body)
        elif block.kind == "think":
            out.append(f"think: {block.body}")
        elif block.kind == "tool_failed":
            out.append(f"tool failed: {block.body}")
        elif block.kind in {"plan", "plan_removed"}:
            out.append(block.body)
        elif block.kind == "mode":
            out.append(f"mode: {block.body}")
        elif block.kind in {"error", "usage_footer"}:
            out.append(block.body)
    return out


def _permission_text(block: PermissionBlock) -> str:
    content_summary = clean_permission_summary(block.content_summary)
    display = permission_display(block.decision, matched=block.matched)
    parts = [display.label]
    if block.tool_kind is not None:
        parts.append(f"[{block.tool_kind}]")
    line = " · ".join(parts)
    extras: list[str] = []
    if block.title is not None:
        extras.append(f"操作：{block.title}")
    if display.unmatched_text is not None:
        extras.append(f"结果：{display.unmatched_text}")
    if block.locations:
        extras.append(
            "位置："
            + ", ".join(f"{loc.path}{f':{loc.line}' if loc.line is not None else ''}" for loc in block.locations)
        )
    if content_summary is not None:
        extras.append(f"原因：{content_summary}")
    if extras:
        line += "\n" + "\n".join(extras)
    return line


def _block_render_size(block: ChatBlock) -> int:
    """估算 block 渲染后字符数，`_should_image` 跟阈值比较。"""
    if not isinstance(block, PermissionBlock):
        return len(block.body)
    size = 0
    if block.title is not None:
        size += len(block.title)
    summary = clean_permission_summary(block.content_summary)
    if summary is not None:
        size += len(summary)
    for loc in block.locations:
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
