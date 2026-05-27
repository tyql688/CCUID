from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .service import (
    do_new,
    do_chat,
    do_deny,
    do_stop,
    do_clear,
    do_approve,
    do_arm_yolo,
    do_engine_set,
    current_engine,
    do_engine_show,
)
from ..utils.auth import require_auth

sv_cmd = SV("CCUID 命令", priority=5)
sv_chat = SV("CCUID 对话", priority=10)


@sv_cmd.on_command(("允许", "approve"), block=True)
@require_auth
async def approve(bot: Bot, ev: Event) -> None:
    always = ev.text.strip() in {"永久", "always", "forever"}
    await do_approve(bot, ev, await current_engine(ev), always=always)


@sv_cmd.on_command(("拒绝", "deny"), block=True)
@require_auth
async def deny(bot: Bot, ev: Event) -> None:
    await do_deny(bot, ev, await current_engine(ev))


@sv_cmd.on_command(("engine", "引擎", "switch", "切换", "eng"), block=True)
@require_auth
async def engine(bot: Bot, ev: Event) -> None:
    arg = ev.text.strip()
    if not arg:
        await do_engine_show(bot, ev)
    else:
        await do_engine_set(bot, ev, arg)


@sv_cmd.on_fullmatch(("停", "stop"), block=True)
@require_auth
async def stop(bot: Bot, ev: Event) -> None:
    await do_stop(bot, ev, await current_engine(ev))


@sv_cmd.on_fullmatch(
    ("new", "reset"),
    block=True,
)
@require_auth
async def new(bot: Bot, ev: Event) -> None:
    await do_new(bot, ev, await current_engine(ev))


@sv_cmd.on_fullmatch(("clear", "清理", "清空"), block=True)
@require_auth
async def clear(bot: Bot, ev: Event) -> None:
    await do_clear(bot, ev, await current_engine(ev))


@sv_cmd.on_fullmatch(("下次允许", "yolo"), block=True)
@require_auth
async def arm_yolo(bot: Bot, ev: Event) -> None:
    await do_arm_yolo(bot, ev, await current_engine(ev))


@sv_chat.on_prefix("")
@require_auth
async def chat(bot: Bot, ev: Event) -> None:
    await do_chat(bot, ev, await current_engine(ev), ev.text.strip())
