from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import send_engine_list
from ..utils.msgs import ChatMsg, QueueMsg
from ..utils.errors import user_error
from ..utils.session import (
    REGISTRY,
    DequeueOk,
    DequeueNotFound,
    DequeueForbidden,
    DequeueIsRunning,
    DequeueNoSession,
)
from ..utils.acp.backend import BackendError
from ..utils.list_render import RecordItem, RecordField, markdown_records


async def do_new(bot: Bot, ev: Event, engine: str) -> None:
    await REGISTRY.restart(ev.user_id, ev.group_id, engine)
    await bot.send(ChatMsg.reset_done(engine))


async def do_clear(bot: Bot, ev: Event, engine: str) -> None:
    try:
        found = await REGISTRY.clear_workdir(ev.user_id, ev.group_id, engine)
    except BackendError as e:
        await bot.send(user_error(e))
        return
    await bot.send(ChatMsg.clear_done(engine) if found else ChatMsg.clear_not_found(engine))


async def do_stop(bot: Bot, ev: Event, engine: str) -> None:
    n = await REGISTRY.cancel(ev.user_id, ev.group_id, engine)
    await bot.send(ChatMsg.stop_done(n))


async def do_queue_list(bot: Bot, ev: Event, engine: str) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send(QueueMsg.NO_SESSION)
        return
    entries = meta.queue.snapshot()
    if not entries:
        await bot.send(QueueMsg.EMPTY)
        return
    running = meta.queue.running()
    running_qid = running.qid if running is not None else None
    records: list[RecordItem] = []
    for e in entries:
        records.append(
            RecordItem(
                f"#{e.qid} {'运行中' if e.qid == running_qid else '排队'}",
                (
                    RecordField("用户", e.uid, code=True),
                    RecordField("等待", f"{e.waited_sec}s"),
                    RecordField("内容", e.preview),
                ),
            )
        )
    md = markdown_records(f"{engine} queue ({len(entries)} 条)", records, footer=QueueMsg.list_hint())
    await send_engine_list(bot, engine, md)


async def do_queue_remove(bot: Bot, ev: Event, engine: str, qid_arg: str) -> None:
    if not qid_arg.isdigit():
        await bot.send(QueueMsg.usage_dequeue())
        return
    qid = int(qid_arg)
    result = await REGISTRY.dequeue(ev.user_id, ev.group_id, engine, qid)
    match result:
        case DequeueNoSession():
            await bot.send(QueueMsg.NO_SESSION)
        case DequeueNotFound(qid=q):
            await bot.send(QueueMsg.not_found(q))
        case DequeueIsRunning(entry=e):
            await bot.send(QueueMsg.is_running(e.qid))
        case DequeueForbidden(entry=e):
            await bot.send(QueueMsg.forbidden(e.qid, e.uid))
        case DequeueOk(entry=e):
            await bot.send(QueueMsg.cancelled(e.qid))
