import re
from typing import Any, Literal
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Callable, AsyncIterator

from acp.schema import (
    UsageUpdate,
    ToolCallStart,
    PromptResponse,
    AgentPlanUpdate,
    TextContentBlock,
    ToolCallProgress,
    UserMessageChunk,
    AgentMessageChunk,
    AgentThoughtChunk,
    CurrentModeUpdate,
    ImageContentBlock,
    SessionInfoUpdate,
    AvailableCommandsUpdate,
)

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button

from .render import (
    ChatBlock,
    ImageContext,
    render_to_png,
    build_markdown,
    engine_icon_url,
    clean_permission_summary,
)
from .engines import get_engine
from .acp.events import PermissionEvent, PermissionOptionView
from .acp.backend import PromptUsage, BackendError
from ..cc_config.cc_config import CCUIDConfig

_AUTO_IMAGE_THRESHOLD = 600
_IMAGE_MAX_WIDTH = 720

# 故意不消费的事件：落这里静默 drop，不计入"陌生类型"告警。
# UsageUpdate / AgentThoughtChunk 有显式 return None 分支，不进此表；
# ToolCallProgress 有条件分支但会落穿，故仍需登记。
_KNOWN_UNUSED_EVENTS: tuple[type, ...] = (
    SessionInfoUpdate,
    AvailableCommandsUpdate,
    ToolCallProgress,
    UserMessageChunk,
)
_warned_event_types: set[str] = set()


# 流式 chunk → fragment，render() 累积成单个 ChatBlock，否则 token-level 切片渲成几十条独立行
StreamKind = Literal["agent", "think"]
StreamFragment = tuple[StreamKind, str]


@dataclass(slots=True)
class RenderContext:
    bot_id: str
    engine: str
    # lazy：subprocess 首次 flush 前才 spawn，传 str 会拿到 ensure 前的 None。返回 None = 隐藏模型槽。
    model_resolver: Callable[[], str | None] = field(default=lambda: None)
    # 附件沙箱根：路径须在此目录内才发。None = 关闭附件检测，避免裸路径绕过 sandbox。
    workdir: str | None = None
    # per-prompt 推理耗时 lazy callable；PromptResponse 前 / mid-stream 返回 None → header 不显示。
    elapsed_resolver: Callable[[], float | None] = field(default=lambda: None)


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
            "kind": ev.tool_kind,
            "title": ev.tool_title,
            "matched": ev.matched,
            "locations": ev.locations,
            "content_summary": ev.content_summary,
            "options": ev.options,
        },
    )


ToolDisplayMode = Literal["off", "brief", "full"]


def _classify(
    ev: object,
    show_thinking: bool,
    tool_display: ToolDisplayMode,
) -> ChatBlock | StreamFragment | None:
    """Single source of truth for ACP-event → CCUID-output mapping.

    Returns:
      * StreamFragment ("agent"|"think", text) — append to the matching stream
                       buffer; flush on next non-stream / different-kind event
      * ChatBlock      — flush both stream buffers, emit this block
      * None           — uninteresting (drop or wait for next)

    Callers spot `PromptResponse` / `UsageUpdate` / `BackendError` themselves —
    they terminate or footer-feed the stream and we don't want to leak that
    protocol detail into here."""
    show_tools = tool_display != "off"
    if isinstance(ev, AgentMessageChunk):
        # agent 直接吐图（ACP ImageContentBlock）—— base64 inline data
        if isinstance(ev.content, ImageContentBlock):
            return ChatBlock("agent_image", "", meta={"data": ev.content.data, "mime_type": ev.content.mime_type})
        text = _chunk_text(ev.content)
        return ("agent", text) if text else None
    if isinstance(ev, AgentThoughtChunk) and show_thinking:
        # token-level 切片 → fragment 累积（见上方 StreamFragment 说明）
        text = _chunk_text(ev.content)
        return ("think", text) if text else None
    if isinstance(ev, ToolCallStart) and show_tools:
        # 同一个 toolCallId 经常 emit 两次（先 generic title 再具体 title）
        kind = ev.kind if ev.kind is not None else "other"
        title = ev.title if ev.title is not None else kind
        return ChatBlock("tool", title, meta={"kind": kind, "tool_call_id": ev.tool_call_id})
    if isinstance(ev, ToolCallProgress) and show_tools:
        # failed 永远显示（不论 brief/full）；带 content 的 update 只在 full 显示
        if ev.status == "failed":
            err = ev.raw_output.get("error") if isinstance(ev.raw_output, dict) else None
            return ChatBlock("tool_failed", str(err) if err else "failed")
        if tool_display == "full" and ev.content:
            from .acp.content import summarize_content  # noqa: PLC0415

            summary = summarize_content(ev.content)
            if summary:
                kind = ev.kind if ev.kind is not None else "other"
                title = ev.title if ev.title is not None else kind
                return ChatBlock(
                    "tool",
                    f"{title}\n{summary}",
                    meta={"kind": kind, "tool_call_id": ev.tool_call_id},
                )
    if isinstance(ev, AgentPlanUpdate) and show_tools:
        return ChatBlock("plan", _fmt_plan(ev))
    if isinstance(ev, CurrentModeUpdate) and show_tools:
        return ChatBlock("mode", ev.current_mode_id)
    if isinstance(ev, PermissionEvent):
        return _permission_block(ev)
    # UsageUpdate 走 footer 路径，这里显式 None 不计入未知类型。
    if isinstance(ev, UsageUpdate):
        return None
    # 关了 ShowThinking 的 AgentThoughtChunk 落这里 → 静默 drop。
    if isinstance(ev, AgentThoughtChunk):
        return None
    # 未消费的事件类型：known-unused 静默；其它陌生类型每种 log 一次。
    if not isinstance(ev, _KNOWN_UNUSED_EVENTS):
        name = type(ev).__name__
        if name not in _warned_event_types:
            _warned_event_types.add(name)
            logger.debug(f"[CCUID] unhandled ACP event type: {name}")
    return None


def _append_or_replace_tool(buf: list[ChatBlock], block: ChatBlock) -> None:
    """claude-code-acp 对同一个 toolCallId 发两次 tool_call（generic → specific
    title），不去重会让用户看到 `Write` + `Write /path/to/file.py` 两条挨着。"""
    tid = block.meta.get("tool_call_id") if block.kind == "tool" else None
    if tid:
        for i in range(len(buf) - 1, -1, -1):
            b = buf[i]
            if b.kind == "tool" and b.meta.get("tool_call_id") == tid:
                buf[i] = block
                return
    buf.append(block)


def _permission_buttons(options: tuple[PermissionOptionView, ...]) -> list[list[Button]]:
    from ..cc_config.prefix import cc_prefix

    pfx = cc_prefix()
    kinds = {option.kind for option in options}
    buttons: list[Button] = []
    if "allow_once" in kinds:
        buttons.append(Button("允许一次", f"{pfx}允许", "允许", action=2))
    if "allow_always" in kinds:
        buttons.append(Button("总是允许", f"{pfx}允许 永久", "永久", action=2))
    if "reject_once" in kinds:
        buttons.append(Button("拒绝一次", f"{pfx}拒绝", "拒绝", style=0, action=2))
    return [buttons] if buttons else []


def blocks_to_text_parts(blocks: list[ChatBlock]) -> list[str]:
    """Flatten blocks into discrete text strings for the text/forward path."""
    out: list[str] = []
    for block in blocks:
        if block.kind == "agent_md":
            out.append(block.body)
        elif block.kind == "think":
            out.append(f"think: {block.body}")
        elif block.kind == "tool":
            out.append(f"{block.meta['kind']}: {block.body}")
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
            if decision == "ask":
                label = "待审核"
            elif not matched:
                label = "已取消"
            elif decision == "allow_once":
                label = "已自动允许"
            elif decision == "allow_always":
                label = "已自动永久允许"
            elif decision == "reject_once":
                label = "已自动拒绝"
            else:
                raise AssertionError(f"unhandled PermissionMode: {decision!r}")
            parts = [label]
            if tool_kind is not None:
                parts.append(f"[{tool_kind}]")
            line = " · ".join(parts)
            extras: list[str] = []
            if tool_title is not None:
                extras.append(f"操作：{tool_title}")
            if not matched:
                extras.append(f"结果：策略 {decision} 没有匹配选项，已取消")
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
    """估算 block 渲染后字符数，`_should_image` 跟阈值比较。
    permission 的 body 是空串、字符量在 meta（locations/content_summary）里，单独算；其它按 body 长度。"""
    if block.kind != "permission":
        return len(block.body)
    size = 0
    title = block.meta["title"]
    if title is not None:
        size += len(title)
    summary = block.meta["content_summary"]
    if summary is not None:
        size += len(summary)
    for loc in block.meta["locations"]:
        size += len(loc.path) + 8  # 行号 + 分隔符的粗估
    return size


def _should_image(blocks: list[ChatBlock]) -> bool:
    fmt = str(CCUIDConfig.get_config("OutputFormat").data).lower()
    return _should_image_with_format(blocks, fmt)


def _should_image_with_format(blocks: list[ChatBlock], fmt: str) -> bool:
    if fmt == "text":
        return False
    if fmt == "image":
        return True
    if fmt == "auto":
        total = sum(_block_render_size(b) for b in blocks)
        return total >= _AUTO_IMAGE_THRESHOLD
    raise AssertionError(f"unhandled OutputFormat: {fmt!r}")


async def _render_blocks_to_png(blocks: list[ChatBlock], ctx: RenderContext) -> bytes | None:
    display = get_engine(ctx.engine).display
    md = build_markdown(
        blocks,
        ImageContext(
            engine_display=display,
            model_label=ctx.model_resolver(),
            elapsed_sec=ctx.elapsed_resolver(),
            icon_url=engine_icon_url(ctx.engine),
        ),
    )
    scale = int(CCUIDConfig.get_config("RenderScale").data)
    return await render_to_png(md, max_width=_IMAGE_MAX_WIDTH, scale=scale)


async def _send_as_images(bot: Bot, blocks: list[ChatBlock], ctx: RenderContext) -> bool:
    if not blocks:
        return False
    img = await _render_blocks_to_png(blocks, ctx)
    if img is None:
        return False
    await bot.send(MessageSegment.image(img))
    return True


async def _send_as_text(bot: Bot, blocks: list[ChatBlock]) -> None:
    parts = blocks_to_text_parts(blocks)
    if not parts:
        return
    await bot.send("\n\n".join(parts))


async def _send_permission_request(bot: Bot, block: ChatBlock, ctx: RenderContext) -> None:
    parts = blocks_to_text_parts([block])
    if not parts:
        return
    buttons = _permission_buttons(block.meta["options"])
    if not buttons:
        await _send_blocks(bot, [block], ctx)
        return
    reply = parts[0]
    ask_format = str(CCUIDConfig.get_config("AskOutputFormat").data).lower()
    if _should_image_with_format([block], ask_format):
        sent = await _send_as_images(bot, [block], ctx)
        if sent:
            reply = "请选择"
    await bot.send_option(
        reply,
        buttons,
        unsuported_platform=True,
        sep=" / ",
        command_tips="可发送：",
    )


async def _send_blocks(bot: Bot, blocks: list[ChatBlock], ctx: RenderContext) -> None:
    if not blocks:
        return
    renderable = [block for block in blocks if block.kind != "agent_image"]
    if _should_image(renderable):
        sent = await _send_as_images(bot, renderable, ctx)
        if not sent:
            await _send_as_text(bot, renderable)
    else:
        await _send_as_text(bot, renderable)
    # agent 通过 ImageContentBlock 内联返回的图，不论走图走文都得追加发
    await _send_agent_images(bot, blocks)
    await _send_referenced_attachments(bot, renderable, ctx)


# 抓 agent 文本 / tool output 里的本地路径发给用户。起点：~ / Unix 绝对 / Windows 盘符；
# 扩展名 1-8 字符免裸 `.` 误中；`\b` 挡 https:// 伪命中（`https:` 的 s 在词中不算 boundary）。
_FILE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|~|/)[\w./\\-]+\.\w{1,8}",
)
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_MAX_IMAGE_BYTES = 30 * 1024 * 1024  # QQ image segment 上限
_MAX_FILE_BYTES = 100 * 1024 * 1024  # OneBot file segment 保守上限


def _collect_attachment_paths(blocks: list[ChatBlock], sandbox: Path | None) -> list[Path]:
    """从 block body grep 路径、按大小阈值收。sandbox=None 不限范围；传 Path 只收其内的，挡 /etc/passwd、~/.ssh 等。"""
    seen: set[Path] = set()
    out: list[Path] = []
    for block in blocks:
        if not block.body:
            continue
        for raw in _FILE_PATH_RE.findall(block.body):
            p = Path(raw).expanduser().resolve()
            if p in seen:
                continue
            seen.add(p)
            if not p.is_file():
                continue
            if sandbox is not None and not p.is_relative_to(sandbox):
                continue
            ext = p.suffix.lstrip(".").lower()
            limit = _MAX_IMAGE_BYTES if ext in _IMAGE_EXTS else _MAX_FILE_BYTES
            if p.stat().st_size > limit:
                continue
            out.append(p)
    return out


async def _send_referenced_attachments(bot: Bot, blocks: list[ChatBlock], ctx: RenderContext) -> None:
    sandbox: Path | None = None
    if CCUIDConfig.get_config("AttachmentSandbox").data:
        if ctx.workdir is None:
            return
        sandbox = Path(ctx.workdir).expanduser().resolve()
    for path in _collect_attachment_paths(blocks, sandbox):
        ext = path.suffix.lstrip(".").lower()
        if ext in _IMAGE_EXTS:
            await bot.send(MessageSegment.image(path))
        else:
            await bot.send(MessageSegment.file(path, path.name))


async def _send_agent_images(bot: Bot, blocks: list[ChatBlock]) -> None:
    """agent 通过 ACP ImageContentBlock 内联返回的图，base64 解码后直发 bot。"""
    import base64  # noqa: PLC0415

    for block in blocks:
        if block.kind != "agent_image":
            continue
        data = base64.b64decode(block.meta["data"])
        await bot.send(MessageSegment.image(data))


def _format_usage_footer(usage: PromptUsage) -> str:
    parts: list[str] = []
    if usage.input_tokens is not None:
        parts.append(f"input {usage.input_tokens}")
    if usage.output_tokens is not None:
        parts.append(f"output {usage.output_tokens}")
    if usage.cached_read_tokens is not None:
        parts.append(f"cached_read {usage.cached_read_tokens}")
    if usage.cached_write_tokens is not None:
        parts.append(f"cached_write {usage.cached_write_tokens}")
    return " · ".join(parts)


async def render(
    bot: Bot,
    events: AsyncIterator[Any],
    ctx: RenderContext,
    *,
    usage_provider: Callable[[], PromptUsage | None] | None = None,
) -> None:
    """Stream events → blocks。`ask` 权限请求要 mid-stream flush，否则死锁：
    agent 等 permission、render 等 PromptResponse、用户什么都看不到。

    策略：
      - agent_md / tool / plan 等累积成一批
      - stream fragments (agent/think) 各自累积到 buf，遇非匹配 kind / 流末 flush 成 ChatBlock
      - PermissionEvent: flush 全部 + 发审批卡 + 继续
      - 流末: flush 残余 + 追加 usage footer

    usage_provider 返回 None = 该 engine 不报数据（如 claude/cursor）→ 不显示 footer。"""
    show_thinking = bool(CCUIDConfig.get_config("ShowThinking").data)
    tool_display: ToolDisplayMode = CCUIDConfig.get_config("ToolDisplay").data
    show_auto_perms = bool(CCUIDConfig.get_config("ShowAutoPermissions").data)
    pending: list[ChatBlock] = []
    agent_buf: list[str] = []
    thought_buf: list[str] = []

    def flush_agent() -> None:
        text = "".join(agent_buf).strip()
        if text:
            pending.append(ChatBlock("agent_md", text))
        agent_buf.clear()

    def flush_thought() -> None:
        text = "".join(thought_buf).strip()
        if text:
            pending.append(ChatBlock("think", text))
        thought_buf.clear()

    def flush_streams() -> None:
        """Both buffers; order: thought 先（先思考再答），agent 后。"""
        flush_thought()
        flush_agent()

    async def flush_pending() -> None:
        flush_streams()
        if pending:
            await _send_blocks(bot, pending, ctx)
            pending.clear()

    try:
        async for ev in events:
            if isinstance(ev, PromptResponse):
                continue
            out = _classify(ev, show_thinking, tool_display)
            if out is None:
                continue
            if isinstance(out, tuple):
                # StreamFragment: 累积到对应 buf；切 kind 时 flush 另一边
                kind, text = out
                if kind == "agent":
                    flush_thought()
                    agent_buf.append(text)
                else:
                    flush_agent()
                    thought_buf.append(text)
                continue
            # 只有 PermissionEvent 要 mid-stream flush；其它累到下次 flush_pending（理由见 docstring）。
            if out.kind == "permission":
                # 自动决策只是信息（用户无需动作、agent 不卡）；ShowAutoPermissions 没开就吞掉免刷屏。
                # ASK 永远显示——用户必须回复才能解锁 agent。
                if out.meta["decision"] != "ask" and not show_auto_perms:
                    continue
                await flush_pending()
                if out.meta["decision"] == "ask":
                    await _send_permission_request(bot, out, ctx)
                else:
                    await _send_blocks(bot, [out], ctx)
            else:
                flush_streams()
                _append_or_replace_tool(pending, out)
    except BackendError as e:
        flush_streams()
        pending.append(ChatBlock("error", str(e)))
    except Exception:
        logger.exception(f"[CCUID/{ctx.engine}] event stream crashed")

    # footer 跟主输出一起 flush：图模式渲进图底部、text 模式追在末尾
    flush_streams()
    if usage_provider is not None:
        usage = usage_provider()
        if usage is not None:
            line = _format_usage_footer(usage)
            if line:
                pending.append(ChatBlock("usage_footer", line))
    if pending:
        await _send_blocks(bot, pending, ctx)
        pending.clear()
