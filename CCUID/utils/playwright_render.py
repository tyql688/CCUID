from __future__ import annotations

import asyncio
from contextlib import suppress, asynccontextmanager
from dataclasses import dataclass
from collections.abc import Callable, Awaitable, AsyncGenerator

from playwright.async_api import Page, Browser, async_playwright

from gsuid_core.logger import logger

_TIMEOUT_MS = 30_000
_INITIAL_HEIGHT = 100
_RENDER_RETRIES = 3
_PAGE_HEIGHT_JS = """() => Math.ceil(Math.max(
    document.body.scrollHeight,
    document.body.offsetHeight,
    document.documentElement.clientHeight,
    document.documentElement.scrollHeight,
    document.documentElement.offsetHeight
))"""
_SEM = asyncio.Semaphore(1)

RenderExtras = Callable[[Page], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class _RenderSpec:
    html: str
    max_width: int
    scale: int
    single_image_height: int
    slice_height: int
    render_extras: RenderExtras


async def render_html_to_pngs(
    html: str,
    *,
    max_width: int,
    scale: int,
    single_image_height: int,
    slice_height: int,
    render_extras: RenderExtras,
) -> list[bytes]:
    spec = _RenderSpec(html, max_width, scale, single_image_height, slice_height, render_extras)
    async with _SEM:
        for retry in range(_RENDER_RETRIES + 1):
            try:
                async with _browser() as browser:
                    return await _render_page(browser, spec)
            except Exception as exc:
                if retry >= _RENDER_RETRIES:
                    logger.exception("[CCUID] markdown 渲染失败")
                    return []
                logger.warning(f"[CCUID] markdown 渲染失败，正在重试 ({retry + 1}/{_RENDER_RETRIES}): {exc!r}")
        return []


@asynccontextmanager
async def _browser() -> AsyncGenerator[Browser]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            with suppress(Exception):
                await browser.close()


async def _render_page(browser: Browser, spec: _RenderSpec) -> list[bytes]:
    context = await browser.new_context(
        color_scheme="light",
        device_scale_factor=spec.scale,
        java_script_enabled=True,
        locale="zh-CN",
        viewport={"width": spec.max_width, "height": _INITIAL_HEIGHT},
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(_TIMEOUT_MS)
        await page.set_content(spec.html, wait_until="load", timeout=_TIMEOUT_MS)
        await spec.render_extras(page)
        await page.evaluate("document.fonts.ready")
        return await _screenshots(page, spec)
    finally:
        with suppress(Exception):
            await context.close()


async def _screenshots(page: Page, spec: _RenderSpec) -> list[bytes]:
    total = int(await page.evaluate(_PAGE_HEIGHT_JS))
    if total <= spec.single_image_height:
        return [await page.locator("body").screenshot(type="png", timeout=_TIMEOUT_MS)]

    pngs: list[bytes] = []
    for y in range(0, max(1, total), spec.slice_height):
        height = min(spec.slice_height, total - y)
        await page.set_viewport_size({"width": spec.max_width, "height": height})
        await page.evaluate("(top) => window.scrollTo(0, top)", y)
        pngs.append(await page.screenshot(type="png", timeout=_TIMEOUT_MS))
    return pngs
