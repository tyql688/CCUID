from gsuid_core.sv import get_plugin_available_prefix


def cc_prefix() -> str:
    return get_plugin_available_prefix("CCUID")
