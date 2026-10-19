"""Основной цикл: оценка чистого PnL по всем позициям и постепенное закрытие."""
from __future__ import annotations

import logging
import time

from .bybit_client import BybitClient
from .closer import PositionCloser
from .config import Config
from .pnl import Evaluation, Key, PositionView, evaluate, parse_position, profit_base

log = logging.getLogger("monitor")


class Bot:
    def __init__(self, client: BybitClient, cfg: Config, closer: PositionCloser | None = None):
        self.client = client
        self.cfg = cfg
        self.closer = closer or PositionCloser(client, cfg)
        self.closing = False
        self.frozen_base: float | None = None
        self.locked: dict[Key, float] = {}   # PnL полностью закрытых за эту сессию позиций
        self._last_net: dict[Key, float] = {}
        self._fee_cache: dict[str, tuple[float, float]] = {}
        self._rr = 0  # round-robin по позициям

    # ---------- вспомогательное ----------
    def _taker_fee(self, symbol: str) -> float:
        ts, fee = self._fee_cache.get(symbol, (0.0, 0.0))
        if time.time() - ts > 600:
            _, fee = self.client.get_fee_rates(self.cfg.category, symbol)
            self._fee_cache[symbol] = (time.time(), fee)
        return fee

    def _track_vanished(self, positions: list[PositionView]) -> None:
        current = {p.key for p in positions}
        for key in list(self._last_net):
            if key not in current and self.closing:
                self.locked[key] = self._last_net[key]
                log.info("Позиция %s закрыта полностью, зафиксировано ≈ %.4f", key, self.locked[key])
        for key in current:
            self.locked.pop(key, None)

    # ---------- один шаг ----------
    def step(self) -> Evaluation | None:
        positions = [parse_position(r) for r in
                     self.client.get_positions(self.cfg.category, self.cfg.settle_coin)]
        self._track_vanished(positions)

        if not positions:
            log.info("Открытых позиций нет (category=%s, settleCoin=%s)",
                     self.cfg.category, self.cfg.settle_coin)
            if self.locked:
                log.info("Все позиции закрыты. Итог сессии ≈ %.4f USDT", sum(self.locked.values()))
            self.locked.clear()
            self._last_net.clear()
            self.closing = False
            self.frozen_base = None
            return None

        books = {s: self.client.get_orderbook(self.cfg.category, s, self.cfg.book_limit)
                 for s in {p.symbol for p in positions}}
        fees = {s: self._taker_fee(s) for s in books}

        if self.closing and self.frozen_base:
            base = self.frozen_base
        else:
            equity = self.client.get_equity(self.cfg.account_type)
            base = profit_base(self.cfg.profit_base, equity, positions)

        ev = evaluate(positions, books, fees, base, self.locked, self.cfg.slippage_buffer_bps)
        self._last_net = dict(ev.per_position)

        log.info("Позиций: %d | чистый PnL ≈ %.4f USDT | %.3f%% от %s=%.2f (цель %.2f%%)%s",
                 len(positions), ev.total_net, ev.pct, self.cfg.profit_base, base,
                 self.cfg.target_profit_pct, " | ЗАКРЫТИЕ" if self.closing else "")

        if ev.pct >= self.cfg.target_profit_pct:
            if not self.closing:
                self.closing = True
                self.frozen_base = base
                log.warning("Цель достигнута (%.3f%% >= %.2f%%) — начинаем постепенное закрытие%s",
                            ev.pct, self.cfg.target_profit_pct,
                            " [DRY-RUN: ордера не отправляются]" if self.cfg.dry_run else "")
            self._close_one_chunk(positions, books)
        elif self.closing:
            log.warning("PnL опустился ниже цели (%.3f%% < %.2f%%) — закрытие приостановлено",
                        ev.pct, self.cfg.target_profit_pct)
            self.closing = False
            self.frozen_base = None
        return ev

    def _close_one_chunk(self, positions: list[PositionView], books: dict[str, dict]) -> None:
        ordered = sorted(positions, key=lambda p: p.key)
        p = ordered[self._rr % len(ordered)]
        self._rr += 1
        try:
            filled = self.closer.close_chunk(p, books[p.symbol])
            # Хедж: на том же инструменте открыта противоположная нога — закрываем ту же долю,
            # чтобы не остаться с перекошенной (направленной) позицией.
            opp = next((q for q in positions if q.symbol == p.symbol and q.key != p.key), None)
            if opp is not None and filled > 0 and p.size > 0:
                self.closer.close_chunk(opp, books[opp.symbol], fraction=min(filled / p.size, 1.0))
        except Exception:  # одна неудачная лимитка не должна ронять бота
            log.exception("Ошибка при закрытии %s", p.symbol)

    # ---------- цикл ----------
    def run(self) -> None:
        mode = "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "БОЕВОЙ РЕЖИМ"
        log.info("Старт. Режим: %s | testnet=%s | цель %.2f%% от %s",
                 mode, self.cfg.testnet, self.cfg.target_profit_pct, self.cfg.profit_base)
        while True:
            try:
                self.step()
            except Exception:
                log.exception("Ошибка шага мониторинга")
            fast = self.closing and not self.cfg.dry_run
            time.sleep(self.cfg.close_loop_sleep_sec if fast else self.cfg.poll_interval_sec)
