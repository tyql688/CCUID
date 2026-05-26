import re
import time
import shutil
import asyncio
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from acp.schema import PermissionOption

from gsuid_core.logger import logger

from .mode import GroupMode
from .engines import get_engine
from .database import CCUIDGrantGroup, CCUIDSessionNative
from .acp.backend import ACPBackend, BackendError
from ..cc_config.cc_config import CCUIDConfig
from .resource.RESOURCE_PATH import WORKDIR_ROOT

_SAFE = re.compile(r"[^a-zA-Z0-9_\-]")
_PART_MAX = 48
_CLOSING_POLL_SEC = 0.05
_CLEANUP_INTERVAL_SEC = 300
_DAY_SEC = 86400
_MAX_CONCURRENT_SESSIONS = 16
_MAX_WORKDIR_AGE_DAYS = 7


async def _purge_workdir(workdir: str) -> None:
    p = Path(workdir)
    if not p.exists():
        return
    # shutil.rmtree(ignore_errors=True) 自己已经吞了 OSError，外层 try 多余
    await asyncio.to_thread(shutil.rmtree, p, ignore_errors=True)


def _part(value: str) -> str:
    part = _SAFE.sub("_", value)[:_PART_MAX]
    return part if part else "x"


def make_sid(uid: str, gid: str | None, engine: str, *, shared: bool = False) -> str:
    user = "shared" if shared else _part(uid)
    group = "dm" if gid is None else _part(gid)
    return f"{user}-{group}-{_part(engine)}"


@dataclass(slots=True)
class SessionMeta:
    sid: str
    uid: str
    gid: str | None
    engine: str
    workdir: str
    shared: bool = False
    last_active: float = field(default_factory=time.time)
    busy: bool = False
    # prompt_lock 让同一 session 的多条 prompt 串行（queue 模式）。
    # waiters 记下排队中 + 正在跑的 task，cc 停 一次性全部 cancel。
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: set[asyncio.Task[Any]] = field(default_factory=set)
    # `cc 下次允许` 设的一次性 flag：下条 prompt 期间所有权限自动 allow_always。
    # 进入 run_prompt 时 consume（置 False）传给 backend。
    next_prompt_auto_approve: bool = False

    @property
    def idle_sec(self) -> int:
        return int(time.time() - self.last_active)

    @property
    def queue_depth(self) -> int:
        # waiters 包含正在 hold prompt_lock 的那个 task；减掉它得到真正排队人数。
        # 不变式 busy=True ⇒ task ∈ waiters，所以 ≥ 0（finally 顺序保证）。
        return len(self.waiters) - (1 if self.busy else 0)


@dataclass(slots=True)
class PendingApproval:
    """`future.set_result(option_id)` 放行；`set_result(None)` 拒绝。"""

    future: asyncio.Future[str | None]
    options: list[PermissionOption]
    tool_kind: str | None
    tool_title: str | None


class SessionRegistry:
    def __init__(self) -> None:
        self._meta: dict[str, SessionMeta] = {}
        self._backends: dict[str, ACPBackend] = {}
        self._closing: set[str] = set()
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._pending: dict[str, list[PendingApproval]] = {}

    def backend(self, engine: str) -> ACPBackend:
        if engine not in self._backends:
            self._backends[engine] = ACPBackend(get_engine(engine))
        return self._backends[engine]

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
                            victim = min(self._meta.values(), key=lambda x: x.last_active)
                            self._meta.pop(victim.sid, None)
                            self._closing.add(victim.sid)
                        meta = SessionMeta(
                            sid=sid,
                            uid=uid,
                            gid=gid,
                            engine=engine,
                            workdir=str(WORKDIR_ROOT / sid),
                            shared=shared,
                        )
                        self._meta[sid] = meta
                    meta.last_active = time.time()
                    result = (meta, self.backend(engine))
            if result:
                if victim:
                    # LRU eviction: close subprocess but keep native_id so the
                    # evicted user can still resume on their next prompt.
                    await self._finish_close(victim, drop_native=False)
                return result
            await asyncio.sleep(_CLOSING_POLL_SEC)

    async def run_prompt(
        self,
        meta: SessionMeta,
        backend: ACPBackend,
        blocks: list[Any],
    ) -> AsyncIterator[Any]:
        # BusyBehavior: queue（默认）= 抢 prompt_lock 串行；reject = 忙就直接报错
        behavior: str = CCUIDConfig.get_config("BusyBehavior").data
        if behavior == "reject" and meta.prompt_lock.locked():
            raise BackendError("session 忙，已拒绝（队列模式可在配置改）")

        task = asyncio.current_task()
        assert task is not None, "run_prompt 必须运行在 asyncio task 里"

        async with self._lock:
            if self._meta.get(meta.sid) is not meta:
                raise BackendError("session closed")
            meta.waiters.add(task)

        try:
            async with meta.prompt_lock:
                # 拿锁之后再确认 session 还在（排队期间可能被 stop / restart / LRU 收走）
                async with self._lock:
                    if self._meta.get(meta.sid) is not meta:
                        raise BackendError("session closed")
                    meta.busy = True
                    meta.last_active = time.time()
                    # 一次性 yolo flag：consume 后立刻清零，避免影响下下个 prompt
                    auto_approve = meta.next_prompt_auto_approve
                    meta.next_prompt_auto_approve = False

                resume = await CCUIDSessionNative.fetch(meta.sid)
                try:
                    async for ev in backend.prompt(
                        meta.sid, meta.workdir, blocks, resume_id=resume, auto_approve=auto_approve
                    ):
                        yield ev
                finally:
                    native = backend.get_native_session_id(meta.sid)
                    alive = False
                    async with self._lock:
                        if self._meta.get(meta.sid) is meta:
                            meta.busy = False
                            meta.last_active = time.time()
                            alive = True
                    if alive and native:
                        await CCUIDSessionNative.store(meta.sid, native)
        finally:
            # discard 幂等，无论从哪条路径退出都一次性清掉自己。queue_depth 在
            # busy=False 但 task 还没 discard 的微秒窗口可能瞬时偏高 1，无害。
            async with self._lock:
                meta.waiters.discard(task)

    async def cancel(self, uid: str, gid: str | None, engine: str) -> int:
        shared = await self._shared(gid)
        async with self._lock:
            targets = [
                m
                for m in self._meta.values()
                if m.gid == gid and m.engine == engine and (m.busy or m.waiters) and (shared or m.uid == uid)
            ]
        count = 0
        current = asyncio.current_task()
        for meta in targets:
            # 先把排队 / 正在跑的 task 全 cancel —— 它们要么卡在 prompt_lock.acquire()，
            # 要么在 yield 循环里，都会抛 CancelledError 干净退出 run_prompt
            async with self._lock:
                # `cc 停` 自己也是个 task；不能 cancel 自己。
                waiters = [t for t in meta.waiters if t is not current]
                meta.waiters.difference_update(waiters)
            for t in waiters:
                t.cancel()
            count += len(waiters)
            # 再通知 ACP 端取消当前请求（让 agent 停手），这是底层清理不再计数
            if meta.busy:
                await self.backend(meta.engine).cancel(meta.sid)
        return count

    async def restart(self, uid: str, gid: str | None, engine: str) -> None:
        sid = make_sid(uid, gid, engine, shared=await self._shared(gid))
        async with self._lock:
            target = self._meta.pop(sid, None)
            if target:
                self._closing.add(sid)
        purge = bool(CCUIDConfig.get_config("PurgeWorkdirOnReset").data)
        if target:
            await self._finish_close(target, drop_native=True, purge_workdir=purge)
        else:
            await CCUIDSessionNative.drop(sid)
            if purge:
                await _purge_workdir(str(WORKDIR_ROOT / sid))

    async def _finish_close(
        self,
        meta: SessionMeta,
        *,
        drop_native: bool,
        purge_workdir: bool = False,
    ) -> None:
        try:
            try:
                await self.backend(meta.engine).close(meta.sid)
            except Exception:
                logger.exception(f"[CCUID] close failed: {meta.sid}")
            if drop_native:
                await CCUIDSessionNative.drop(meta.sid)
            if purge_workdir:
                await _purge_workdir(meta.workdir)
        finally:
            async with self._lock:
                self._closing.discard(meta.sid)

    def list_sessions(self) -> list[SessionMeta]:
        return list(self._meta.values())

    # ---- ask-mode permission approvals (used by ACPClient + cc允许/cc拒绝) ----

    def register_pending(
        self,
        sid: str,
        options: list[PermissionOption],
        tool_kind: str | None,
        tool_title: str | None,
    ) -> asyncio.Future[str | None]:
        """Create + register a future for an `ask`-mode permission request.

        Returns the future. ACPClient awaits it (with a timeout); when a user
        runs `cc允许 / cc拒绝`, `take_pending` resolves the oldest pending."""
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._pending.setdefault(sid, []).append(
            PendingApproval(
                future=future,
                options=options,
                tool_kind=tool_kind,
                tool_title=tool_title,
            )
        )
        return future

    def cancel_pending(self, sid: str, future: asyncio.Future[str | None]) -> None:
        """Remove a pending entry (used on timeout). Future itself is *not*
        resolved here — caller has already given up on it."""
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
        """Return + remove the oldest pending approval for (uid, gid, engine).

        Returns `None` if nothing is waiting. Caller is responsible for
        calling `.set_result(...)` on the returned PendingApproval.future."""
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
        # 把启动 sweep 同步等完再起 _loop：fire-and-forget create_task 没存引用，
        # GC 可能提前回收（3.11+ 直接 RuntimeWarning）；先 sweep 也能让首 tick 不竞速。
        await self._reap_expired()
        self._cleanup_task = asyncio.get_running_loop().create_task(self._loop(), name="CCUID-cleanup")

    async def shutdown(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        async with self._lock:
            targets = list(self._meta.values())
            self._meta.clear()
            self._closing.update(m.sid for m in targets)
        # Don't drop native_id on shutdown — let the next start-up sweep decide
        # based on idle time, so a quick restart doesn't lose active sessions.
        for meta in targets:
            await self._finish_close(meta, drop_native=False)
        for backend in self._backends.values():
            await backend.close_all()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL_SEC)
                timeout = int(CCUIDConfig.get_config("IdleTimeoutSec").data)
                now = time.time()
                async with self._lock:
                    targets = [
                        self._meta.pop(sid)
                        for sid, m in list(self._meta.items())
                        if not (m.busy or m.waiters) and now - m.last_active > timeout
                    ]
                    self._closing.update(m.sid for m in targets)
                # Idle expiry drops native_id; workdir 留到 _reap_expired 按硬过期天数删除。
                for meta in targets:
                    await self._finish_close(meta, drop_native=True)
                await self._reap_expired()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[CCUID] cleanup failed")

    async def _reap_expired(self) -> None:
        """Drop expired native_ids by DB time, purge old workdirs by mtime."""
        if not WORKDIR_ROOT.exists():
            return
        soft_sec = int(CCUIDConfig.get_config("IdleTimeoutSec").data)
        if soft_sec <= 0 and _MAX_WORKDIR_AGE_DAYS <= 0:
            return
        now = time.time()
        async with self._lock:
            live = set(self._meta) | self._closing
        for entry in WORKDIR_ROOT.iterdir():
            if not entry.is_dir() or entry.name in live:
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if _MAX_WORKDIR_AGE_DAYS > 0 and age > _MAX_WORKDIR_AGE_DAYS * _DAY_SEC:
                await _purge_workdir(str(entry))
                await CCUIDSessionNative.drop(entry.name)
                logger.info(f"[CCUID] session 硬过期(workdir 已回收): {entry.name}")
                continue
            if soft_sec > 0:
                updated_at = await CCUIDSessionNative.fetch_updated_at(entry.name)
                if updated_at is not None and updated_at > 0 and now - updated_at > soft_sec:
                    await CCUIDSessionNative.drop(entry.name)
                    idle = int(now - updated_at)
                    logger.info(f"[CCUID] session 软过期(上下文已丢弃): {entry.name} idle={idle}s")


REGISTRY = SessionRegistry()
