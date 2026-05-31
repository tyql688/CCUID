from __future__ import annotations

import time
import asyncio
import contextlib
from collections.abc import AsyncIterator

from gsuid_core.logger import logger

from ..mode import GroupMode
from .models import (
    DequeueOk,
    SessionMeta,
    DequeueResult,
    DequeueNotFound,
    PendingApproval,
    DequeueForbidden,
    DequeueIsRunning,
    DequeueNoSession,
    _runtime_now,
)
from .workdir import clear_workdir_contents
from ..engines import get_engine
from .identity import make_sid
from ..database import CCUIDGrantGroup, CCUIDSessionModel, CCUIDSessionNative
from ..acp.types import RequestPermissionRequest
from ..acp.prompt import PromptBlock
from ..acp.backend import ACPBackend, BackendError
from ...cc_config.cc_config import CCUIDConfig
from ..resource.RESOURCE_PATH import WORKDIR_ROOT

_CLOSING_POLL_SEC = 0.05
_CLEANUP_INTERVAL_SEC = 300
_MAX_CONCURRENT_SESSIONS = 16


class SessionRegistry:
    def __init__(self) -> None:
        self._meta: dict[str, SessionMeta] = {}
        self._backends: dict[str, ACPBackend] = {}
        self._closing: set[str] = set()
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._pending: dict[str, list[PendingApproval]] = {}

    def backend(self, engine: str) -> ACPBackend:
        if engine not in self._backends:
            self._backends[engine] = ACPBackend(get_engine(engine), self)
        return self._backends[engine]

    def _resolve_pending(self, sid: str) -> int:
        pending = self._pending.pop(sid, [])
        for item in pending:
            if not item.future.done():
                item.future.set_result(None)
        return len(pending)

    def _can_recycle(self, meta: SessionMeta) -> bool:
        return not self._is_active(meta)

    def _is_active(self, meta: SessionMeta) -> bool:
        return meta.queue.has_entries or self.backend(meta.engine).is_session_busy(meta.sid)

    def _select_lru_victim(self) -> SessionMeta | None:
        candidates = [m for m in self._meta.values() if self._can_recycle(m)]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x.last_active)

    def _expired_recyclable_sessions(self, *, now: float, timeout: int) -> list[SessionMeta]:
        return [m for m in self._meta.values() if self._can_recycle(m) and now - m.last_active > timeout]

    async def _shared(self, gid: str | None) -> bool:
        if gid is None:
            return False
        return (await CCUIDGrantGroup.get_mode(gid)) is GroupMode.SHARED

    async def get_or_create(self, uid: str, gid: str | None, engine: str) -> tuple[SessionMeta, ACPBackend]:
        get_engine(engine)
        shared = await self._shared(gid)
        sid = make_sid(uid, gid, engine, shared=shared)
        while True:
            victim: SessionMeta | None = None
            result: tuple[SessionMeta, ACPBackend] | None = None
            async with self._lock:
                if sid not in self._closing:
                    meta = self._meta.get(sid)
                    if meta is None:
                        if len(self._meta) >= _MAX_CONCURRENT_SESSIONS:
                            victim = self._select_lru_victim()
                            if victim is not None:
                                self._meta.pop(victim.sid, None)
                                self._closing.add(victim.sid)
                            else:
                                raise BackendError("活跃 session 已满，所有 session 都在执行中，稍后再试")
                        meta = SessionMeta(
                            sid=sid,
                            uid=uid,
                            gid=gid,
                            engine=engine,
                            workdir=str(WORKDIR_ROOT / sid),
                            shared=shared,
                        )
                        self._meta[sid] = meta
                    meta.last_active = _runtime_now()
                    result = (meta, self.backend(engine))
            if result:
                if victim:
                    # LRU 淘汰：关子进程但留 native_id，被淘汰的用户下条 prompt 仍能 resume。
                    await self._finish_close(victim, drop_native=False)
                return result
            await asyncio.sleep(_CLOSING_POLL_SEC)

    async def workdir_for(self, uid: str, gid: str | None, engine: str) -> str:
        shared = await self._shared(gid)
        sid = make_sid(uid, gid, engine, shared=shared)
        return str(WORKDIR_ROOT / sid)

    async def run_prompt(
        self,
        meta: SessionMeta,
        backend: ACPBackend,
        blocks: list[PromptBlock],
        *,
        submitter_uid: str,
        preview: str,
    ) -> AsyncIterator[object]:
        # BusyBehavior: queue（默认）= 抢 lock 串行；reject = 忙就直接报错
        behavior: str = CCUIDConfig.get_config("BusyBehavior").data
        if behavior == "reject" and meta.queue.is_busy:
            raise BackendError("session 忙，已拒绝（队列模式可在配置改）")

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run_prompt 必须运行在 asyncio task 里")

        async with self._lock:
            if self._meta.get(meta.sid) is not meta:
                raise BackendError("session closed")
            entry = meta.queue.add(task, submitter_uid, preview)

        try:
            async with meta.queue.lock:
                async with self._lock:
                    if self._meta.get(meta.sid) is not meta:
                        raise BackendError("session closed")
                    meta.queue.mark_running(entry.qid)
                    meta.last_active = _runtime_now()

                resume = await CCUIDSessionNative.fetch(meta.sid)
                try:
                    async for ev in backend.prompt(meta.sid, meta.workdir, blocks, resume_id=resume):
                        yield ev
                finally:
                    native = backend.get_native_session_id(meta.sid)
                    alive = False
                    async with self._lock:
                        if self._meta.get(meta.sid) is meta:
                            meta.queue.mark_done(entry.qid)
                            meta.last_active = _runtime_now()
                            alive = True
                    if alive and native:
                        await CCUIDSessionNative.store(meta.sid, native)
        finally:
            async with self._lock:
                meta.queue.remove(entry.qid)

    async def cancel(self, uid: str, gid: str | None, engine: str) -> int:
        """一次性砍掉 (uid, gid, engine) 对应所有 session 的全部 prompt。

        `cc 停` 自己跑在另一条 task 上——cancel_all_except(current) 把它排除掉。
        """
        shared = await self._shared(gid)
        current = asyncio.current_task()
        notify: list[SessionMeta] = []
        count = 0
        async with self._lock:
            for m in self._meta.values():
                if m.gid != gid or m.engine != engine:
                    continue
                if not (shared or m.uid == uid):
                    continue
                self._resolve_pending(m.sid)
                if not m.queue.has_entries:
                    continue
                removed, running_cancelled = m.queue.cancel_all_except(current)
                count += len(removed)
                if running_cancelled:
                    notify.append(m)
        for m in notify:
            await self.backend(m.engine).cancel(m.sid)
        return count

    async def dequeue(
        self,
        uid: str,
        gid: str | None,
        engine: str,
        qid: int,
    ) -> DequeueResult:
        """删队列里的一条 prompt（不会动正在跑的那条）。"""
        sid = make_sid(uid, gid, engine, shared=await self._shared(gid))
        async with self._lock:
            meta = self._meta.get(sid)
            if meta is None:
                return DequeueNoSession()
            entry = meta.queue.get(qid)
            if entry is None:
                return DequeueNotFound(qid=qid)
            running = meta.queue.running()
            if running is not None and running.qid == qid:
                return DequeueIsRunning(entry=entry)
            if entry.uid != uid:
                return DequeueForbidden(entry=entry)
            entry.task.cancel()
            meta.queue.remove(qid)
            return DequeueOk(entry=entry)

    async def restart(self, uid: str, gid: str | None, engine: str) -> None:
        sid = make_sid(uid, gid, engine, shared=await self._shared(gid))
        async with self._lock:
            target = self._meta.pop(sid, None)
            self._resolve_pending(sid)
            if target:
                self._closing.add(sid)
        if target:
            await self._finish_close(target, drop_native=True)
        else:
            await CCUIDSessionNative.drop(sid)

    async def clear_workdir(self, uid: str, gid: str | None, engine: str) -> bool:
        """只擦 workdir 内容，不动 session。返回 workdir 是否存在。"""
        sid = make_sid(uid, gid, engine, shared=await self._shared(gid))
        async with self._lock:
            meta = self._meta.get(sid)
            if meta is not None and self._is_active(meta):
                raise BackendError("session 正在执行，先 stop 或 new 后再 clear")
        return await clear_workdir_contents(str(WORKDIR_ROOT / sid))

    async def bind_native_session(self, uid: str, gid: str | None, engine: str, native_id: str) -> None:
        shared = await self._shared(gid)
        sid = make_sid(uid, gid, engine, shared=shared)
        async with self._lock:
            meta = self._meta.get(sid)
            if meta is not None and self._is_active(meta):
                raise BackendError("session 正在执行，先 stop 或 new 后再 resume")
            if meta is not None:
                self._meta.pop(sid, None)
                self._resolve_pending(sid)
                self._closing.add(sid)
        if meta is not None:
            await self._finish_close(meta, drop_native=False)
        await CCUIDSessionNative.store(sid, native_id)
        await CCUIDSessionModel.drop(sid)

    async def _finish_close(self, meta: SessionMeta, *, drop_native: bool) -> None:
        try:
            await self._close_backend_session(meta)
            if drop_native:
                await CCUIDSessionNative.drop(meta.sid)
        finally:
            async with self._lock:
                self._closing.discard(meta.sid)

    async def _close_backend_session(self, meta: SessionMeta) -> None:
        try:
            await self.backend(meta.engine).close(meta.sid)
        except Exception:
            logger.exception(f"[CCUID] close failed: {meta.sid}")

    def list_sessions(self) -> list[SessionMeta]:
        return list(self._meta.values())

    def register_pending(
        self,
        sid: str,
        request: RequestPermissionRequest,
    ) -> asyncio.Future[str | None]:
        """Create + register a future for an `ask`-mode permission request."""
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._pending.setdefault(sid, []).append(
            PendingApproval(
                future=future,
                request=request,
            )
        )
        return future

    def cancel_pending(self, sid: str, future: asyncio.Future[str | None]) -> None:
        """Remove a pending entry (used on timeout). Future itself is not resolved here."""
        plist = self._pending.get(sid)
        if not plist:
            return
        for i, p in enumerate(plist):
            if p.future is future:
                plist.pop(i)
                if not plist:
                    self._pending.pop(sid, None)
                return

    async def take_pending(self, uid: str, gid: str | None, engine: str) -> PendingApproval | None:
        """Return + remove the oldest pending approval for (uid, gid, engine)."""
        shared = await self._shared(gid)
        sid = make_sid(uid, gid, engine, shared=shared)
        plist = self._pending.get(sid)
        if not plist:
            return None
        pending = plist.pop(0)
        if not plist:
            self._pending.pop(sid, None)
        return pending

    def find(self, uid: str, gid: str | None, engine: str) -> SessionMeta | None:
        for m in self._meta.values():
            if m.engine == engine and m.gid == gid and (m.uid == uid or m.shared):
                return m
        return None

    async def start_cleanup(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        await self._reap_expired()
        self._cleanup_task = asyncio.get_running_loop().create_task(self._loop(), name="CCUID-cleanup")

    async def shutdown(self) -> None:
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            if not cleanup_task.done():
                cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        async with self._lock:
            targets = list(self._meta.values())
            self._meta.clear()
            self._closing.update(m.sid for m in targets)
            for meta in targets:
                self._resolve_pending(meta.sid)
        for meta in targets:
            await self._finish_close(meta, drop_native=False)
        for backend in self._backends.values():
            await backend.close_all()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
                timeout = int(CCUIDConfig.get_config("IdleTimeoutSec").data)
                now = _runtime_now()
                async with self._lock:
                    targets = self._expired_recyclable_sessions(now=now, timeout=timeout)
                    for m in targets:
                        self._meta.pop(m.sid, None)
                    self._closing.update(m.sid for m in targets)
                for meta in targets:
                    await self._finish_close(meta, drop_native=True)
                await self._reap_expired()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[CCUID] cleanup failed")

    async def _reap_expired(self) -> None:
        """超时 native_id 从 DB drop 掉，agent 下次拿不到旧 session_id resume。"""
        if not WORKDIR_ROOT.exists():
            return
        soft_sec = int(CCUIDConfig.get_config("IdleTimeoutSec").data)
        if soft_sec <= 0:
            return
        now = time.time()
        async with self._lock:
            live = set(self._meta) | self._closing
        for entry in WORKDIR_ROOT.iterdir():
            if not entry.is_dir() or entry.name in live:
                continue
            updated_at = await CCUIDSessionNative.fetch_updated_at(entry.name)
            if updated_at is not None and updated_at > 0 and now - updated_at > soft_sec:
                await CCUIDSessionNative.drop(entry.name)
                idle = int(now - updated_at)
                logger.debug(f"[CCUID] session 软过期(上下文已丢弃): {entry.name} idle={idle}s")


REGISTRY = SessionRegistry()
