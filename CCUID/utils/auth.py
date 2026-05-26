from typing import Any
from functools import wraps
from collections.abc import Callable, Awaitable

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .database import CCUIDGrantUser, CCUIDGrantGroup


async def is_authorized(uid: str, gid: str | None, user_pm: int) -> bool:
    if user_pm <= 0:
        return True
    if await CCUIDGrantUser.exists(uid):
        return True
    if gid is None:
        return False
    return await CCUIDGrantGroup.exists(gid)


def require_auth(fn: Callable[[Bot, Event], Awaitable[Any]]) -> Callable[[Bot, Event], Awaitable[None]]:
    @wraps(fn)
    async def wrapper(bot: Bot, ev: Event) -> None:
        if await is_authorized(ev.user_id, ev.group_id, ev.user_pm):
            await fn(bot, ev)
            return
        logger.info(f"[CCUID] unauthorized: user={ev.user_id} group={ev.group_id} cmd={ev.command!r}")

    return wrapper
