def user_error(err: BaseException, *, default_message: str = "操作失败") -> str:
    raw = str(err).strip()
    if not raw:
        return default_message
    raw = raw.split("stderr tail:", 1)[0].strip()
    if not raw:
        return default_message
    return raw
