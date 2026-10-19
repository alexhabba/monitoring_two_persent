"""Часовой пояс «дня» бота. Не зависит от пояса машины (важно при переезде на сервер)."""
from __future__ import annotations

from datetime import timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def get_tz(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    if name == "Europe/Moscow":  # в Москве нет перехода на летнее время
        return timezone(timedelta(hours=3), "MSK")
    raise ValueError(f"Неизвестный часовой пояс {name!r}; установите пакет tzdata")
