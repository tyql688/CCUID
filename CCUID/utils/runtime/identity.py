from __future__ import annotations

import re

_SAFE = re.compile(r"[^a-zA-Z0-9_\-]")
_PART_MAX = 48


def _part(value: str) -> str:
    part = _SAFE.sub("_", value)[:_PART_MAX]
    return part if part else "x"


def make_sid(uid: str, gid: str | None, engine: str, *, shared: bool = False) -> str:
    user = "shared" if shared else _part(uid)
    group = "dm" if gid is None else _part(gid)
    return f"{user}-{group}-{_part(engine)}"
