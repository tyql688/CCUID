from __future__ import annotations

from acp.schema import (
    SessionConfigSelectGroup,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionConfigOptionBoolean,
)

ConfigOption = SessionConfigOptionSelect | SessionConfigOptionBoolean


def _select_values(option: SessionConfigOptionSelect) -> tuple[SessionConfigSelectOption, ...]:
    values: list[SessionConfigSelectOption] = []
    for item in option.options:
        if isinstance(item, SessionConfigSelectGroup):
            values.extend(item.options)
        else:
            values.append(item)
    return tuple(values)


def _find_config_option(
    options: tuple[ConfigOption, ...],
    key: str,
) -> ConfigOption | None:
    for option in options:
        if option.id == key:
            return option
    for option in options:
        if option.category == key:
            return option
    return None


def _find_config_select(
    options: tuple[ConfigOption, ...],
    key: str,
) -> SessionConfigOptionSelect | None:
    option = _find_config_option(options, key)
    return option if isinstance(option, SessionConfigOptionSelect) else None
