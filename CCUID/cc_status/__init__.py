from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.auth import require_auth
from .status_service import run_doctor, run_status

sv_status = SV("CCUID 状态查询", priority=5)


@sv_status.on_fullmatch(("status", "状态"), block=True)
@require_auth
async def status(bot: Bot, ev: Event) -> None:
    await run_status(bot, ev)


@sv_status.on_fullmatch(("doctor", "体检"), block=True)
@require_auth
async def doctor(bot: Bot, ev: Event) -> None:
    await run_doctor(bot, ev)
