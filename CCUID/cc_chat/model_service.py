from __future__ import annotations

import shutil
from typing import TypeVar
from collections.abc import Callable

from acp.schema import (
    SessionMode,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionConfigOptionBoolean,
)

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import current_engine, send_engine_list
from ..utils.msgs import ChatMsg, ModeMsg, ModelMsg
from ..utils.errors import user_error
from ..utils.engines import EngineSpec, resolve, list_engines
from ..utils.database import CCUIDUserEngine, CCUIDSessionModel, CCUIDSessionNative
from ..utils.acp.state import ConfigOption, _select_values, _find_config_option
from ..cc_config.prefix import cc_prefix
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
_TRUE_VALUES = {"1", "true", "on", "yes", "开", "开启"}
_FALSE_VALUES = {"0", "false", "off", "no", "关", "关闭"}


async def _engine_status(spec: EngineSpec, ev: Event) -> str:
    launcher = spec.cmd[0]
    head = "ok" if shutil.which(launcher) else "missing"

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


def _resolve_model(
    token: str,
    available: tuple[SessionConfigSelectOption, ...],
) -> SessionConfigSelectOption | None:
    return _resolve_indexed(token, available, lambda model: model.value, lambda model: model.name)


def _resolve_mode(
    token: str,
    available: tuple[SessionMode, ...],
) -> SessionMode | None:
    return _resolve_indexed(token, available, lambda mode: mode.id, lambda mode: mode.name)


def _resolve_config_value(option: ConfigOption, token: str) -> str | bool | None:
    if isinstance(option, SessionConfigOptionBoolean):
        value = token.strip().lower()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
        return None
    selected = _resolve_indexed(
        token,
        _select_values(option),
        lambda item: item.value,
        lambda item: item.name,
    )
    return None if selected is None else selected.value


def _config_value_text(value: str | bool) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


def _config_choices(option: ConfigOption) -> str:
    if isinstance(option, SessionConfigOptionBoolean):
        return "on / off"
    return "；".join(f"#{i} {item.name} ({item.value})" for i, item in enumerate(_select_values(option), 1))


async def do_config(
    bot: Bot,
    ev: Event,
    engine: str,
    key: str | None = None,
    token: str | None = None,
) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    if meta is None:
        await bot.send("session 未启动，先发条 prompt 让 agent 起来再查 configOptions")
        return
    backend = REGISTRY.backend(engine)
    options = backend.list_config_options(meta.sid)
    if not options:
        await bot.send("当前 engine 没返回 configOptions")
        return
    option: ConfigOption | None = None
    if key is not None:
        option = _find_config_option(options, key)
        if option is None:
            await bot.send(f"找不到 configOption: {key}")
            return
    if option is None or token is None or token == "":
        shown = options if option is None else (option,)
        records = [
            RecordItem(
                f"#{i} {item.name}",
                (
                    RecordField("ID", item.id, code=True),
                    RecordField("类型", item.type),
                    RecordField("当前", _config_value_text(item.current_value), code=True),
                    RecordField("可选", _config_choices(item)),
                    RecordField("说明", item.description),
                ),
            )
            for i, item in enumerate(shown, 1)
        ]
        md = markdown_records(
            f"{engine} configOptions ({len(shown)} 个)",
            records,
            footer=f"→ {cc_prefix()}config <id> <value> 修改；effort / fast 是快捷入口",
        )
        await send_engine_list(bot, engine, md)
        return
    value = _resolve_config_value(option, token)
    if value is None:
        await bot.send(f"{option.id} 不支持值: {token}\n用 {cc_prefix()}config {option.id} 查看可选值")
        return
    try:
        updated = await backend.set_config_option(meta.sid, option.id, value)
    except BackendError as e:
        await bot.send(f"修改 config 失败: {user_error(e)}")
        return
    if updated is None:
        await bot.send(f"找不到 configOption: {option.id}")
        return
    if isinstance(updated, SessionConfigOptionSelect) and (updated.id == "model" or updated.category == "model"):
        await CCUIDSessionModel.store(meta.sid, updated.current_value)
    await bot.send(f"✓ {updated.name} ({updated.id}): {_config_value_text(updated.current_value)}")


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
        rows.append(["✓" if model.value == cur_id else "", i, model.name, model.value])
    md = f"## {engine} model ({len(available)} 个)\n\n"
    md += markdown_table(["当前", "#", "名称", "ID"], rows)
    model_config = _find_config_option(backend.list_config_options(meta.sid), "model")
    if isinstance(model_config, SessionConfigOptionSelect):
        md += f"\n\n{ModelMsg.list_hint()}"
    else:
        md += "\n\n当前 engine 仅提供只读 model 信息"
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
    model_config = _find_config_option(backend.list_config_options(meta.sid), "model")
    if not isinstance(model_config, SessionConfigOptionSelect):
        await bot.send("当前 engine 的 model 信息只读，不能切换")
        return
    resolved = _resolve_model(token, available)
    if resolved is None:
        await bot.send(ModelMsg.not_found(token))
        return
    target_id = resolved.value
    try:
        switched = await backend.set_model(meta.sid, target_id)
    except BackendError as e:
        await bot.send(ModelMsg.switch_failed(user_error(e)))
        return
    if switched is None:
        await bot.send(ModelMsg.not_found(token))
        return
    await CCUIDSessionModel.store(meta.sid, switched.value)
    await bot.send(ModelMsg.switched(switched.value, switched.name))


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
