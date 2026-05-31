from __future__ import annotations

import re
from html import escape
from dataclasses import dataclass

from gsuid_core.bot import Bot
from gsuid_core.segment import MessageSegment

from .render import ChatBlock, ImageContext, render_to_png, build_html_body, engine_icon_url
from ..cc_config.cc_config import CCUIDConfig

_LIST_IMAGE_MAX_WIDTH = 720
_CELL_MAX_CHARS = 240
_RECORD_VALUE_MAX_CHARS = 420
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_{}\[\]()#+\-.!])")
_BACKTICK_RUN_RE = re.compile(r"`+")
_AUTOLINK_SCHEME_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20})://")


@dataclass(slots=True, frozen=True)
class RecordField:
    label: str
    value: object | None
    code: bool = False
    max_chars: int | None = _RECORD_VALUE_MAX_CHARS


@dataclass(slots=True, frozen=True)
class RecordItem:
    title: str
    fields: tuple[RecordField, ...]


def _plain_text(value: object | None, *, max_chars: int | None = _RECORD_VALUE_MAX_CHARS) -> str:
    text = "" if value is None else str(value)
    if text == "":
        return "-"
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    text = escape(text, quote=False).replace("\n", " / ")
    return _AUTOLINK_SCHEME_RE.sub(r"\1:&#8203;//", text)


def _md_text(value: object | None, *, max_chars: int | None = _RECORD_VALUE_MAX_CHARS) -> str:
    text = _plain_text(value, max_chars=max_chars)
    text = _MARKDOWN_SPECIAL_RE.sub(r"\\\1", text)
    return text.replace("|", "\\|")


def _md_code(value: object | None, *, max_chars: int | None = _RECORD_VALUE_MAX_CHARS) -> str:
    text = _plain_text(value, max_chars=max_chars)
    if text == "-":
        return text
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`"):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def md_cell(value: object | None) -> str:
    return _md_text(value, max_chars=_CELL_MAX_CHARS)


def markdown_table(headers: list[str], rows: list[list[object | None]]) -> str:
    head = "| " + " | ".join(md_cell(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(md_cell(v) for v in row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def markdown_records(title: str, records: list[RecordItem], *, footer: str | None = None) -> str:
    lines = [f"## {_md_text(title, max_chars=120)}", ""]
    for record in records:
        lines.append(f"### {_md_text(record.title, max_chars=180)}")
        for field in record.fields:
            value = (
                _md_code(field.value, max_chars=field.max_chars)
                if field.code
                else _md_text(
                    field.value,
                    max_chars=field.max_chars,
                )
            )
            lines.append(f"- **{_md_text(field.label, max_chars=48)}**：{value}")
        lines.append("")
    if footer:
        lines.append(_md_text(footer, max_chars=600))
    return "\n".join(lines).strip()


async def send_markdown_image(
    bot: Bot,
    markdown: str,
    *,
    display: str,
    engine_name: str | None = None,
) -> bool:
    icon_url = engine_icon_url(engine_name) if engine_name else None
    body_html = build_html_body(
        [ChatBlock("agent_md", markdown)],
        ImageContext(engine_display=display, icon_url=icon_url, render_style="list"),
    )
    scale = int(CCUIDConfig.get_config("RenderScale").data)
    img = await render_to_png(body_html, max_width=_LIST_IMAGE_MAX_WIDTH, scale=scale)
    if img is None:
        return False
    await bot.send(MessageSegment.image(img))
    return True


async def send_markdown_image_or_text(
    bot: Bot,
    markdown: str,
    *,
    display: str,
    engine_name: str | None = None,
) -> None:
    if not await send_markdown_image(bot, markdown, display=display, engine_name=engine_name):
        await bot.send(markdown)
