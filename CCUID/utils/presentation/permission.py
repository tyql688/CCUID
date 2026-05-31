from __future__ import annotations

from typing import Literal
from dataclasses import dataclass

from ..acp.permission import PermissionMode


@dataclass(slots=True, frozen=True, kw_only=True)
class PermissionDisplay:
    label: str
    state: Literal["pending", "allowed", "denied"]
    unmatched_text: str | None = None


def clean_permission_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    text = summary.strip()
    if text.startswith("text: "):
        text = text[6:].strip()
    if text.startswith("Not in allowlist:"):
        detail = text.removeprefix("Not in allowlist:").strip()
        text = f"不在允许列表：{detail}" if detail else "不在允许列表"
    return text


def permission_display(decision: PermissionMode, *, matched: bool) -> PermissionDisplay:
    if decision == "ask":
        return PermissionDisplay(label="待审核", state="pending")
    if not matched:
        return PermissionDisplay(label="已取消", state="denied", unmatched_text=f"策略 {decision} 没有匹配选项，已取消")
    if decision == "allow_once":
        return PermissionDisplay(label="已自动允许", state="allowed")
    if decision == "allow_always":
        return PermissionDisplay(label="已自动永久允许", state="allowed")
    if decision == "reject_once":
        return PermissionDisplay(label="已自动拒绝", state="denied")
    raise ValueError(f"unhandled PermissionMode: {decision!r}")
