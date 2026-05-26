import json
from pathlib import Path

from PIL import Image

from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help

from ..version import VERSION
from ..utils.image import get_footer
from ..cc_config.prefix import cc_prefix

_HERE = Path(__file__).parent
ICON = _HERE.parent.parent / "ICON.png"
TEXTURE = _HERE / "texture2d"
ICON_PATH = _HERE / "icon_path"
HELP_DATA = _HERE / "help.json"


def get_help_data() -> dict[str, PluginHelp]:
    with HELP_DATA.open(encoding="utf-8") as file:
        return json.load(file)


plugin_help = get_help_data()


def _maybe(name: str) -> Image.Image | None:
    p = TEXTURE / name
    return Image.open(p) if p.exists() else None


async def get_help(pm: int) -> str | bytes:
    return await get_new_help(
        plugin_name="CCUID",
        plugin_info={f"v{VERSION}": ""},
        plugin_icon=Image.open(ICON),
        plugin_help=plugin_help,
        plugin_prefix=cc_prefix(),
        help_mode="dark",
        banner_bg=_maybe("banner_bg.jpg"),
        banner_sub_text="把 cli agents 装进 gscore",
        help_bg=_maybe("bg.jpg"),
        cag_bg=_maybe("cag_bg.png"),
        item_bg=_maybe("item.png"),
        footer=get_footer(),
        highlight_bg=_maybe("highlight.png"),
        icon_path=ICON_PATH,
        enable_cache=False,
        column=4,
        pm=pm,
    )
