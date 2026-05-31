from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.engines import DEFAULT_ENGINE, get_engine, has_engine
from ..utils.database import CCUIDUserEngine
from ..utils.list_render import send_markdown_image_or_text


async def current_engine(ev: Event) -> str:
    name = await CCUIDUserEngine.get(ev.user_id, ev.group_id)
    if name is not None and has_engine(name):
        return name
    return DEFAULT_ENGINE


async def send_engine_list(bot: Bot, engine: str, markdown: str) -> None:
    await send_markdown_image_or_text(
        bot,
        markdown,
        display=get_engine(engine).display,
        engine_name=engine,
    )
