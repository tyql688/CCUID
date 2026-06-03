from __future__ import annotations

import base64

from acp.schema import (
    TextContentBlock,
    AudioContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    EmbeddedResourceContentBlock,
)

# 必须覆盖 acp SDK conn.prompt 的 ContentBlock 全集：list 不变型，窄化会让 list[PromptBlock] 不可赋值。
PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


def text_block(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def image_block(raw: bytes, mime_type: str) -> ImageContentBlock:
    return ImageContentBlock(type="image", data=base64.b64encode(raw).decode("ascii"), mime_type=mime_type)
