import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import current_engine, send_engine_list
from ..utils.msgs import ChatMsg, ModeMsg, ModelMsg
from ..utils.errors import user_error
from ..utils.engines import EngineSpec, resolve, list_engines
from ..utils.session import REGISTRY, make_sid
from ..utils.database import CCUIDUserEngine, CCUIDSessionModel, CCUIDSessionNative
from ..utils.list_render import (
    RecordItem,
    RecordField,
    markdown_table,
    markdown_records,
    send_markdown_image_or_text,
)


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


def _resolve_model(token: str, available: tuple[tuple[str, str], ...]) -> tuple[str, str] | None:
    low = token.strip().lower()
    for mid, name in available:
        if low in (mid.lower(), name.lower()):
            return mid, name
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(available):
            return available[idx]
    matches = [(mid, name) for mid, name in available if low in mid.lower() or low in name.lower()]
    return matches[0] if len(matches) == 1 else None


def _resolve_mode(
    token: str,
    available: tuple[tuple[str, str, str | None], ...],
) -> tuple[str, str, str | None] | None:
    low = token.strip().lower()
    for mode_id, name, desc in available:
        if low in (mode_id.lower(), name.lower()):
            return mode_id, name, desc
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(available):
            return available[idx]
    matches = [
        (mode_id, name, desc) for mode_id, name, desc in available if low in mode_id.lower() or low in name.lower()
    ]
    return matches[0] if len(matches) == 1 else None


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
    for i, (mid, name) in enumerate(available, 1):
        rows.append(["✓" if mid == cur_id else "", i, name, mid])
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
    target_id, _ = resolved
    try:
        switched = await backend.set_model(meta.sid, target_id)
    except Exception as e:
        await bot.send(ModelMsg.switch_failed(user_error(e)))
        return
    if switched is None:
        await bot.send(ModelMsg.not_found(token))
        return
    new_id, new_name = switched
    await CCUIDSessionModel.store(meta.sid, new_id)
    await bot.send(ModelMsg.switched(new_id, new_name))


async def do_mode_show(bot: Bot, ev: Event, engine: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(ModeMsg.NO_SESSION)
        return
    cur_id, available = REGISTRY.backend(engine).list_modes(meta.sid)
    if not available:
        await bot.send(ModeMsg.NO_MODES)
        return
    records = [
        RecordItem(
            f"{'✓ ' if mode_id == cur_id else ''}#{i} {name}",
            (
                RecordField("ID", mode_id, code=True),
                RecordField("说明", desc),
            ),
        )
        for i, (mode_id, name, desc) in enumerate(available, 1)
    ]
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
    target_id, _, _ = resolved
    try:
        switched = await backend.set_mode(meta.sid, target_id)
    except Exception as e:
        await bot.send(ModeMsg.switch_failed(user_error(e)))
        return
    if switched is None:
        await bot.send(ModeMsg.not_found(token))
        return
    mode_id, name, _ = switched
    await bot.send(ModeMsg.switched(mode_id, name))
