from __future__ import annotations

import time
import asyncio
from dataclasses import field, dataclass

from acp.schema import RequestPermissionRequest

from ..prompt_queue import QueueEntry, PromptQueue


def _runtime_now() -> float:
    return time.monotonic()


@dataclass(slots=True)
class SessionMeta:
    sid: str
    uid: str
    gid: str | None
    engine: str
    workdir: str
    shared: bool = False
    last_active: float = field(default_factory=_runtime_now)
    # 同 session 多条 prompt 串行排队，详见 prompt_queue.PromptQueue。
    queue: PromptQueue = field(default_factory=PromptQueue)

    @property
    def idle_sec(self) -> int:
        return int(_runtime_now() - self.last_active)

    @property
    def busy(self) -> bool:
        return self.queue.is_busy

    @property
    def queue_depth(self) -> int:
        return self.queue.depth


@dataclass(slots=True)
class PendingApproval:
    """`future.set_result(option_id)` 放行；`set_result(None)` 拒绝。"""

    future: asyncio.Future[str | None]
    request: RequestPermissionRequest


# dequeue 五态结果：上层 match 分发文案，pyright 做穷尽检查
@dataclass(slots=True, frozen=True)
class DequeueOk:
    entry: QueueEntry


@dataclass(slots=True, frozen=True)
class DequeueNotFound:
    qid: int


class DequeueNoSession:
    __slots__ = ()


@dataclass(slots=True, frozen=True)
class DequeueIsRunning:
    entry: QueueEntry


@dataclass(slots=True, frozen=True)
class DequeueForbidden:
    entry: QueueEntry


DequeueResult = DequeueOk | DequeueNotFound | DequeueNoSession | DequeueIsRunning | DequeueForbidden
