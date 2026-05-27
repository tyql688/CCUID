from __future__ import annotations

import re
import base64
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from pathlib import Path
from datetime import datetime
from functools import lru_cache
from dataclasses import field, dataclass

from markdown import markdown
from playwright.async_api import async_playwright

from gsuid_core.logger import logger

if TYPE_CHECKING:
    from playwright.async_api import Page

_HERE = Path(__file__).parent
CHAT_CSS = _HERE / "render_assets" / "chat.css"
CHAT_HTML = _HERE / "render_assets" / "chat.html.j2"
CHAT_JS = _HERE / "render_assets" / "chat.js"
_ASSETS = _HERE / "render_assets"
_VENDOR = _ASSETS / "vendor"
_KATEX_JS = _VENDOR / "katex" / "katex.min.js"
_KATEX_CSS = _VENDOR / "katex" / "katex.min.css"
_MERMAID_JS = _VENDOR / "mermaid" / "mermaid.min.js"

_FONT_URL = re.compile(r"url\([\"']?(fonts/[^)\"']+)[\"']?\)")

_PLAYWRIGHT_TIMEOUT_MS = 30_000
_PLAYWRIGHT_INITIAL_HEIGHT = 100


def _text(s: str) -> str:
    return escape(s, quote=False).replace("\n", " ")


def _format_mermaid(
    source: str, language: str, class_name: str, options: dict[str, Any], md: Any, **kwargs: Any
) -> str:
    return f'<pre class="mermaid">{escape(source, quote=False)}</pre>'


_MARKDOWN_EXTENSIONS = (
    "extra",
    "sane_lists",
    "pymdownx.highlight",
    "pymdownx.tilde",
    "pymdownx.tasklist",
    "pymdownx.arithmatex",
    "pymdownx.superfences",
)
_MARKDOWN_EXTENSION_CONFIGS: dict[str, Any] = {
    "pymdownx.highlight": {
        "use_pygments": True,
        "guess_lang": False,
        "default_lang": "text",
        "noclasses": False,
        "css_class": "highlight",
        "pygments_style": "friendly",
        "auto_title": True,
        "auto_title_map": {"Text Only": "text"},
    },
    "pymdownx.tasklist": {"custom_checkbox": True},
    "pymdownx.arithmatex": {"generic": True},
    "pymdownx.superfences": {
        "custom_fences": [
            {
                "name": "mermaid",
                "class": "mermaid",
                "format": _format_mermaid,
            }
        ]
    },
}

BlockKind = Literal[
    "agent_md",
    "agent_image",
    "think",
    "tool",
    "tool_failed",
    "plan",
    "mode",
    "error",
    "permission",
]


@dataclass(slots=True, frozen=True)
class ChatBlock:
    kind: BlockKind
    body: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageContext:
    engine_display: str
    model_label: str | None = None  # e.g. "claude-sonnet-4-7"；None 时隐藏


def _tag(label: str, kind: str = "") -> str:
    cls = f"cc-tag cc-tag-{kind}" if kind else "cc-tag"
    return f'<span class="{cls}">{_text(label)}</span>'


# agent 流式 chunks 裸 join 时 `out## title` 会被 markdown 当成 prose；强制
# 在标题前插换行。`[^\n#]` 排除 `#` 避免误拆 `##` / `###` 连续标记
_HEADING_INLINE = re.compile(r"([^\n#])(#{1,6}\s+)")
_BLOCK_MARKER = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|```)")


def _normalize_blocks(md: str) -> str:
    md = _HEADING_INLINE.sub(r"\1\n\n\2", md)
    lines = md.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if i > 0 and lines[i - 1].strip() and not _BLOCK_MARKER.match(lines[i - 1]) and _BLOCK_MARKER.match(line):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _render_block(block: ChatBlock) -> str:
    if block.kind == "agent_md":
        return _normalize_blocks(block.body.rstrip())
    if block.kind == "agent_image":
        return ""
    if block.kind == "think":
        return f'<div class="cc-think">{_tag("think")}{_text(block.body)}</div>'
    if block.kind == "tool":
        kind = block.meta["kind"]
        return f'<div class="cc-tool cc-tool-{kind}">{_tag(kind, kind)}{_text(block.body)}</div>'
    if block.kind == "tool_failed":
        return f'<div class="cc-tool cc-tool-failed">{_tag("failed", "failed")}{_text(block.body)}</div>'
    if block.kind == "plan":
        return block.body
    if block.kind == "mode":
        return f'<div class="cc-aux">{_tag("mode")}{_text(block.body)}</div>'
    if block.kind == "error":
        body = escape(block.body, quote=False)
        return f'<div class="cc-error">{_tag("error", "failed")}<pre class="cc-error-body">{body}</pre></div>'
    if block.kind == "permission":
        return _render_permission(block)
    return _text(block.body)


def _render_permission(block: ChatBlock) -> str:
    from ..cc_config.prefix import cc_prefix

    decision = block.meta["decision"]
    tool_kind = block.meta["kind"]
    tool_title = block.meta["title"]
    matched = block.meta["matched"]
    locations = block.meta["locations"]
    content_summary = block.meta["content_summary"]
    options = block.meta["options"]
    hint = ""
    if decision == "ask":
        cls, label = "pending", "NEED APPROVAL"
        # Pull the live prefix so the hint stays correct when the user has
        # remapped the CCUID plugin prefix away from the default "cc".
        pfx = cc_prefix()
        hint = (
            '<div class="cc-permission-hint">→ 发送 '
            f"<code>{pfx}允许</code> / <code>{pfx}允许 永久</code> / "
            f"<code>{pfx}拒绝</code> 决定</div>"
        )
    elif not matched:
        cls, label = "denied", f"NO OPTION {decision} → CANCELLED"
    elif decision == "allow_once":
        cls, label = "allowed", "AUTO ALLOW ONCE"
    elif decision == "allow_always":
        cls, label = "allowed", "AUTO ALLOW ALWAYS"
    elif decision == "reject_once":
        cls, label = "denied", "AUTO REJECT ONCE"
    else:
        raise AssertionError(f"unhandled PermissionMode: {decision!r}")

    header_parts = [_tag(label, cls)]
    if tool_kind is not None:
        header_parts.append(_tag(tool_kind, tool_kind))
    if tool_title is not None:
        header_parts.append(_text(tool_title))

    detail_parts: list[str] = []
    if locations:
        items = "".join(
            f"<li>{_text(loc.path)}{f':{loc.line}' if loc.line is not None else ''}</li>" for loc in locations
        )
        detail_parts.append(f'<div class="cc-perm-locations"><b>locations</b><ul>{items}</ul></div>')
    if content_summary is not None:
        detail_parts.append(f'<div class="cc-perm-content"><b>content</b> {_text(content_summary)}</div>')
    if options and decision == "ask":
        opt_items = "".join(f"<li>{_tag(opt.kind, opt.kind)}{_text(opt.name)}</li>" for opt in options)
        detail_parts.append(f'<div class="cc-perm-options"><b>options</b><ul>{opt_items}</ul></div>')

    body = "".join(header_parts) + "".join(detail_parts) + hint
    return f'<div class="cc-permission cc-permission-{cls}">{body}</div>'


def build_markdown(blocks: list[ChatBlock], ctx: ImageContext) -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    top = f'<span class="cc-engine">{_text(ctx.engine_display)}</span><span class="cc-ts">{ts}</span>'
    sub = (
        f'<div class="cc-subheader"><span class="cc-model">{_text(ctx.model_label)}</span></div>'
        if ctx.model_label
        else ""
    )
    header = f'<div class="cc-header">{top}</div>{sub}'
    parts: list[str] = [header, ""]
    for block in blocks:
        parts.append(_render_block(block))
        parts.append("")
    return "\n".join(parts).strip()


@lru_cache(maxsize=1)
def _chat_css() -> str:
    if not CHAT_CSS.exists():
        return ""
    return _inline_font_urls(CHAT_CSS.read_text(encoding="utf-8"), _ASSETS)


@lru_cache(maxsize=1)
def _chat_html() -> str:
    return CHAT_HTML.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _katex_css() -> str:
    if not _KATEX_CSS.exists():
        return ""
    return _inline_font_urls(_KATEX_CSS.read_text(encoding="utf-8"), _KATEX_CSS.parent)


def _inline_font_urls(css: str, root: Path) -> str:
    def repl(m: re.Match[str]) -> str:
        data = base64.b64encode((root / m.group(1)).read_bytes()).decode("ascii")
        return f"url(data:font/woff2;base64,{data})"

    return _FONT_URL.sub(repl, css)


@lru_cache(maxsize=1)
def _pygments_css() -> str:
    from pygments.formatters.html import HtmlFormatter

    return HtmlFormatter(cssclass="highlight", style="friendly").get_style_defs(".highlight")


def _html_doc(md: str) -> str:
    body = markdown(
        md,
        extensions=list(_MARKDOWN_EXTENSIONS),
        extension_configs=_MARKDOWN_EXTENSION_CONFIGS,
        output_format="html",
    )
    css = f"{_katex_css()}{_chat_css()}{_pygments_css()}"
    return (
        _chat_html().replace("{{ asset_base }}", _ASSETS.as_uri()).replace("{{ css }}", css).replace("{{ body }}", body)
    )


async def _render_extras(page: Page) -> None:
    has_math = await page.locator(".arithmatex").count() > 0
    has_mermaid = await page.locator("pre.mermaid").count() > 0
    if not has_math and not has_mermaid:
        return
    if has_math:
        await page.add_script_tag(path=str(_KATEX_JS))
    if has_mermaid:
        await page.add_script_tag(path=str(_MERMAID_JS))
    await page.add_script_tag(path=str(CHAT_JS))
    await page.evaluate("window.ccuidRenderExtras()")


async def render_to_png(md: str, *, max_width: int = 720, scale: int = 2) -> bytes | None:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    color_scheme="light",
                    device_scale_factor=scale,
                    java_script_enabled=True,
                    locale="zh-CN",
                    viewport={"width": max_width, "height": _PLAYWRIGHT_INITIAL_HEIGHT},
                )
                page = await context.new_page()
                page.set_default_timeout(_PLAYWRIGHT_TIMEOUT_MS)
                await page.set_content(_html_doc(md), wait_until="load", timeout=_PLAYWRIGHT_TIMEOUT_MS)
                await _render_extras(page)
                await page.evaluate("document.fonts.ready")
                return await page.locator("body").screenshot(type="png", timeout=_PLAYWRIGHT_TIMEOUT_MS)
            finally:
                await browser.close()
    except Exception:
        logger.exception("[CCUID] markdown 渲染失败")
        return None
