from PIL import Image

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.help.utils import register_help

from .get_help import ICON, get_help
from ..utils.auth import require_auth
from ..cc_config.prefix import cc_prefix

sv_help = SV("CCUID 帮助", area="ALL", priority=5)


@sv_help.on_fullmatch(("help", "帮助"), block=True)
@require_auth
async def send_help(bot: Bot, ev: Event) -> None:
    await bot.send_option(await get_help(ev.user_pm))


register_help("CCUID", f"{cc_prefix()}帮助", Image.open(ICON))
