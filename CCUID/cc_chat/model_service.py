from __future__ import annotations

import shutil
from typing import TypeVar
from collections.abc import Callable

from acp.schema import ModelInfo, SessionMode

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import current_engine, send_engine_list
from ..utils.msgs import ChatMsg, ModeMsg, ModelMsg
from ..utils.errors import user_error
from ..utils.engines import EngineSpec, resolve, list_engines
from ..utils.database import CCUIDUserEngine, CCUIDSessionModel, CCUIDSessionNative
from ..utils.acp.backend import BackendError
from ..utils.list_render import (
    RecordItem,
    RecordField,
    markdown_table,
    markdown_records,
    send_markdown_image_or_text,
)
from ..utils.runtime.identity import make_sid
from ..utils.runtime.registry import REGISTRY

_T = TypeVar("_T")


async def _engine_status(spec: EngineSpec, ev: Event) -> str:
    command = spec.cmd
    launcher = command[0] if command else "<empty>"
    head = "ok" if command and shutil.which(launcher) else "missing"

    meta = REGISTRY.find(ev.user_id, ev.group_id, spec.name)
    if meta:
        if meta.busy:
            qd = meta.queue_depth
            state = f"busy (queue={qd})" if qd else "busy"
        else:
            state = f"idle {meta.idle_sec}s"
        return f"{head}, {state}"

    for shared in (False, True):
        sid = make_sid(ev.user_id, ev.group_id, spec.name, shared=shared)
        if await CCUIDSessionNative.fetch(sid):
            return f"{head}, resumable"
    return head


async def do_engine_show(bot: Bot, ev: Event) -> None:
    cur = await current_engine(ev)
    rows: list[list[object | None]] = []
    for i, e in enumerate(list_engines(), 1):
        rows.append(
            [
                "✓" if e.name == cur else "",
                i,
                e.display,
                e.name,
                await _engine_status(e, ev),
            ]
        )
    md = "## CCUID Engines\n\n" + markdown_table(["当前", "#", "名称", "ID", "状态"], rows)
    await send_markdown_image_or_text(bot, md, display="CCUID")


async def do_engine_set(bot: Bot, ev: Event, token: str) -> None:
    target = resolve(token)
    if target is None:
        return
    await CCUIDUserEngine.set(ev.user_id, ev.group_id, target.name)
    await bot.send(ChatMsg.engine_set(target.name))


def _resolve_indexed(
    token: str,
    available: tuple[_T, ...],
    *keys: Callable[[_T], str],
) -> _T | None:
    low = token.strip().lower()
    if not low:
        return None
    for item in available:
        values = tuple(key(item).lower() for key in keys)
        if low in values:
            return item
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(available):
            return available[idx]
    matches = [item for item in available if any(low in key(item).lower() for key in keys)]
    return matches[0] if len(matches) == 1 else None


def _resolve_model(token: str, available: tuple[ModelInfo, ...]) -> ModelInfo | None:
    return _resolve_indexed(token, available, lambda model: model.model_id, lambda model: model.name)


def _resolve_mode(
    token: str,
    available: tuple[SessionMode, ...],
) -> SessionMode | None:
    return _resolve_indexed(token, available, lambda mode: mode.id, lambda mode: mode.name)


async def do_model_show(bot: Bot, ev: Event, engine: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(ModelMsg.NO_SESSION)
        return
    backend = REGISTRY.backend(engine)
    cur_id, available = backend.list_models(meta.sid)
    if not available:
        await bot.send(ModelMsg.NO_MODELS)
        return
    rows: list[list[object | None]] = []
    for i, model in enumerate(available, 1):
        rows.append(["✓" if model.model_id == cur_id else "", i, model.name, model.model_id])
    md = f"## {engine} model ({len(available)} 个)\n\n"
    md += markdown_table(["当前", "#", "名称", "ID"], rows)
    md += f"\n\n{ModelMsg.list_hint()}"
    await send_engine_list(bot, engine, md)


async def do_model_set(bot: Bot, ev: Event, engine: str, token: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(ModelMsg.NO_SESSION)
        return
    backend = REGISTRY.backend(engine)
    _, available = backend.list_models(meta.sid)
    if not available:
        await bot.send(ModelMsg.NO_MODELS)
        return
    resolved = _resolve_model(token, available)
    if resolved is None:
        await bot.send(ModelMsg.not_found(token))
        return
    target_id = resolved.model_id
    try:
        switched = await backend.set_model(meta.sid, target_id)
    except BackendError as e:
        await bot.send(ModelMsg.switch_failed(user_error(e)))
        return
    if switched is None:
        await bot.send(ModelMsg.not_found(token))
        return
    await CCUIDSessionModel.store(meta.sid, switched.model_id)
    await bot.send(ModelMsg.switched(switched.model_id, switched.name))


async def do_mode_show(bot: Bot, ev: Event, engine: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(ModeMsg.NO_SESSION)
        return
    cur_id, available = REGISTRY.backend(engine).list_modes(meta.sid)
    if not available:
        await bot.send(ModeMsg.NO_MODES)
        return
    records: list[RecordItem] = []
    for i, mode in enumerate(available, 1):
        records.append(
            RecordItem(
                f"{'✓ ' if mode.id == cur_id else ''}#{i} {mode.name}",
                (
                    RecordField("ID", mode.id, code=True),
                    RecordField("说明", mode.description),
                ),
            )
        )
    md = markdown_records(f"{engine} mode ({len(available)} 个)", records, footer=ModeMsg.list_hint())
    await send_engine_list(bot, engine, md)


async def do_mode_set(bot: Bot, ev: Event, engine: str, token: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(ModeMsg.NO_SESSION)
        return
    backend = REGISTRY.backend(engine)
    _, available = backend.list_modes(meta.sid)
    if not available:
        await bot.send(ModeMsg.NO_MODES)
        return
    resolved = _resolve_mode(token, available)
    if resolved is None:
        await bot.send(ModeMsg.not_found(token))
        return
    target_id = resolved.id
    try:
        switched = await backend.set_mode(meta.sid, target_id)
    except BackendError as e:
        await bot.send(ModeMsg.switch_failed(user_error(e)))
        return
    if switched is None:
        await bot.send(ModeMsg.not_found(token))
        return
    await bot.send(ModeMsg.switched(switched.id, switched.name))
