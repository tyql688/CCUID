from __future__ import annotations

import re
import base64
import binascii
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence

from acp.schema import PermissionOption

from gsuid_core.bot import Bot
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button

from .blocks import _block_render_size, blocks_to_text_parts
from ..render import (
    ChatBlock,
    ImageContext,
    render_to_png,
    build_html_body,
    engine_icon_url,
)
from ..engines import get_engine
from ...cc_config.cc_config import CCUIDConfig

_AUTO_IMAGE_THRESHOLD = 600
_IMAGE_MAX_WIDTH = 720


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


def _permission_buttons(options: Sequence[PermissionOption]) -> list[list[Button]]:
    from ...cc_config.prefix import cc_prefix

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
    raise ValueError(f"unhandled OutputFormat: {fmt!r}")


async def _render_blocks_to_png(blocks: list[ChatBlock], ctx: RenderContext) -> bytes | None:
    display = get_engine(ctx.engine).display
    body_html = build_html_body(
        blocks,
        ImageContext(
            engine_display=display,
            model_label=ctx.model_resolver(),
            elapsed_sec=ctx.elapsed_resolver(),
            icon_url=engine_icon_url(ctx.engine),
        ),
    )
    scale = int(CCUIDConfig.get_config("RenderScale").data)
    return await render_to_png(body_html, max_width=_IMAGE_MAX_WIDTH, scale=scale)


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


async def send_permission_request(bot: Bot, block: ChatBlock, ctx: RenderContext) -> None:
    parts = blocks_to_text_parts([block])
    if not parts:
        return
    buttons = _permission_buttons(block.meta["options"])
    if not buttons:
        await send_blocks(bot, [block], ctx)
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


async def send_blocks(bot: Bot, blocks: list[ChatBlock], ctx: RenderContext) -> None:
    if not blocks:
        return
    renderable = [block for block in blocks if block.kind != "agent_image"]
    if _should_image(renderable):
        sent = await _send_as_images(bot, renderable, ctx)
        if not sent:
            await _send_as_text(bot, renderable)
    else:
        await _send_as_text(bot, renderable)
    await _send_agent_images(bot, blocks)
    await _send_referenced_attachments(bot, renderable, ctx)


# 抓 agent 文本 / tool output 里的本地路径发给用户。起点：~ / Unix 绝对 / Windows 盘符；
# 扩展名 1-8 字符免裸 `.` 误中；`\b` 挡 https:// 伪命中（`https:` 的 s 在词中不算 boundary）。
_FILE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|~|(?<![:/\w])/)[\w./\\-]+\.\w{1,8}",
)
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_MAX_IMAGE_BYTES = 30 * 1024 * 1024
_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_REFERENCED_ATTACHMENTS = 8
_MAX_AGENT_IMAGE_BYTES = _MAX_IMAGE_BYTES
_MAX_AGENT_IMAGE_BASE64_CHARS = ((_MAX_AGENT_IMAGE_BYTES + 2) // 3) * 4 + 4
_SUPPORTED_AGENT_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp"}
)


def _resolve_attachment_path(raw: str, sandbox: Path | None) -> Path | None:
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return None
    try:
        if not path.is_file():
            return None
        if sandbox is not None and not path.is_relative_to(sandbox):
            return None
        size = path.stat().st_size
    except OSError:
        return None
    ext = path.suffix.lstrip(".").lower()
    limit = _MAX_IMAGE_BYTES if ext in _IMAGE_EXTS else _MAX_FILE_BYTES
    return path if size <= limit else None


def _collect_attachment_paths(blocks: list[ChatBlock], sandbox: Path | None) -> list[Path]:
    """从 block body grep 路径、按大小阈值收。sandbox=None 不限范围；传 Path 只收其内的。"""
    seen: set[Path] = set()
    out: list[Path] = []
    for block in blocks:
        if not block.body:
            continue
        for raw in _FILE_PATH_RE.findall(block.body):
            if len(out) >= _MAX_REFERENCED_ATTACHMENTS:
                return out
            path = _resolve_attachment_path(raw, sandbox)
            if path is None or path in seen:
                continue
            seen.add(path)
            out.append(path)
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


def _agent_image_mime_supported(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    mime_type = raw.split(";", 1)[0].strip().lower()
    return mime_type in _SUPPORTED_AGENT_IMAGE_MIME_TYPES


def _decode_agent_image(raw: object) -> bytes | None:
    if not isinstance(raw, str):
        return None
    if len(raw) > _MAX_AGENT_IMAGE_BASE64_CHARS:
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > _MAX_AGENT_IMAGE_BYTES:
        return None
    return data


async def _send_agent_images(bot: Bot, blocks: list[ChatBlock]) -> None:
    """agent 通过 ACP ImageContentBlock 内联返回的图，base64 解码后直发 bot。"""
    for block in blocks:
        if block.kind != "agent_image":
            continue
        if not _agent_image_mime_supported(block.meta.get("mime_type")):
            continue
        data = _decode_agent_image(block.meta.get("data"))
        if data is None:
            continue
        await bot.send(MessageSegment.image(data))
