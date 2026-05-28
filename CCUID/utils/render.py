from __future__ import annotations

import re
import base64
from html import escape
from typing import TYPE_CHECKING, Any, Literal
from pathlib import Path
from datetime import datetime
from functools import lru_cache
from dataclasses import field, dataclass

from pygments import highlight as _pyg_highlight
from markdown_it import MarkdownIt
from pygments.util import ClassNotFound
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from mdit_py_plugins.gfm import gfm_plugin
from playwright.async_api import async_playwright
from mdit_py_plugins.deflist import deflist_plugin
from pygments.formatters.html import HtmlFormatter

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
# Cursor IDE 行号引用 info string: `<start>:<end>:<path>`
_CURSOR_REF_RE = re.compile(r"^(\d+):(\d+):(.+)$")


def _text(s: str) -> str:
    return escape(s, quote=False).replace("\n", " ")


def clean_permission_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    text = summary.strip()
    if text.startswith("text: "):
        text = text[6:].strip()
    if text.startswith("Not in allowlist:"):
        detail = text.removeprefix("Not in allowlist:").strip()
        return f"不在允许列表：{detail}" if detail else "不在允许列表"
    return text


_PYG_FORMATTER = HtmlFormatter(cssclass="highlight", style="friendly", nowrap=True)


def _resolve_caption_and_lexer(name: str) -> tuple[str | None, Any]:
    """info string → (caption_or_None, lexer_or_None)
    * Cursor 行号引用 `<start>:<end>:<path>` → caption=路径行号, lexer 按 path
    * 已知 lang                             → caption=None (无 figure 包裹), lexer
    * 未知 lang                             → caption=lang, lexer=None
    * 无                                    → (None, None)
    """
    if not name:
        return None, None
    cursor_ref = _CURSOR_REF_RE.match(name)
    if cursor_ref:
        start, end, path = cursor_ref.groups()
        caption = f"{path} :{start}-{end}"
        try:
            return caption, get_lexer_for_filename(path)
        except ClassNotFound:
            return caption, None
    try:
        return None, get_lexer_by_name(name)
    except ClassNotFound:
        return name, None


def _highlight_fence(code: str, name: str, _attrs: str) -> str:
    """markdown-it-py fence renderer。统一 DOM：
      * mermaid    → `<pre class="mermaid">` （前端处理，不进 figure）
      * 无 caption → `<pre[.highlight]><code>...</code></pre>` (2 层)
      * 有 caption → `<figure class="cc-code"><figcaption>...</figcaption>` + 上述 → 3 层

    pygments 用 `nowrap=True` 只输出 token spans，由我们自己包 pre.highlight；
    CSS `.highlight .k` 等子选择器在 pre 是 .highlight 时仍匹配。
    """
    name = name.strip()
    if name.lower() == "mermaid":
        return f'<pre class="mermaid">{escape(code, quote=False)}</pre>'
    caption, lexer = _resolve_caption_and_lexer(name)
    if lexer is not None:
        body = f'<pre class="highlight"><code>{_pyg_highlight(code, lexer, _PYG_FORMATTER)}</code></pre>'
    else:
        body = f"<pre><code>{escape(code, quote=False)}</code></pre>"
    if caption is None:
        return body
    return f'<figure class="cc-code"><figcaption>{escape(caption)}</figcaption>{body}</figure>'


def _build_md_engine() -> MarkdownIt:
    """commonmark + GFM 全套 (gfm_plugin: table/strikethrough/autolink/tasklist/
    alerts/footnote + dollarmath) + deflist。行为对齐 cc-session 的 remark-gfm。

    要点：
    * GFM 严格语义——表格遇到下一行不含 `|` 会关闭，不会卷吞后文（旧 python-markdown
      的 tables 扩展不严格，会把 `---` / `## H2` / mermaid 块全卷进 `<td>`）。
    * `html=True`：`build_markdown` 把 `<div class="cc-header">` chrome 拼进 markdown
      字符串，需要 raw HTML pass-through；agent 偶尔也发 `<br/>`。
    * dollarmath 输出 `<span class="math inline">` / `<div class="math block">`，
      chat.js 找这两个 class 渲染 katex。
    * `> [!NOTE]` / `[!WARNING]` 等 GFM alert 内置支持，渲为
      `<div class="markdown-alert markdown-alert-note">`。
    """
    md = MarkdownIt("commonmark", {"highlight": _highlight_fence, "html": True, "linkify": True})
    md.enable("linkify")
    md.use(gfm_plugin, dollarmath=True)
    md.use(deflist_plugin)
    return md


_MD_ENGINE = _build_md_engine()

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
    "usage_footer",
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
    if block.kind == "usage_footer":
        return f'<div class="cc-usage-footer">{_text(block.body)}</div>'
    return _text(block.body)


def _render_permission(block: ChatBlock) -> str:
    decision = block.meta["decision"]
    tool_kind = block.meta["kind"]
    tool_title = block.meta["title"]
    matched = block.meta["matched"]
    locations = block.meta["locations"]
    content_summary = clean_permission_summary(block.meta["content_summary"])
    hint = ""
    if decision == "ask":
        cls, label = "pending", "待审核"
    elif not matched:
        cls, label = "denied", "已取消"
    elif decision == "allow_once":
        cls, label = "allowed", "已自动允许"
    elif decision == "allow_always":
        cls, label = "allowed", "已自动永久允许"
    elif decision == "reject_once":
        cls, label = "denied", "已自动拒绝"
    else:
        raise AssertionError(f"unhandled PermissionMode: {decision!r}")

    header_parts = [_tag(label, cls)]
    if tool_kind is not None:
        header_parts.append(_tag(tool_kind, tool_kind))
    header = f'<div class="cc-perm-head">{"".join(header_parts)}</div>'

    detail_parts: list[str] = []
    if tool_title is not None:
        detail_parts.append(
            '<section class="cc-perm-section">'
            '<div class="cc-perm-label">操作</div>'
            f'<pre class="cc-perm-command">{escape(tool_title, quote=False)}</pre>'
            "</section>"
        )
    if not matched:
        detail_parts.append(
            '<section class="cc-perm-section">'
            '<div class="cc-perm-label">结果</div>'
            f'<div class="cc-perm-text">策略 <code>{_text(decision)}</code> 没有匹配选项，已取消。</div>'
            "</section>"
        )
    if locations:
        items = "".join(
            f"<li>{_text(loc.path)}{f':{loc.line}' if loc.line is not None else ''}</li>" for loc in locations
        )
        detail_parts.append(
            '<section class="cc-perm-section">'
            '<div class="cc-perm-label">位置</div>'
            f'<ul class="cc-perm-list">{items}</ul>'
            "</section>"
        )
    if content_summary is not None:
        detail_parts.append(
            '<section class="cc-perm-section">'
            '<div class="cc-perm-label">原因</div>'
            f'<div class="cc-perm-text">{_text(content_summary)}</div>'
            "</section>"
        )
    body = header + "".join(detail_parts) + hint
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
    body = _MD_ENGINE.render(md)
    css = f"{_katex_css()}{_chat_css()}{_pygments_css()}"
    return (
        _chat_html().replace("{{ asset_base }}", _ASSETS.as_uri()).replace("{{ css }}", css).replace("{{ body }}", body)
    )


async def _render_extras(page: Page) -> None:
    has_math = await page.locator(".math").count() > 0
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
