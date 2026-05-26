import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.output import RenderContext, render
from ..utils.engines import DEFAULT_ENGINE, EngineSpec, resolve, list_engines
from ..utils.session import REGISTRY, PendingApproval, make_sid
from ..utils.database import CCUIDUserEngine, CCUIDSessionNative
from ..utils.attachments import build_prompt


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
    return " ".join(parts) if parts else "(无描述)"


_NO_PENDING_HINT = "当前没有待审批的权限请求\n如果刚发过命令，agent 可能还在思考"


async def do_approve(bot: Bot, ev: Event, engine: str, *, always: bool) -> None:
    """User said yes to the oldest pending permission request. Picks the
    matching `allow_once` / `allow_always` option from what the agent offered.
    Fails closed (cancelled) if the agent didn't offer the requested allowance."""
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(_NO_PENDING_HINT)
        return
    target = "allow_always" if always else "allow_once"
    chosen_option_id = next((opt.option_id for opt in pending.options if opt.kind == target), None)
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.options)
        pending.future.set_result(None)
        await bot.send(f"agent 未提供 {target}（仅 {offered}）\n→ 已发出协议级 cancelled · {desc}")
        return
    pending.future.set_result(chosen_option_id)
    if always:
        head, note = "已永久允许", "agent 缓存放行，同类操作不再询问"
    else:
        head, note = "已允许", "agent 已收到放行，正在继续执行"
    await bot.send(f"✓ {head}  {desc}\n{note}")


async def do_deny(bot: Bot, ev: Event, engine: str) -> None:
    """User said no to the oldest pending request.

    Per ACP, "user rejects this tool" 的精确表达是选 `reject_once`
    PermissionOption — 让 agent 区分用户拒绝 vs 协议级 cancelled。agent 没提
    供 reject_once 才 fallback 到 cancelled。

    `reject_always` 实测 claude-code-acp 不下发，CCUID 也不暴露给用户。"""
    pending = await REGISTRY.take_pending(ev.user_id, ev.group_id, engine)
    if pending is None:
        await bot.send(_NO_PENDING_HINT)
        return
    chosen_option_id = next(
        (opt.option_id for opt in pending.options if opt.kind == "reject_once"),
        None,
    )
    desc = _describe_pending(pending)
    if chosen_option_id is None:
        offered = ", ".join(opt.kind for opt in pending.options)
        pending.future.set_result(None)
        await bot.send(f"agent 未提供 reject_once（仅 {offered}）\n→ 已发出协议级 cancelled · {desc}")
        return
    pending.future.set_result(chosen_option_id)
    await bot.send(f"✗ 已拒绝  {desc}\nagent 将放弃此操作")


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
    await render(bot, REGISTRY.run_prompt(meta, backend, blocks), ctx)


async def do_new(bot: Bot, ev: Event, engine: str) -> None:
    await REGISTRY.restart(ev.user_id, ev.group_id, engine)
    await bot.send(f"{engine}: 已重置")


async def do_stop(bot: Bot, ev: Event, engine: str) -> None:
    n = await REGISTRY.cancel(ev.user_id, ev.group_id, engine)
    await bot.send(f"已打断 {n} 个任务")


async def do_arm_yolo(bot: Bot, ev: Event, engine: str) -> None:
    """给当前 (uid, gid, engine) session 打一次性 yolo flag：下一条 prompt 期间
    所有 ACP permission 都按 allow_always 走。"""
    meta, _ = await REGISTRY.get_or_create(ev.user_id, ev.group_id, engine)
    meta.next_prompt_auto_approve = True
    await bot.send(f"{engine}: 下条 prompt 自动放行所有权限（一次性）")


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
    lines = ["**CCUID Engines**"]
    for i, e in enumerate(list_engines(), 1):
        mark = "*" if e.name == cur else " "
        lines.append(f"{i}.{mark} {e.display} · {await _engine_status(e, ev)}")
    await bot.send("\n".join(lines))


async def do_engine_set(bot: Bot, ev: Event, token: str) -> None:
    target = resolve(token)
    if target is None:
        return
    await CCUIDUserEngine.set(ev.user_id, ev.group_id, target.name)
    await bot.send(f"engine: {target.name}")
