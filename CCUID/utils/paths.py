from __future__ import annotations

from pathlib import Path


def _normalized_path(value: str) -> Path | None:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None


def same_path(a: str | None, b: str) -> bool:
    if a is None or a == "":
        return False
    left = _normalized_path(a)
    right = _normalized_path(b)
    return left is not None and right is not None and left == right
