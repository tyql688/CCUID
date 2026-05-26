from enum import StrEnum


class GroupMode(StrEnum):
    SOLO = "solo"
    SHARED = "shared"


_ALIASES: dict[str, GroupMode] = {
    "独立": GroupMode.SOLO,
    "solo": GroupMode.SOLO,
    "共享": GroupMode.SHARED,
    "shared": GroupMode.SHARED,
}


def parse_mode(token: str) -> GroupMode | None:
    return _ALIASES.get(token.strip().lower())
