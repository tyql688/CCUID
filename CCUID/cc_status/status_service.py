import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.engines import list_engines
from ..utils.session import REGISTRY
from ..utils.database import CCUIDSessionNative


async def run_status(bot: Bot, _ev: Event) -> None:
    sessions = REGISTRY.list_sessions()
    if not sessions:
        await bot.send("无活跃 session")
        return
    lines = ["**活跃 session**"]
    for m in sessions:
        native = await CCUIDSessionNative.fetch(m.sid)
        state = "busy" if m.busy else f"idle {m.idle_sec}s"
        suffix = f" native={native}" if native else ""
        lines.append(f"- {m.sid}: {state}{suffix}")
    await bot.send("\n".join(lines))


async def run_doctor(bot: Bot, _ev: Event) -> None:
    lines = ["**CCUID 体检**"]
    for engine in list_engines():
        command = engine.cmd
        launcher = command[0] if command else "<empty>"
        if command and shutil.which(launcher):
            lines.append(f"- {engine.name}: ok")
        else:
            lines.append(f"- {engine.name}: missing `{launcher}` → 装法 {engine.install_url}")
    await bot.send("\n".join(lines))
