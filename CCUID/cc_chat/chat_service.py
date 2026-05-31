from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.msgs import ChatMsg, QueueMsg
from ..utils.errors import user_error
from ..utils.output import render
from ..utils.runtime import REGISTRY, PendingApproval
from ..utils.acp.backend import BackendError
from ..utils.attachments import build_prompt
from ..utils.presentation.delivery import RenderContext


def _make_preview(text: str) -> str:
    preview = text.strip()
    if not preview:
        return QueueMsg.PREVIEW_ATTACHMENTS_ONLY
    return preview


def _describe_pending(pending: PendingApproval) -> str:
    parts: list[str] = []
    tool_call = pending.request.tool_call
    if tool_call.kind is not None:
        parts.append(f"[{tool_call.kind}]")
    if tool_call.title is not None:
        parts.append(tool_call.title)
    return " ".join(parts) if parts else ChatMsg.DESC_FALLBACK


async def do_approve(bot: Bot, ev: Event, engine: str, *, always: bool) -> None:
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(ChatMsg.NO_PENDING)
        return
    target = "allow_always" if always else "allow_once"
    chosen_option_id = next((opt.option_id for opt in pending.request.options if opt.kind == target), None)
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.request.options)
        pending.future.set_result(None)
        await bot.send(ChatMsg.approve_unavailable(target, offered, desc))
        return
    pending.future.set_result(chosen_option_id)
    await bot.send(ChatMsg.approved(always=always))


async def do_deny(bot: Bot, ev: Event, engine: str) -> None:
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(ChatMsg.NO_PENDING)
        return
    chosen_option_id = next(
        (opt.option_id for opt in pending.request.options if opt.kind == "reject_once"),
        None,
    )
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.request.options)
        pending.future.set_result(None)
        await bot.send(ChatMsg.deny_unavailable(offered, desc))
        return
    pending.future.set_result(chosen_option_id)
    await bot.send(ChatMsg.denied())


async def do_chat(bot: Bot, ev: Event, engine: str, prompt: str) -> None:
    prompt_result = await build_prompt(ev, prompt)
    if prompt_result.warnings:
        await bot.send("\n".join(prompt_result.warnings))
    blocks = prompt_result.blocks
    if not blocks:
        return
    try:
        meta, backend = await REGISTRY.get_or_create(ev.user_id, ev.group_id, engine)
    except BackendError as e:
        await bot.send(user_error(e))
        return

    def _model_label() -> str | None:
        mid, mname = backend.get_model(meta.sid)
        return mname if mname is not None else mid

    def _usage_for_footer():
        return backend.snapshot_usage(meta.sid)

    def _elapsed_for_header():
        return backend.snapshot_elapsed(meta.sid)

    ctx = RenderContext(
        ev.bot_id,
        engine,
        model_resolver=_model_label,
        workdir=meta.workdir,
        elapsed_resolver=_elapsed_for_header,
    )
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
        usage_provider=_usage_for_footer,
    )
