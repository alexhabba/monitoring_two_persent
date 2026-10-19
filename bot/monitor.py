"""Основной цикл: дневная цель от баланса на 00:00 (МСК) и закрытие позиций после её достижения."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from .bybit_client import BybitClient
from .closer import PositionCloser
from .config import Config
from .pnl import Evaluation, PositionView, evaluate, parse_position
from .state import StateStore
from .timeutil import get_tz

log = logging.getLogger("monitor")


class Bot:
    def __init__(self, client: BybitClient, cfg: Config, store: StateStore,
                 closer: PositionCloser | None = None, clock=None):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.closer = closer or PositionCloser(client, cfg)
        self.tz = get_tz(cfg.timezone)
        self.clock = clock or (lambda: datetime.now(self.tz))
        self._fee_cache: dict[str, tuple[float, float]] = {}
        self._rr = 0
        self._pending_final = False  # были позиции, закрываемые после цели — нужно записать итог дня
        self.closing = False

    # ---------- вспомогательное ----------
    def _taker_fee(self, symbol: str) -> float:
        ts, fee = self._fee_cache.get(symbol, (0.0, 0.0))
        if time.time() - ts > 600:
            _, fee = self.client.get_fee_rates(self.cfg.category, symbol)
            self._fee_cache[symbol] = (time.time(), fee)
        return fee

    def _ensure_day(self, equity: float) -> dict:
        now = self.clock()
        day = now.date().isoformat()
        row = self.store.get_day(day)
        if row:
            return row
        if self.store.count_days() == 0:
            kind = "first_run"
        elif now.hour == 0 and now.minute < 5:
            kind = "midnight"
        else:
            kind = "late_start"  # бот был выключен в полночь — база = баланс на момент старта
        self.store.create_day(day, equity, now.isoformat(), kind, self.cfg.target_profit_pct)
        self.store.add_event(day, now.isoformat(), "day_start", f"{kind}: база дня {equity:.4f}")
        log.warning("Новый день %s: база (баланс) = %.4f USDT [%s]. Цель +%.2f%% = %.4f",
                    day, equity, kind, self.cfg.target_profit_pct,
                    equity * (1 + self.cfg.target_profit_pct / 100))
        self._pending_final = False
        return self.store.get_day(day)

    # ---------- один шаг ----------
    def step(self) -> Evaluation:
        cfg = self.cfg
        now = self.clock()
        equity = self.client.get_equity(cfg.account_type)
        day = self._ensure_day(equity)
        positions = [parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin)]

        books = {s: self.client.get_orderbook(cfg.category, s, cfg.book_limit)
                 for s in {p.symbol for p in positions}}
        fees = {s: self._taker_fee(s) for s in books}
        ev = evaluate(equity, day["start_equity"], positions, books, fees, cfg.slippage_buffer_bps)

        goal = bool(day["goal_reached"])
        if not goal and ev.pct >= cfg.target_profit_pct:
            self.store.mark_goal(day["day"], now.isoformat(), ev.projected, ev.pct)
            self.store.add_event(day["day"], now.isoformat(), "goal",
                                 f"цель достигнута: {ev.pct:.3f}% (баланс с учётом выхода {ev.projected:.4f})")
            goal = True
            log.warning("ЦЕЛЬ ДНЯ ДОСТИГНУТА: %.3f%% >= %.2f%% (прогнозный баланс %.4f, база %.4f). "
                        "Закрываем все позиции и все новые до конца дня.%s",
                        ev.pct, cfg.target_profit_pct, ev.projected, ev.start_equity,
                        " [DRY-RUN: ордера не отправляются]" if cfg.dry_run else "")
        ev.goal = goal
        self.closing = goal and bool(positions)

        log.info("%s | баланс %.4f (база %.4f) | с учётом выхода %.4f | %+.3f%% / цель %.2f%% | позиций %d%s",
                 day["day"], ev.equity, ev.start_equity, ev.projected, ev.pct, cfg.target_profit_pct,
                 len(positions), " | ЦЕЛЬ ВЫПОЛНЕНА" if goal else "")

        if goal and positions:
            self._pending_final = True
            self._close_one_chunk(positions, books)
        elif goal and not positions and self._pending_final:
            self.store.set_final(day["day"], now.isoformat(), equity)
            self.store.add_event(day["day"], now.isoformat(), "final",
                                 f"все позиции закрыты, баланс {equity:.4f} ({ev.pct:+.3f}% от базы)")
            log.warning("Все позиции закрыты. Итог: баланс %.4f, %+.3f%% от базы дня (%.4f).",
                        equity, ev.pct, ev.start_equity)
            self._pending_final = False
        return ev

    def _close_one_chunk(self, positions: list[PositionView], books: dict[str, dict]) -> None:
        ordered = sorted(positions, key=lambda p: p.key)
        p = ordered[self._rr % len(ordered)]
        self._rr += 1
        try:
            filled = self.closer.close_chunk(p, books[p.symbol])
            # Хедж: закрываем ту же долю противоположной ноги, чтобы не остаться с перекошенной позицией.
            opp = next((q for q in positions if q.symbol == p.symbol and q.key != p.key), None)
            if opp is not None and filled > 0 and p.size > 0:
                self.closer.close_chunk(opp, books[opp.symbol], fraction=min(filled / p.size, 1.0))
        except Exception:  # одна неудачная лимитка не должна ронять бота
            log.exception("Ошибка при закрытии %s", p.symbol)

    # ---------- цикл ----------
    def run(self) -> None:
        mode = "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "БОЕВОЙ РЕЖИМ"
        log.info("Старт. Режим: %s | testnet=%s | цель +%.2f%% в день | пояс %s | состояние: %s",
                 mode, self.cfg.testnet, self.cfg.target_profit_pct, self.cfg.timezone, self.cfg.state_path)
        while True:
            try:
                self.step()
            except Exception:
                log.exception("Ошибка шага мониторинга")
            fast = self.closing and not self.cfg.dry_run
            time.sleep(self.cfg.close_loop_sleep_sec if fast else self.cfg.poll_interval_sec)
