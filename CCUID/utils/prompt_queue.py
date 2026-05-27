from __future__ import annotations

import time
import asyncio
from typing import Any
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class QueueEntry:
    qid: int
    task: asyncio.Task[Any]
    uid: str
    preview: str
    enqueued_at: float

    @property
    def waited_sec(self) -> int:
        return int(time.time() - self.enqueued_at)


class PromptQueue:
    """同 session prompt 串行排队。

    协议：调用方 add → 等 `lock` → mark_running → ... → mark_done → remove。
    所有 mutator 要求外层 `SessionRegistry._lock` 已持有，保证 qid 单调与
    _running_qid 一致性不被并发协程撕裂。读方法在 asyncio 单线程模型下原子。
    """

    __slots__ = ("lock", "_entries", "_next_qid", "_running_qid")

    def __init__(self) -> None:
        self.lock: asyncio.Lock = asyncio.Lock()
        # 插入序保留（CPython dict 自 3.7 起有序），snapshot / cancel 按提交顺序遍历
        self._entries: dict[int, QueueEntry] = {}
        self._next_qid: int = 0
        self._running_qid: int | None = None

    @property
    def is_busy(self) -> bool:
        return self.lock.locked()

    @property
    def depth(self) -> int:
        """正在 acquire 上排队的人数（不含正在跑的那条）。"""
        return len(self._entries) - (1 if self._running_qid is not None else 0)

    @property
    def has_entries(self) -> bool:
        return bool(self._entries)

    def get(self, qid: int) -> QueueEntry | None:
        return self._entries.get(qid)

    def running(self) -> QueueEntry | None:
        if self._running_qid is None:
            return None
        return self._entries.get(self._running_qid)

    def snapshot(self) -> list[QueueEntry]:
        return list(self._entries.values())

    def add(self, task: asyncio.Task[Any], uid: str, preview: str) -> QueueEntry:
        qid = self._next_qid
        self._next_qid += 1
        entry = QueueEntry(
            qid=qid,
            task=task,
            uid=uid,
            preview=preview,
            enqueued_at=time.time(),
        )
        self._entries[qid] = entry
        return entry

    def mark_running(self, qid: int) -> None:
        # 不变式：lock 串行 ⇒ 同一时刻最多一个 qid 在跑
        assert qid in self._entries, f"mark_running on unknown qid={qid}"
        assert self._running_qid is None, f"mark_running({qid}) while {self._running_qid} 还在跑"
        self._running_qid = qid

    def mark_done(self, qid: int) -> None:
        # cancel 路径可能已经清过 running，正常 finally 再调一次也应无害
        if self._running_qid == qid:
            self._running_qid = None

    def remove(self, qid: int) -> None:
        # finally 兜底用，幂等：cancel / dequeue 路径可能已经摘掉
        self._entries.pop(qid, None)
        if self._running_qid == qid:
            self._running_qid = None

    def cancel_all_except(
        self,
        current: asyncio.Task[Any] | None,
    ) -> tuple[list[QueueEntry], bool]:
        """把除 `current` 以外的全部 task cancel 并从队列摘除。

        返回 (removed_entries, running_was_cancelled)，后者让调用方决定要不要
        额外向 ACP 子进程发协议级 cancel。
        """
        removed: list[QueueEntry] = []
        running_cancelled = False
        for qid, entry in list(self._entries.items()):
            if entry.task is current:
                continue
            entry.task.cancel()
            del self._entries[qid]
            if self._running_qid == qid:
                self._running_qid = None
                running_cancelled = True
            removed.append(entry)
        return removed, running_cancelled
