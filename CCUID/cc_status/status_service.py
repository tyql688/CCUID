import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.msgs import StatusMsg
from ..utils.engines import list_engines
from ..utils.session import REGISTRY
from ..utils.database import CCUIDSessionNative


async def run_status(bot: Bot, _ev: Event) -> None:
    sessions = REGISTRY.list_sessions()
    if not sessions:
        await bot.send(StatusMsg.NO_ACTIVE)
        return
    lines = [StatusMsg.ACTIVE_HEADER]
    for m in sessions:
        native = await CCUIDSessionNative.fetch(m.sid)
        state = "busy" if m.busy else f"idle {m.idle_sec}s"
        lines.append(StatusMsg.session_line(m.sid, state, native))
    await bot.send("\n".join(lines))


async def run_doctor(bot: Bot, _ev: Event) -> None:
    lines = [StatusMsg.DOCTOR_HEADER]
    for engine in list_engines():
        command = engine.cmd
        launcher = command[0] if command else "<empty>"
        if command and shutil.which(launcher):
            lines.append(StatusMsg.doctor_ok(engine.name))
        else:
            lines.append(StatusMsg.doctor_missing(engine.name, launcher, engine.install_url))
    await bot.send("\n".join(lines))
