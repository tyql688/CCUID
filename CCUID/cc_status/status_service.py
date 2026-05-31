from __future__ import annotations

import shutil

from gsuid_core.bot import Bot
from gsuid_core.models import Event

from ..utils.msgs import StatusMsg
from ..utils.engines import list_engines
from ..utils.session import REGISTRY
from ..utils.timefmt import format_local_datetime
from ..utils.database import CCUIDSessionNative
from ..utils.list_render import RecordItem, RecordField, markdown_table, markdown_records, send_markdown_image_or_text


async def run_status(bot: Bot, _ev: Event) -> None:
    sessions = REGISTRY.list_sessions()
    if not sessions:
        await bot.send(StatusMsg.NO_ACTIVE)
        return
    records: list[RecordItem] = []
    for m in sessions:
        native = await CCUIDSessionNative.fetch(m.sid)
        state = "busy" if m.busy else f"idle {m.idle_sec}s"
        title, updated_at = REGISTRY.backend(m.engine).get_session_info(m.sid)
        fields = [
            RecordField("SID", m.sid, code=True),
            RecordField("Native ID", native, code=True),
            RecordField("标题", title),
        ]
        local_updated_at = format_local_datetime(updated_at)
        if local_updated_at is not None:
            fields.append(RecordField("更新时间", local_updated_at))
        records.append(
            RecordItem(
                f"{m.engine} · {state}",
                tuple(fields),
            )
        )
    md = markdown_records("活跃 session", records)
    await send_markdown_image_or_text(bot, md, display="CCUID")


async def run_doctor(bot: Bot, _ev: Event) -> None:
    rows: list[list[object | None]] = []
    for engine in list_engines():
        command = engine.cmd
        launcher = command[0] if command else "<empty>"
        if command and shutil.which(launcher):
            rows.append([engine.name, engine.display, "ok", ""])
        else:
            rows.append([engine.name, engine.display, f"missing {launcher}", engine.install_url])
    md = "## CCUID 体检\n\n" + markdown_table(["Engine", "名称", "状态", "安装"], rows)
    await send_markdown_image_or_text(bot, md, display="CCUID")
