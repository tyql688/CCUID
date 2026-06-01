from __future__ import annotations

from acp.schema import AvailableCommand

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import send_engine_list
from ..utils.msgs import CommandMsg
from .chat_service import do_chat
from ..utils.list_render import RecordItem, RecordField, markdown_records
from ..utils.runtime.registry import REGISTRY

_SLASH_COMMAND_NAME_MAX = 80
_SLASH_COMMAND_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_. -")


def _valid_slash_command_name(name: str) -> bool:
    return (
        0 < len(name) <= _SLASH_COMMAND_NAME_MAX
        and name[0].isalnum()
        and all(char in _SLASH_COMMAND_CHARS for char in name)
    )


def _resolve_slash_command(raw: str, commands: tuple[AvailableCommand, ...]) -> tuple[str, str] | None:
    text = raw.strip()
    if not text or "\n" in text or "\r" in text:
        return None
    if text.startswith("/"):
        text = text[1:].lstrip()
    if not text:
        return None

    for command in sorted(commands, key=lambda item: len(item.name), reverse=True):
        name = command.name.strip()
        if not _valid_slash_command_name(name):
            continue
        if text == name:
            return name, ""
        if text.startswith(f"{name} "):
            return name, text[len(name) :].strip()
    return None


def _format_slash_prompt(name: str, args: str) -> str:
    return f"/{name} {args}".rstrip()


async def do_command_show(bot: Bot, ev: Event, engine: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(CommandMsg.NO_SESSION)
        return
    commands = REGISTRY.backend(engine).list_commands(meta.sid)
    if not commands:
        await bot.send(CommandMsg.EMPTY)
        return
    records: list[RecordItem] = []
    for command in commands:
        hint = command.input.root.hint if command.input is not None else None
        records.append(
            RecordItem(
                f"/{command.name}",
                (
                    RecordField("说明", command.description),
                    RecordField("输入", hint, code=True),
                ),
            )
        )
    md = markdown_records("Slash Commands", records)
    await send_engine_list(bot, engine, md)


async def do_slash(bot: Bot, ev: Event, engine: str, raw: str) -> None:
    if not raw.strip():
        await do_command_show(bot, ev, engine)
        return
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(CommandMsg.NO_SESSION)
        return
    commands = REGISTRY.backend(engine).list_commands(meta.sid)
    if not commands:
        await bot.send(CommandMsg.EMPTY)
        return
    resolved = _resolve_slash_command(raw, commands)
    if resolved is None:
        await bot.send(CommandMsg.not_found(raw))
        return
    name, args = resolved
    await do_chat(bot, ev, engine, _format_slash_prompt(name, args))
