import time

from sqlmodel import Field, SQLModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session

from .mode import GroupMode
from .engines import DEFAULT_ENGINE


def _group_key(gid: str | None) -> str:
    return gid if gid is not None else ""


class CCUIDGrantUser(SQLModel, table=True):
    __tablename__: str = "ccuid_grant_user"
    user_id: str = Field(primary_key=True, max_length=128)

    @classmethod
    @with_session
    async def grant(cls, session: AsyncSession, uid: str) -> bool:
        if await session.get(cls, uid):
            return False
        session.add(cls(user_id=uid))
        return True

    @classmethod
    @with_session
    async def revoke(cls, session: AsyncSession, uid: str) -> bool:
        row = await session.get(cls, uid)
        if not row:
            return False
        await session.delete(row)
        return True

    @classmethod
    @with_session
    async def exists(cls, session: AsyncSession, uid: str) -> bool:
        return await session.get(cls, uid) is not None

    @classmethod
    @with_session
    async def list_all(cls, session: AsyncSession) -> list[str]:
        rows = (await session.execute(select(cls))).scalars().all()
        return sorted(r.user_id for r in rows)


class CCUIDGrantGroup(SQLModel, table=True):
    __tablename__: str = "ccuid_grant_group"
    group_id: str = Field(primary_key=True, max_length=128)
    mode: str = Field(default=GroupMode.SOLO.value, max_length=16)

    @classmethod
    @with_session
    async def grant(cls, session: AsyncSession, gid: str, mode: GroupMode = GroupMode.SOLO) -> bool:
        row = await session.get(cls, gid)
        if row:
            row.mode = mode.value
            session.add(row)
            return False
        session.add(cls(group_id=gid, mode=mode.value))
        return True

    @classmethod
    @with_session
    async def revoke(cls, session: AsyncSession, gid: str) -> bool:
        row = await session.get(cls, gid)
        if not row:
            return False
        await session.delete(row)
        return True

    @classmethod
    @with_session
    async def get_mode(cls, session: AsyncSession, gid: str) -> GroupMode:
        row = await session.get(cls, gid)
        return GroupMode(row.mode) if row else GroupMode.SOLO

    @classmethod
    @with_session
    async def exists(cls, session: AsyncSession, gid: str) -> bool:
        return await session.get(cls, gid) is not None

    @classmethod
    @with_session
    async def list_all(cls, session: AsyncSession) -> list[tuple[str, GroupMode]]:
        rows = (await session.execute(select(cls))).scalars().all()
        return sorted((r.group_id, GroupMode(r.mode)) for r in rows)


class CCUIDSessionNative(SQLModel, table=True):
    __tablename__: str = "ccuid_session_native"
    session_id: str = Field(primary_key=True, max_length=256)
    native_id: str = Field(default="", max_length=128)
    updated_at: int = Field(default=0)

    @classmethod
    @with_session
    async def fetch(cls, session: AsyncSession, sid: str) -> str | None:
        row = await session.get(cls, sid)
        return row.native_id if row else None

    @classmethod
    @with_session
    async def fetch_updated_at(cls, session: AsyncSession, sid: str) -> int | None:
        row = await session.get(cls, sid)
        if row is None:
            return None
        return row.updated_at

    @classmethod
    @with_session
    async def store(cls, session: AsyncSession, sid: str, native: str) -> None:
        now = int(time.time())
        row = await session.get(cls, sid)
        if row:
            row.native_id = native
            row.updated_at = now
            session.add(row)
            return
        session.add(cls(session_id=sid, native_id=native, updated_at=now))

    @classmethod
    @with_session
    async def drop(cls, session: AsyncSession, sid: str) -> None:
        row = await session.get(cls, sid)
        if row:
            await session.delete(row)


class CCUIDSessionModel(SQLModel, table=True):
    """用户在该 sid 上手动选的 model_id；ACP 协议没有「session 创建时带 model」的口子，
    backend._ensure 拿到 session 后会读这张表 reapply 一次，让选择跨进程 / cc new 粘住。"""

    __tablename__: str = "ccuid_session_model"
    session_id: str = Field(primary_key=True, max_length=256)
    model_id: str = Field(default="", max_length=256)
    updated_at: int = Field(default=0)

    @classmethod
    @with_session
    async def fetch(cls, session: AsyncSession, sid: str) -> str | None:
        row = await session.get(cls, sid)
        return row.model_id if row and row.model_id else None

    @classmethod
    @with_session
    async def store(cls, session: AsyncSession, sid: str, model_id: str) -> None:
        now = int(time.time())
        row = await session.get(cls, sid)
        if row:
            row.model_id = model_id
            row.updated_at = now
            session.add(row)
            return
        session.add(cls(session_id=sid, model_id=model_id, updated_at=now))

    @classmethod
    @with_session
    async def drop(cls, session: AsyncSession, sid: str) -> None:
        row = await session.get(cls, sid)
        if row:
            await session.delete(row)


class CCUIDUserEngine(SQLModel, table=True):
    __tablename__: str = "ccuid_user_engine"
    user_id: str = Field(primary_key=True, max_length=128)
    group_id: str = Field(primary_key=True, max_length=128, default="")
    engine: str = Field(max_length=32, default=DEFAULT_ENGINE)

    @classmethod
    @with_session
    async def get(cls, session: AsyncSession, uid: str, gid: str | None) -> str | None:
        row = await session.get(cls, (uid, _group_key(gid)))
        return row.engine if row else None

    @classmethod
    @with_session
    async def set(cls, session: AsyncSession, uid: str, gid: str | None, engine: str) -> None:
        group_id = _group_key(gid)
        row = await session.get(cls, (uid, group_id))
        if row:
            row.engine = engine
            session.add(row)
            return
        session.add(cls(user_id=uid, group_id=group_id, engine=engine))
