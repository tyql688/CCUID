from __future__ import annotations

from datetime import datetime


def format_local_datetime(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            dt = datetime.fromtimestamp(int(text))
        else:
            raw = f"{text[:-1]}+00:00" if text.endswith("Z") else text
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                return text
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
