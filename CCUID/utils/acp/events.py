from __future__ import annotations

from acp.schema import RequestPermissionRequest

from .policy import PermissionMode


class PermissionEvent(RequestPermissionRequest):
    """向用户呈现一个 agent 权限请求。

    `decision`=当时生效的 PermissionMode（意图）；`matched`=agent 是否真的给了匹配选项。
    其余字段直接继承 ACP `RequestPermissionRequest`。"""

    decision: PermissionMode
    matched: bool = True
