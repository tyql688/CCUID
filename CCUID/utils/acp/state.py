from __future__ import annotations

from acp.schema import (
    ModelInfo,
    SessionMode,
    SessionModeState,
    SessionModelState,
    SessionConfigSelectGroup,
    SessionConfigOptionSelect,
    SessionConfigOptionBoolean,
)


def _extract_models(
    state: SessionModelState | None,
) -> tuple[str | None, str | None, tuple[ModelInfo, ...]]:
    """按 current_model_id 在 available_models 里查出显示名；查不到返回 None。"""
    if state is None:
        return None, None, ()
    available = tuple(state.available_models)
    cur_name = next((model.name for model in available if model.model_id == state.current_model_id), None)
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


def _find_config_select(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
    key: str,
) -> SessionConfigOptionSelect | None:
    option = _find_config_option(options, category=key)
    if option is None:
        option = _find_config_option(options, option_id=key)
    return option if isinstance(option, SessionConfigOptionSelect) else None


def _extract_model_config(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
) -> tuple[str | None, str | None, tuple[ModelInfo, ...], str | None]:
    option = _find_config_select(options, "model")
    if option is None:
        return None, None, (), None
    available = tuple(ModelInfo(model_id=value, name=name) for value, name in _select_values(option))
    cur_name = next((model.name for model in available if model.model_id == option.current_value), None)
    return option.current_value, cur_name, available, option.id


def _extract_modes(
    state: SessionModeState | None,
) -> tuple[str | None, tuple[SessionMode, ...]]:
    if state is None:
        return None, ()
    available = tuple(state.available_modes)
    return state.current_mode_id, available


def _extract_mode_config(
    options: tuple[SessionConfigOptionSelect | SessionConfigOptionBoolean, ...],
) -> tuple[str | None, tuple[SessionMode, ...], str | None]:
    option = _find_config_select(options, "mode")
    if option is None:
        return None, (), None
    available = tuple(SessionMode(id=value, name=name) for value, name in _select_values(option))
    return option.current_value, available, option.id
