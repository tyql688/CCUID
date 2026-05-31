from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .common import send_engine_list
from ..utils.msgs import RemoteSessionMsg
from ..utils.paths import same_path
from ..utils.errors import user_error
from ..utils.runtime import REGISTRY
from ..utils.timefmt import format_local_datetime
from ..utils.acp.types import SessionInfo
from ..utils.acp.backend import BackendError
from ..utils.list_render import RecordItem, RecordField, markdown_records
from ..cc_config.cc_config import CCUIDConfig

_SESSION_SCOPE_WORKDIR = "workdir"
_SESSION_SCOPE_ALL = "all"


def _session_load_scope() -> str:
    raw = str(CCUIDConfig.get_config("SessionLoadScope").data).strip().lower()
    if raw in {_SESSION_SCOPE_ALL, "全部"}:
        return _SESSION_SCOPE_ALL
    return _SESSION_SCOPE_WORKDIR


def _session_scope_label(scope: str) -> str:
    return "全部" if scope == _SESSION_SCOPE_ALL else "当前工作区"


def _session_title(title: str | None) -> str:
    if title is None or title == "":
        return "(无标题)"
    return title


def _filter_current_workdir_sessions(
    sessions: tuple[SessionInfo, ...],
    workdir: str,
) -> tuple[SessionInfo, ...]:
    with_cwd = [session for session in sessions if session.cwd]
    if not with_cwd:
        return sessions
    return tuple(session for session in sessions if same_path(session.cwd, workdir))


def _filter_sessions_by_scope(
    sessions: tuple[SessionInfo, ...],
    workdir: str,
    scope: str,
) -> tuple[SessionInfo, ...]:
    if scope == _SESSION_SCOPE_ALL:
        return sessions
    return _filter_current_workdir_sessions(sessions, workdir)


def _resolve_session_ref(token: str, sessions: tuple[SessionInfo, ...]) -> SessionInfo | None:
    raw = token.strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
        return None
    exact = [session for session in sessions if session.session_id == raw]
    if exact:
        return exact[0]
    prefix = [session for session in sessions if session.session_id.startswith(raw)]
    return prefix[0] if len(prefix) == 1 else None


async def _list_scoped_sessions(
    engine: str,
    workdir: str,
    *,
    scope: str,
    cursor: str | None = None,
) -> tuple[bool, tuple[SessionInfo, ...], str | None]:
    # claude-code-acp 的服务端 cwd 过滤会漏掉实际存在的 session；统一本地过滤，保持各 adapter 行为一致。
    supported, sessions, next_cursor = await REGISTRY.backend(engine).list_agent_sessions(
        workdir,
        cursor=cursor,
    )
    if not supported:
        return False, (), None
    return True, _filter_sessions_by_scope(sessions, workdir, scope), next_cursor


async def do_remote_session_list(bot: Bot, ev: Event, engine: str, cursor: str | None = None) -> None:
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    workdir = meta.workdir if meta is not None else await REGISTRY.workdir_for(ev.user_id, ev.group_id, engine)
    scope = _session_load_scope()
    try:
        supported, sessions, next_cursor = await _list_scoped_sessions(engine, workdir, scope=scope, cursor=cursor)
    except BackendError as e:
        await bot.send(RemoteSessionMsg.failed(user_error(e)))
        return
    if not supported:
        await bot.send(RemoteSessionMsg.UNSUPPORTED)
        return
    if not sessions:
        await bot.send(
            RemoteSessionMsg.EMPTY if scope == _SESSION_SCOPE_ALL else RemoteSessionMsg.EMPTY_CURRENT_WORKDIR
        )
        return
    records: list[RecordItem] = []
    for i, session in enumerate(sessions, 1):
        records.append(
            RecordItem(
                f"#{i} {_session_title(session.title)}",
                (
                    RecordField("Session ID", session.session_id, code=True),
                    RecordField("目录", session.cwd, code=True),
                    RecordField("更新时间", format_local_datetime(session.updated_at)),
                ),
            )
        )
    footer = RemoteSessionMsg.next_cursor(next_cursor) if next_cursor else None
    md = markdown_records(
        f"{engine} sessions - {_session_scope_label(scope)} ({len(sessions)} 条)", records, footer=footer
    )
    await send_engine_list(bot, engine, md)


async def do_remote_session_resume(bot: Bot, ev: Event, engine: str, token: str) -> None:
    if not token.strip():
        await bot.send(RemoteSessionMsg.usage_resume())
        return
    meta = REGISTRY.find(ev.user_id, ev.group_id, engine)
    workdir = meta.workdir if meta is not None else await REGISTRY.workdir_for(ev.user_id, ev.group_id, engine)
    scope = _session_load_scope()
    try:
        supported, sessions, next_cursor = await _list_scoped_sessions(engine, workdir, scope=scope)
        if not supported:
            await bot.send(RemoteSessionMsg.UNSUPPORTED)
            return
        target = _resolve_session_ref(token, sessions)
        # session_id 可能不在第一页；序号只对应第一页，直接 session_id/prefix 最多往后翻 4 页。
        if target is None and not token.strip().isdigit():
            for _ in range(4):
                if not next_cursor:
                    break
                _, more, next_cursor = await _list_scoped_sessions(engine, workdir, scope=scope, cursor=next_cursor)
                target = _resolve_session_ref(token, more)
                if target is not None:
                    break
        if target is None:
            await bot.send(RemoteSessionMsg.resume_not_found(token, _session_scope_label(scope)))
            return
        session_id = str(target.session_id)
        await REGISTRY.bind_native_session(ev.user_id, ev.group_id, engine, session_id)
    except BackendError as e:
        await bot.send(RemoteSessionMsg.resume_failed(user_error(e)))
        return
    await bot.send(RemoteSessionMsg.resumed(session_id, target.title))
