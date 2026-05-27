import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.msgs import ChatMsg, ModelMsg, QueueMsg
from ..utils.output import RenderContext, render
from ..utils.engines import DEFAULT_ENGINE, EngineSpec, resolve, list_engines
from ..utils.session import (
    REGISTRY,
    DequeueOk,
    DequeueNotFound,
    PendingApproval,
    DequeueForbidden,
    DequeueIsRunning,
    DequeueNoSession,
    make_sid,
)
from ..utils.database import CCUIDUserEngine, CCUIDSessionModel, CCUIDSessionNative
from ..utils.attachments import build_prompt

_PREVIEW_MAX = 30


def _make_preview(text: str) -> str:
    """Compact one-line summary shown in queue listings."""
    flat = text.replace("\n", " ").strip()
    if not flat:
        return QueueMsg.PREVIEW_ATTACHMENTS_ONLY
    if len(flat) <= _PREVIEW_MAX:
        return flat
    return flat[:_PREVIEW_MAX] + "…"


async def current_engine(ev: Event) -> str:
    name = await CCUIDUserEngine.get(ev.user_id, ev.group_id)
    return name if name in {e.name for e in list_engines()} else DEFAULT_ENGINE


def _describe_pending(pending: PendingApproval) -> str:
    """Compact one-line summary of what the pending request is about.
    Pulled out so approve / deny messages have a consistent format."""
    parts: list[str] = []
    if pending.tool_kind is not None:
        parts.append(f"[{pending.tool_kind}]")
    if pending.tool_title is not None:
        parts.append(pending.tool_title)
    return " ".join(parts) if parts else ChatMsg.DESC_FALLBACK


async def do_approve(bot: Bot, ev: Event, engine: str, *, always: bool) -> None:
    """User said yes to the oldest pending permission request. Picks the
    matching `allow_once` / `allow_always` option from what the agent offered.
    Fails closed (cancelled) if the agent didn't offer the requested allowance."""
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(ChatMsg.NO_PENDING)
        return
    target = "allow_always" if always else "allow_once"
    chosen_option_id = next((opt.option_id for opt in pending.options if opt.kind == target), None)
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.options)
        pending.future.set_result(None)
        await bot.send(ChatMsg.approve_unavailable(target, offered, desc))
        return
    pending.future.set_result(chosen_option_id)
    await bot.send(ChatMsg.approved(always=always))


async def do_deny(bot: Bot, ev: Event, engine: str) -> None:
    """User said no to the oldest pending request.

    Per ACP, "user rejects this tool" 的精确表达是选 `reject_once`
    PermissionOption — 让 agent 区分用户拒绝 vs 协议级 cancelled。agent 没提
    供 reject_once 才 fallback 到 cancelled。

    `reject_always` 实测 claude-code-acp 不下发，CCUID 也不暴露给用户。"""
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(ChatMsg.NO_PENDING)
        return
    chosen_option_id = next(
        (opt.option_id for opt in pending.options if opt.kind == "reject_once"),
        None,
    )
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.options)
        pending.future.set_result(None)
        await bot.send(ChatMsg.deny_unavailable(offered, desc))
        return
    pending.future.set_result(chosen_option_id)
    await bot.send(ChatMsg.denied())


async def do_chat(bot: Bot, ev: Event, engine: str, prompt: str) -> None:
    blocks = await build_prompt(ev, prompt)
    if not blocks:
        return
    meta, backend = await REGISTRY.get_or_create(ev.user_id, ev.group_id, engine)

    # Lazy resolver：subprocess 在 run_prompt 第一次 yield 时才 spawn，flush 时再查
    def _model_label() -> str | None:
        mid, mname = backend.get_model(meta.sid)
        return mname if mname is not None else mid

    ctx = RenderContext(ev.bot_id, engine, model_resolver=_model_label, workdir=meta.workdir)
    await render(
        bot,
        REGISTRY.run_prompt(
            meta,
            backend,
            blocks,
            submitter_uid=ev.user_id,
            preview=_make_preview(prompt),
        ),
        ctx,
    )


async def do_new(bot: Bot, ev: Event, engine: str) -> None:
    await REGISTRY.restart(ev.user_id, ev.group_id, engine)
    await bot.send(ChatMsg.reset_done(engine))


async def do_clear(bot: Bot, ev: Event, engine: str) -> None:
    found = await REGISTRY.clear_workdir(ev.user_id, ev.group_id, engine)
    await bot.send(ChatMsg.clear_done(engine) if found else ChatMsg.clear_not_found(engine))


async def do_stop(bot: Bot, ev: Event, engine: str) -> None:
    n = await REGISTRY.cancel(ev.user_id, ev.group_id, engine)
    await bot.send(ChatMsg.stop_done(n))


async def do_queue_list(bot: Bot, ev: Event, engine: str) -> None:
    """列出当前 session 的整条队列（含正在跑的那条，加 `*` 标记）。"""
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
    lines = [QueueMsg.header(engine, len(entries))]
    for e in entries:
        mark = "*" if e.qid == running_qid else " "
        lines.append(f"{mark} #{e.qid} {e.uid} {e.waited_sec}s — {e.preview}")
    lines.append(QueueMsg.list_hint())
    await bot.send("\n".join(lines))


async def do_queue_remove(bot: Bot, ev: Event, engine: str, qid_arg: str) -> None:
    """删队列里的一条 prompt（拒绝跑中的、拒绝别人的）。"""
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


async def do_arm_yolo(bot: Bot, ev: Event, engine: str) -> None:
    """给当前 (uid, gid, engine) session 打一次性 yolo flag：下一条 prompt 期间
    所有 ACP permission 都按 allow_always 走。"""
    meta, _ = await REGISTRY.get_or_create(ev.user_id, ev.group_id, engine)
    meta.next_prompt_auto_approve = True
    await bot.send(ChatMsg.yolo_armed(engine))


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
    lines = [ChatMsg.ENGINES_HEADER]
    for i, e in enumerate(list_engines(), 1):
        mark = "*" if e.name == cur else " "
        lines.append(f"{i}.{mark} {e.display} · {await _engine_status(e, ev)}")
    await bot.send("\n".join(lines))


async def do_engine_set(bot: Bot, ev: Event, token: str) -> None:
    target = resolve(token)
    if target is None:
        return
    await CCUIDUserEngine.set(ev.user_id, ev.group_id, target.name)
    await bot.send(ChatMsg.engine_set(target.name))


def _resolve_model(token: str, available: tuple[tuple[str, str], ...]) -> tuple[str, str] | None:
    """支持 model_id 精确 / 1-based 序号 / name 大小写无关子串。子串多命中算失败，
    用户得写得更具体；列里没有 model_id 精确同字串时 substring 兜底。"""
    low = token.strip().lower()
    for mid, name in available:
        if low in (mid.lower(), name.lower()):
            return mid, name
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(available):
            return available[idx]
    matches = [(mid, name) for mid, name in available if low in mid.lower() or low in name.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


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
    lines = [ModelMsg.header(engine, len(available))]
    for i, (mid, name) in enumerate(available, 1):
        mark = "*" if mid == cur_id else " "
        lines.append(f"{i}.{mark} {name} · {mid}")
    lines.append(ModelMsg.list_hint())
    await bot.send("\n".join(lines))


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
        await bot.send(ModelMsg.switch_failed(str(e)))
        return
    if switched is None:
        await bot.send(ModelMsg.not_found(token))
        return
    new_id, new_name = switched
    await CCUIDSessionModel.store(meta.sid, new_id)
    await bot.send(ModelMsg.switched(new_id, new_name))
