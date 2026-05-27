from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.mode import GroupMode, parse_mode
from ..utils.msgs import AdminMsg
from ..utils.database import CCUIDGrantUser, CCUIDGrantGroup


def _target_uid(ev: Event) -> str | None:
    if ev.at:
        return ev.at
    parts = ev.text.strip().split()
    return parts[0] if parts else None


async def run_grant_user(bot: Bot, ev: Event) -> None:
    uid = _target_uid(ev)
    if not uid:
        return
    added = await CCUIDGrantUser.grant(uid)
    await bot.send(AdminMsg.user_grant(uid, added=added))


async def run_revoke_user(bot: Bot, ev: Event) -> None:
    uid = _target_uid(ev)
    if not uid:
        return
    removed = await CCUIDGrantUser.revoke(uid)
    await bot.send(AdminMsg.user_revoke(uid, removed=removed))


async def run_grant_group(bot: Bot, ev: Event) -> None:
    parts = ev.text.strip().split()
    if parts and parse_mode(parts[0]) is not None:
        if ev.group_id is None:
            return
        parts.insert(0, ev.group_id)
    gid = parts[0] if parts else ev.group_id
    mode = parse_mode(parts[1]) if len(parts) > 1 else GroupMode.SOLO
    if not gid or mode is None:
        return
    added = await CCUIDGrantGroup.grant(gid, mode)
    await bot.send(AdminMsg.group_grant(gid, mode.value, added=added))


async def run_revoke_group(bot: Bot, ev: Event) -> None:
    arg = ev.text.strip()
    gid = arg if arg else ev.group_id
    if not gid:
        return
    removed = await CCUIDGrantGroup.revoke(gid)
    await bot.send(AdminMsg.group_revoke(gid, removed=removed))


async def run_list_grants(bot: Bot, _ev: Event) -> None:
    users = await CCUIDGrantUser.list_all()
    groups = await CCUIDGrantGroup.list_all()
    user_line = ", ".join(f"`{u}`" for u in users) if users else AdminMsg.EMPTY
    group_line = "\n".join(f"  • `{g}` [{m.value}]" for g, m in groups) if groups else AdminMsg.EMPTY
    await bot.send(AdminMsg.list_grants(user_line, group_line, len(users), len(groups)))
