_MAX_USER_ERROR_CHARS = 180


def user_error(err: BaseException, *, default_message: str = "操作失败") -> str:
    raw = str(err).strip()
    if not raw:
        return default_message
    raw = raw.split("stderr tail:", 1)[0].strip()
    line = raw.splitlines()[0].strip()
    if not line:
        return default_message
    if len(line) > _MAX_USER_ERROR_CHARS:
        return line[: _MAX_USER_ERROR_CHARS - 1] + "…"
    return line
