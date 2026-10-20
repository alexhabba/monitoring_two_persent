"""Состояние бота в одном файле SQLite: база дня, цель выполнена, итоги, события."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS days(
    day            TEXT PRIMARY KEY,   -- дата по поясу бота (МСК), YYYY-MM-DD
    start_equity   REAL NOT NULL,      -- баланс счёта на начало дня (или на момент старта бота)
    start_ts       TEXT NOT NULL,
    start_kind     TEXT NOT NULL,      -- midnight | first_run | late_start
    target_pct     REAL NOT NULL,
    goal_reached   INTEGER NOT NULL DEFAULT 0,
    goal_ts        TEXT,
    goal_equity    REAL,               -- баланс счёта в момент достижения цели (справочно)
    goal_pct       REAL,               -- результат дня в % от базы в момент цели
    final_equity   REAL,               -- баланс после закрытия всех позиций (справочно)
    final_ts       TEXT,
    start_unrealised REAL NOT NULL DEFAULT 0,  -- нереализованный PnL на начало дня
    goal_gain      REAL,               -- результат дня в USDT в момент цели
    final_gain     REAL                -- результат дня в USDT после закрытия всего
);
CREATE TABLE IF NOT EXISTS events(
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    day  TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str, check_same_thread: bool = True):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(days)")}
        for col, ddl in (("start_unrealised", "REAL NOT NULL DEFAULT 0"), ("goal_gain", "REAL"), ("final_gain", "REAL")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE days ADD COLUMN {col} {ddl}")

    def close(self) -> None:
        self.conn.close()

    def count_days(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM days").fetchone()[0]

    def get_day(self, day: str):
        row = self.conn.execute("SELECT * FROM days WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None

    def create_day(self, day: str, equity: float, unrealised: float, ts: str, kind: str, target_pct: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO days(day,start_equity,start_unrealised,start_ts,start_kind,target_pct) "
                "VALUES(?,?,?,?,?,?)", (day, equity, unrealised, ts, kind, target_pct))

    def delete_day(self, day: str) -> bool:
        """Сбросить день (строка дня удаляется, при следующем шаге база пересчитается)."""
        with self.conn:
            n = self.conn.execute("DELETE FROM days WHERE day=?", (day,)).rowcount
            self.conn.execute("INSERT INTO events(ts,day,kind,text) VALUES(datetime('now'),?,?,?)",
                              (day, "reset", "день сброшен вручную"))
        return n > 0

    def mark_goal(self, day: str, ts: str, equity: float, gain: float, pct: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE days SET goal_reached=1, goal_ts=?, goal_equity=?, goal_gain=?, goal_pct=? WHERE day=?",
                (ts, equity, gain, pct, day))

    def set_final(self, day: str, ts: str, equity: float, gain: float | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE days SET final_equity=?, final_gain=?, final_ts=? WHERE day=?",
                              (equity, gain, ts, day))

    def add_event(self, day: str, ts: str, kind: str, text: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO events(ts,day,kind,text) VALUES(?,?,?,?)", (ts, day, kind, text))
