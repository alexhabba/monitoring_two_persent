"""Логи: отдельный файл на каждый день, хранение N дней, время в заданном поясе."""
from __future__ import annotations

import glob
import logging
import os
import re
import sys
import time
from datetime import date, datetime

_NAME_RE = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.log$")


class TzFormatter(logging.Formatter):
    def __init__(self, fmt: str, tz):
        super().__init__(fmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.tz).isoformat(sep=" ", timespec="seconds")


class DailyFileHandler(logging.Handler):
    """Пишет в <dir>/bot-YYYY-MM-DD.log; при смене даты открывает новый файл и чистит старые."""

    def __init__(self, directory: str, tz, retention_days: int = 7):
        super().__init__()
        self.directory = directory
        self.tz = tz
        self.retention_days = retention_days
        self._day: date | None = None
        self._stream = None
        os.makedirs(directory, exist_ok=True)
        self.cleanup(datetime.now(tz).date())

    def path_for(self, day: date) -> str:
        return os.path.join(self.directory, f"bot-{day.isoformat()}.log")

    def cleanup(self, today: date) -> list[str]:
        """Удаляет файлы старше retention_days. Свежие не трогает."""
        removed = []
        for path in glob.glob(os.path.join(self.directory, "*.log*")):
            name = os.path.basename(path)
            m = _NAME_RE.match(name)
            try:
                if m:
                    old = (today - date.fromisoformat(m.group(1))).days > self.retention_days
                else:  # посторонние/старые файлы — по времени изменения
                    old = (time.time() - os.path.getmtime(path)) > self.retention_days * 86400
                if old:
                    os.remove(path)
                    removed.append(name)
            except OSError:
                pass
        return removed

    def emit(self, record):
        try:
            day = datetime.fromtimestamp(record.created, self.tz).date()
            if day != self._day:
                if self._stream:
                    self._stream.close()
                self._stream = open(self.path_for(day), "a", encoding="utf-8")
                self._day = day
                self.cleanup(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


def setup_logging(log_dir: str, tz, retention_days: int = 7) -> DailyFileHandler:
    fmt = TzFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", tz)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    fileh = DailyFileHandler(log_dir, tz, retention_days)
    fileh.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(fileh)
    return fileh
