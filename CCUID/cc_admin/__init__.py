from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

from .admin_service import (
    run_grant_user,
    run_grant_group,
    run_list_grants,
    run_revoke_user,
    run_revoke_group,
)

# 授权列表单独一个 SV，priority 比 sv_admin 更高（数字越小越早）：
# fullmatch 在 regex 之前优先匹配，确保「授权列表」不会被「授权」前缀吞掉。
# 实际上下面的 regex 已经 $ 锚定排除「授权列表」，独立 SV 是二重保险。
sv_admin_list = SV("CCUID 授权列表", pm=0, area="ALL", priority=3)
sv_admin = SV("CCUID 授权", pm=0, area="ALL", priority=4)


@sv_admin_list.on_fullmatch(("授权列表", "grants"), block=True)
async def list_grants(bot: Bot, ev: Event) -> None:
    await run_list_grants(bot, ev)


# 一个 regex router 同时覆盖：授权 / 取消授权 / 授权群 / 取消授权群。
@sv_admin.on_regex(r"^(?P<action>取消)?授权(?P<target>群)?\s*(?P<arg>.+)?$", block=True)
async def admin_router(bot: Bot, ev: Event) -> None:
    # 把命令字剥完后的参数写回 ev.text，让下游 run_* 拿到不带命令字的原参数。
    arg = ev.regex_dict.get("arg")
    ev.text = arg.strip() if isinstance(arg, str) else ""
    is_revoke = bool(ev.regex_dict.get("action"))
    is_group = bool(ev.regex_dict.get("target"))
    if is_group:
        runner = run_revoke_group if is_revoke else run_grant_group
    else:
        runner = run_revoke_user if is_revoke else run_grant_user
    await runner(bot, ev)
