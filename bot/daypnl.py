"""Реализованный торговый результат дня из лога транзакций Bybit (UTA).

change = cashFlow + funding − fee. В расчёт идут только торговые типы записей; переводы
(TRANSFER_IN/OUT), бонусы, конвертации, Earn и пр. исключены — поэтому пополнения/выводы и
изменение цены обеспечения (BTC и др.) на цель не влияют.
"""
from __future__ import annotations

import logging

from .bybit_client import BybitClient
from .config import Config

log = logging.getLogger("daypnl")

# TRADE — сделки (реализованный PnL и комиссии), SETTLEMENT — фандинг, остальное — редкие торговые события
INCLUDED_TYPES = {"TRADE", "SETTLEMENT", "DELIVERY", "LIQUIDATION", "ADL", "FEE_REFUND", "INTEREST"}
OVERLAP_MS = 5 * 60 * 1000          # перекрытие при дозапросе: запись может появиться в логе с задержкой
FULL_REFRESH_MS = 10 * 60 * 1000    # раз в 10 минут пересчитываем день целиком
FUTURE_MS = 60 * 1000


class DayPnlTracker:
    def __init__(self, client: BybitClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self._day: str | None = None
        self._tx: dict[str, float] = {}
        self._last_ms = 0
        self._full_at = 0

    def realized(self, day: str, start_ms: int, now_ms: int) -> float:
        if day != self._day:
            self._day, self._tx, self._last_ms, self._full_at = day, {}, 0, 0
        full = self._last_ms == 0 or now_ms - self._full_at > FULL_REFRESH_MS
        frm = start_ms if full else max(start_ms, self._last_ms - OVERLAP_MS)
        rows = self.client.get_transaction_log(
            self.cfg.account_type, self.cfg.category, self.cfg.settle_coin, int(frm), int(now_ms + FUTURE_MS))
        fresh: dict[str, float] = {}
        last = self._last_ms
        for r in rows:
            t = int(r.get("transactionTime") or 0)
            if t < start_ms:
                continue
            last = max(last, t)
            if r.get("type") in INCLUDED_TYPES and r.get("currency", self.cfg.settle_coin) == self.cfg.settle_coin:
                fresh[str(r["id"])] = float(r.get("change") or 0)
        if full:
            self._tx = fresh
            self._full_at = now_ms
        else:
            self._tx.update(fresh)
        self._last_ms = max(last, start_ms)
        return sum(self._tx.values())
