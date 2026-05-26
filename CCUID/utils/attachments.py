import base64

from acp.schema import TextContentBlock, ImageContentBlock

from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes

_SIGS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
PromptBlock = TextContentBlock | ImageContentBlock


def _mime(data: bytes) -> str:
    for sig, mime in _SIGS:
        if data.startswith(sig):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


async def _collect_image_bytes(ev: Event) -> list[bytes]:
    urls: list[str] = [u for u in ev.image_list if isinstance(u, str) and u]
    if not urls and ev.image:
        urls = [ev.image]
    if not urls:
        return []
    logger.debug(f"[CCUID] build_prompt images={len(urls)} reply={ev.reply!r} at={ev.at!r}")
    try:
        result = await change_ev_image_to_bytes(urls)
    except Exception:
        logger.exception("[CCUID] 下载图片失败")
        return []
    if result is None:
        return []
    return [b for b in result if b]


async def build_prompt(ev: Event, text: str) -> list[PromptBlock]:
    blocks: list[PromptBlock] = []
    for raw in await _collect_image_bytes(ev):
        blocks.append(ImageContentBlock(type="image", data=base64.b64encode(raw).decode(), mime_type=_mime(raw)))
    if text:
        blocks.append(TextContentBlock(type="text", text=text))
    return blocks
