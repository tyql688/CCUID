import json
from pathlib import Path

from PIL import Image

from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help

from ..version import VERSION
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


async def get_help(pm: int) -> str | bytes:
    return await get_new_help(
        plugin_name="CCUID",
        plugin_info={f"v{VERSION}": ""},
        plugin_icon=Image.open(ICON),
        plugin_help=plugin_help,
        plugin_prefix=cc_prefix(),
        help_mode="dark",
        banner_bg=Image.open(TEXTURE / "banner_bg.jpg"),
        banner_sub_text="把 cli agents 装进 gscore",
        help_bg=Image.open(TEXTURE / "bg.jpg"),
        cag_bg=Image.open(TEXTURE / "cag_bg.png"),
        item_bg=Image.open(TEXTURE / "item.png"),
        footer=Image.open(TEXTURE / "footer.png"),
        icon_path=ICON_PATH,
        enable_cache=False,
        column=4,
        pm=pm,
    )
