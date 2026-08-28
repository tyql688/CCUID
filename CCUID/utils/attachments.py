from __future__ import annotations

from dataclasses import dataclass

from acp.schema import AudioContentBlock

from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.utils.resource_manager import RM
from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes

from .acp.prompt import PromptBlock, text_block, audio_block, image_block

_SIGS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_MAX_PROMPT_IMAGES = 4
_MAX_PROMPT_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_PROMPT_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_PROMPT_AUDIO = 2
_MAX_PROMPT_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_PROMPT_AUDIO_TOTAL_BYTES = 30 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class PromptBuildResult:
    blocks: list[PromptBlock]
    warnings: tuple[str, ...] = ()


def _mime(data: bytes) -> str:
    for sig, mime in _SIGS:
        if data.startswith(sig):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_urls(ev: Event) -> list[str]:
    urls = [u for u in ev.image_list if isinstance(u, str) and u]
    if urls:
        return urls
    return [ev.image] if ev.image else []


def _audio_ids(ev: Event) -> list[str]:
    candidates = [audio_id for audio_id in ev.audio_id_list if audio_id]
    if not candidates and ev.audio_id:
        candidates.append(ev.audio_id)
    unique: list[str] = []
    seen: set[str] = set()
    for audio_id in candidates:
        if audio_id not in seen:
            seen.add(audio_id)
            unique.append(audio_id)
    return unique


async def _collect_image_bytes(urls: list[str]) -> list[bytes]:
    if not urls:
        return []
    try:
        result = await change_ev_image_to_bytes(urls)
    except Exception as err:
        logger.debug(f"[CCUID] 下载图片失败: {err!r}")
        return []
    if result is None:
        return []
    return [b for b in result if b]


def _limit_image_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    if len(urls) <= _MAX_PROMPT_IMAGES:
        return urls, []
    skipped = len(urls) - _MAX_PROMPT_IMAGES
    return urls[:_MAX_PROMPT_IMAGES], [f"图片最多 {_MAX_PROMPT_IMAGES} 张，已跳过 {skipped} 张"]


def _filter_image_bytes(images: list[bytes]) -> tuple[list[bytes], list[str]]:
    kept: list[bytes] = []
    warnings: list[str] = []
    total = 0
    for i, raw in enumerate(images, 1):
        size = len(raw)
        if size > _MAX_PROMPT_IMAGE_BYTES:
            warnings.append(f"第 {i} 张图片超过 {_MAX_PROMPT_IMAGE_BYTES // 1024 // 1024}MB，已跳过")
            continue
        if total + size > _MAX_PROMPT_IMAGE_TOTAL_BYTES:
            warnings.append(f"图片总大小超过 {_MAX_PROMPT_IMAGE_TOTAL_BYTES // 1024 // 1024}MB，已跳过后续图片")
            break
        kept.append(raw)
        total += size
    return kept, warnings


def _audio_mime(data: bytes) -> str | None:
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF:
        if data[1] & 0xF6 == 0xF0:
            return "audio/aac"
        if data[1] & 0xE0 == 0xE0:
            return "audio/mpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4"
    return None


async def _collect_audio_blocks(audio_ids: list[str]) -> tuple[list[AudioContentBlock], list[str]]:
    kept: list[AudioContentBlock] = []
    warnings: list[str] = []
    total = 0
    for index, audio_id in enumerate(audio_ids[:_MAX_PROMPT_AUDIO], 1):
        try:
            raw = await RM.get(audio_id)
        except ValueError as error:
            logger.debug(f"[CCUID] 读取语音失败 id={audio_id}: {error!r}")
            warnings.append(f"第 {index} 段语音读取失败，已跳过")
            continue
        mime_type = _audio_mime(raw)
        if mime_type is None:
            warnings.append(f"第 {index} 段语音格式不受支持，已跳过")
            continue
        size = len(raw)
        if size > _MAX_PROMPT_AUDIO_BYTES:
            warnings.append(f"第 {index} 段语音超过 {_MAX_PROMPT_AUDIO_BYTES // 1024 // 1024}MB，已跳过")
            continue
        if total + size > _MAX_PROMPT_AUDIO_TOTAL_BYTES:
            warnings.append(f"语音总大小超过 {_MAX_PROMPT_AUDIO_TOTAL_BYTES // 1024 // 1024}MB，已跳过后续语音")
            break
        kept.append(audio_block(raw, mime_type))
        total += size
    if len(audio_ids) > _MAX_PROMPT_AUDIO:
        warnings.append(f"语音最多 {_MAX_PROMPT_AUDIO} 段，已跳过 {len(audio_ids) - _MAX_PROMPT_AUDIO} 段")
    return kept, warnings


async def build_prompt(ev: Event, text: str) -> PromptBuildResult:
    warnings: list[str] = []
    urls = _image_urls(ev)
    if urls:
        urls, url_warnings = _limit_image_urls(urls)
        warnings.extend(url_warnings)
    logger.debug(f"[CCUID] build_prompt images={len(urls)} reply={ev.reply!r} at={ev.at!r}")
    images, image_warnings = _filter_image_bytes(await _collect_image_bytes(urls))
    warnings.extend(image_warnings)
    audio, audio_warnings = await _collect_audio_blocks(_audio_ids(ev))
    warnings.extend(audio_warnings)
    blocks: list[PromptBlock] = [image_block(raw, _mime(raw)) for raw in images]
    blocks.extend(audio)
    if text:
        blocks.append(text_block(text))
    return PromptBuildResult(blocks=blocks, warnings=tuple(warnings))
