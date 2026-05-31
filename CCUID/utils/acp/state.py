from __future__ import annotations

from acp.schema import (
    SessionModeState,
    SessionModelState,
    SessionConfigSelectGroup,
    SessionConfigOptionSelect,
    SessionConfigOptionBoolean,
)


def _extract_models(
    state: SessionModelState | None,
) -> tuple[str | None, str | None, tuple[tuple[str, str], ...]]:
    """label 直接用 selected.name，agent 给什么就显示什么。"""
    if state is None:
        return None, None, ()
    available = tuple((m.model_id, m.name) for m in state.available_models)
    cur_name = next((name for mid, name in available if mid == state.current_model_id), None)
    return state.current_model_id, cur_name, available


def _select_values(option: SessionConfigOptionSelect) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for item in option.options:
        if isinstance(item, SessionConfigSelectGroup):
            values.extend((sub.value, sub.name) for sub in item.options)
        else:
            values.append((item.value, item.name))
    return tuple(values)


def _find_config_option(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
    *,
    category: str | None = None,
    option_id: str | None = None,
) -> SessionConfigOptionSelect | SessionConfigOptionBoolean | None:
    for option in options:
        if option_id is not None and option.id == option_id:
            return option
        if category is not None and option.category == category:
            return option
    return None


def _extract_model_config(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
) -> tuple[str | None, str | None, tuple[tuple[str, str], ...], str | None]:
    option = _find_config_option(options, category="model") or _find_config_option(options, option_id="model")
    if not isinstance(option, SessionConfigOptionSelect):
        return None, None, (), None
    available = _select_values(option)
    cur_name = next((name for value, name in available if value == option.current_value), None)
    return option.current_value, cur_name, available, option.id


def _extract_modes(
    state: SessionModeState | None,
) -> tuple[str | None, tuple[tuple[str, str, str | None], ...]]:
    if state is None:
        return None, ()
    available = tuple((m.id, m.name, m.description) for m in state.available_modes)
    return state.current_mode_id, available


def _extract_mode_config(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
) -> tuple[str | None, tuple[tuple[str, str, str | None], ...], str | None]:
    option = _find_config_option(options, category="mode") or _find_config_option(options, option_id="mode")
    if not isinstance(option, SessionConfigOptionSelect):
        return None, (), None
    available = tuple((value, name, None) for value, name in _select_values(option))
    return option.current_value, available, option.id
