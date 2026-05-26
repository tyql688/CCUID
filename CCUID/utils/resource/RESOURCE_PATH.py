from pathlib import Path

from gsuid_core.data_store import get_res_path

MAIN_PATH = get_res_path() / "CCUID"
CONFIG_PATH = MAIN_PATH / "config.json"
WORKDIR_ROOT = Path.home() / ".ccuid"

MAIN_PATH.mkdir(parents=True, exist_ok=True)
WORKDIR_ROOT.mkdir(parents=True, exist_ok=True)
