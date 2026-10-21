"""Основной цикл: дневная цель от базы дня (00:00 МСК) и закрытие позиций после её достижения."""
from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .bybit_client import BybitClient
from .closer import PositionCloser
from .config import Config
from .daypnl import INCLUDED_TYPES, DayPnlTracker
from .pnl import Evaluation, Key, PositionView, evaluate, parse_position
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
        self.tracker = DayPnlTracker(client, cfg)
        self._fee_cache: dict[str, tuple[float, float]] = {}
        self._last_priority: list[str] = []
        self._priority_at = 0.0
        self._goal_log_at = 0.0
        self._hist: deque[tuple[float, dict[Key, float]]] = deque()   # (monotonic, {позиция: нереализованный PnL})
        self._pending_final = False  # закрывали позиции после цели — нужно записать итог дня
        self.closing = False

    # ---------- вспомогательное ----------
    def _taker_fee(self, symbol: str) -> float:
        ts, fee = self._fee_cache.get(symbol, (0.0, 0.0))
        if time.time() - ts > 600:
            _, fee = self.client.get_fee_rates(self.cfg.category, symbol)
            self._fee_cache[symbol] = (time.time(), fee)
        return fee

    def _now_ms(self, now: datetime) -> int:
        return int(now.timestamp() * 1000) + int(getattr(self.client, "time_offset_ms", 0))

    def _ensure_day(self, now: datetime, equity: float, unrealised: float) -> dict:
        day = now.date().isoformat()
        row = self.store.get_day(day)
        if row:
            return row
        if self.store.count_days() == 0:
            kind = "first_run"
        elif now.hour == 0 and now.minute < 5:
            kind = "midnight"
        else:
            kind = "late_start"  # бот был выключен в полночь

        start_ts, start_equity, note = now.isoformat(), equity, ""
        if kind != "midnight":
            # Бот стартовал днём: восстанавливаем базу на 00:00 по логу транзакций, чтобы учесть уже сделанное за день.
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                rows = self.client.get_transaction_log(
                    self.cfg.account_type, None, self.cfg.settle_coin,
                    int(midnight.timestamp() * 1000), self._now_ms(now) + 60_000)
                total = sum(float(r.get("change") or 0) for r in rows
                            if r.get("currency", self.cfg.settle_coin) == self.cfg.settle_coin
                            and int(r.get("transactionTime") or 0) >= int(midnight.timestamp() * 1000))
                trade = sum(float(r.get("change") or 0) for r in rows
                            if r.get("type") in INCLUDED_TYPES and r.get("category") == self.cfg.category
                            and r.get("currency", self.cfg.settle_coin) == self.cfg.settle_coin
                            and int(r.get("transactionTime") or 0) >= int(midnight.timestamp() * 1000))
                est_midnight = equity - total      # баланс на 00:00 (баланс сейчас − все изменения счёта за день)
                est_capital = equity - trade       # капитал без торгового результата дня (переводы за день учтены)
                base = max(est_midnight, est_capital)   # берём большую — процент получается осторожнее
                if base <= 0:
                    log.warning("Восстановить базу на 00:00 не удалось: баланс %.4f, изменения счёта %+.4f, торговый "
                                "результат %+.4f. Берём текущий баланс. Запустите ./run.sh diag",
                                equity, total, trade)
                else:
                    start_equity, start_ts = base, midnight.isoformat()
                    note = (f"; база восстановлена: баланс на 00:00 ≈ {est_midnight:.4f}, капитал без результата дня ≈ "
                            f"{est_capital:.4f} (переводы за день {total - trade:+.4f}) — взято {base:.4f}; "
                            f"нереализованный PnL позиций на 00:00 неизвестен — считаем только его дальнейшее изменение")
            except Exception:
                log.exception("Не удалось восстановить базу на 00:00 — берём текущий баланс")

        self.store.create_day(day, start_equity, unrealised, start_ts, kind, self.cfg.target_profit_pct)
        self.store.add_event(day, now.isoformat(), "day_start",
                             f"{kind}: база дня {start_equity:.4f}, нереализованный {unrealised:.4f}{note}")
        log.warning("Новый день %s: база (баланс) = %.4f USDT, нереализованный PnL на старте = %+.4f [%s]. "
                    "Цель +%.2f%% = %+.4f USDT результата за день%s",
                    day, start_equity, unrealised, kind, self.cfg.target_profit_pct,
                    start_equity * self.cfg.target_profit_pct / 100, note)
        self._pending_final = False
        return self.store.get_day(day)

    def _books(self, symbols: set[str]) -> dict[str, dict]:
        if len(symbols) <= 1:
            return {s: self.client.get_orderbook(self.cfg.category, s, self.cfg.book_limit) for s in symbols}
        with ThreadPoolExecutor(max_workers=min(len(symbols), 8)) as ex:   # стаканы всех монет сразу, а не по очереди
            futs = {s: ex.submit(self.client.get_orderbook, self.cfg.category, s, self.cfg.book_limit) for s in symbols}
            return {s: f.result() for s, f in futs.items()}

    def _remember(self, positions: list[PositionView]) -> None:
        """Запоминаем нереализованный PnL позиций — по нему считаем, у кого прибыль растёт быстрее всех."""
        now = time.monotonic()
        self._hist.append((now, {p.key: p.unrealised_pnl for p in positions}))
        horizon = max(self.cfg.momentum_windows_sec) + 60
        while self._hist and now - self._hist[0][0] > horizon:
            self._hist.popleft()

    def _momentum(self, p: PositionView, now: float) -> float:
        """Рост нереализованной прибыли позиции за последние 1–2 минуты (USDT): чем больше, тем раньше закрываем."""
        scores = []
        for w in self.cfg.momentum_windows_sec:
            old = None
            for ts, snap in self._hist:        # самый свежий снимок не новее now − w
                if ts <= now - w and p.key in snap:
                    old = snap[p.key]
            if old is None:                    # истории меньше окна — берём самый старый снимок, если ему хотя бы 15 с
                for ts, snap in self._hist:
                    if now - ts >= 15 and p.key in snap:
                        old = snap[p.key]
                        break
            if old is not None:
                scores.append(p.unrealised_pnl - old)
        return max(scores) if scores else 0.0

    def _evaluate(self, day: dict, now: datetime, positions: list[PositionView],
                  equity: float) -> tuple[Evaluation, dict[str, dict]]:
        """Результат дня «как если бы всё закрыли сейчас». Ошибка лога транзакций — исключение, т.е. без решения."""
        books = self._books({p.symbol for p in positions})
        fees = {s: self._taker_fee(s) for s in books}
        start_ms = int(datetime.fromisoformat(day["start_ts"]).timestamp() * 1000)
        realized = self.tracker.realized(day["day"], start_ms, self._now_ms(now))
        ev = evaluate(day["start_equity"], day["start_unrealised"], realized,
                      positions, books, fees, self.cfg.slippage_buffer_bps, equity)
        return ev, books

    # ---------- один шаг ----------
    def step(self) -> Evaluation:
        cfg = self.cfg
        now = self.clock()
        row = self.store.get_day(now.date().isoformat())
        if row and row["goal_reached"]:
            return self._goal_step(row, now)   # цель уже в БД — лог транзакций и баланс больше не запрашиваем

        equity = self.client.get_equity(cfg.account_type)
        positions = [parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin)]
        self._remember(positions)
        day = self._ensure_day(now, equity, sum(p.unrealised_pnl for p in positions))

        ev, books = self._evaluate(day, now, positions, equity)

        goal = ev.pct >= cfg.target_profit_pct
        if goal:
            self.store.mark_goal(day["day"], now.isoformat(), equity, ev.gain, ev.pct)
            self.store.add_event(day["day"], now.isoformat(), "goal",
                                 f"цель достигнута: {ev.gain:+.4f} USDT = {ev.pct:.3f}% от базы {ev.start_equity:.4f}")
            log.warning("ЦЕЛЬ ДНЯ ДОСТИГНУТА: результат %+.4f USDT = %.3f%% >= %.2f%% (база %.4f). "
                        "Записано в БД. Закрываем все позиции и все новые до конца дня.%s",
                        ev.gain, ev.pct, cfg.target_profit_pct, ev.start_equity,
                        " [DRY-RUN: ордера не отправляются]" if cfg.dry_run else "")
        ev.goal = goal
        self.closing = goal and bool(positions)

        log.info("%s | результат дня %+.4f (%+.3f%% / цель %.2f%%) = реализ. %+.4f + нереализ. Δ %+.4f + выход %+.4f"
                 " | позиций %d | баланс %.4f (справочно)%s",
                 day["day"], ev.gain, ev.pct, cfg.target_profit_pct, ev.realized, ev.unrealised_delta,
                 ev.adjustment, len(positions), equity, " | ЦЕЛЬ ВЫПОЛНЕНА" if goal else "")

        if goal and positions:
            self._pending_final = True
            self._close_all(positions, books)
        return ev

    def _goal_step(self, row: dict, now: datetime) -> Evaluation:
        """Цель дня уже отмечена. Перед КАЖДЫМ куском закрытия результат дня пересчитывается заново: если он
        опустился ниже цели — закрытие останавливается, отметка о цели снимается и бот снова следит за результатом.
        Если позиций нет — раз в шаг тоже проверяем, что результат (уже реализованный) не ниже цели."""
        cfg = self.cfg
        positions = [parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin)]
        self._remember(positions)
        ev, books = self._evaluate(row, now, positions, 0.0)
        if ev.pct < cfg.target_profit_pct:
            self.closer.cancel_all()    # живые ордера на закрытие больше не нужны: цель снята
            self.store.clear_goal(row["day"])
            self.store.add_event(row["day"], now.isoformat(), "goal_lost",
                                 f"результат дня {ev.gain:+.4f} USDT = {ev.pct:.3f}% < цели {cfg.target_profit_pct:.2f}% "
                                 f"— закрытие остановлено, цель снята")
            log.warning("%s | результат дня %+.4f USDT = %.3f%% опустился ниже цели %.2f%% — ЗАКРЫТИЕ ОСТАНОВЛЕНО, "
                        "цель снята, продолжаем следить (позиций %d)",
                        row["day"], ev.gain, ev.pct, cfg.target_profit_pct, len(positions))
            self.closing = False
            self._pending_final = False
            ev.goal = False
            return ev
        ev.goal = True
        if positions:
            self.closing = True
            self._pending_final = True
            if time.monotonic() - self._goal_log_at >= 5:      # раз в 5 с, а не каждую секунду
                self._goal_log_at = time.monotonic()
                log.info("%s | цель дня выполнена (результат %+.4f = %.3f%% >= %.2f%%) — закрываем позиции (%d)",
                         row["day"], ev.gain, ev.pct, cfg.target_profit_pct, len(positions))
            self._close_all(positions, books)
            return ev
        self.closing = False
        if self._pending_final:
            self._record_final(row, now)
        else:
            log.info("%s | цель дня выполнена (%+.4f = %.3f%%), позиций нет — ждём до 00:00",
                     row["day"], ev.gain, ev.pct)
        return ev

    def _record_final(self, row: dict, now: datetime) -> None:
        equity = self.client.get_equity(self.cfg.account_type)
        gain = None
        try:
            start_ms = int(datetime.fromisoformat(row["start_ts"]).timestamp() * 1000)
            gain = self.tracker.realized(row["day"], start_ms, self._now_ms(now))
        except Exception:
            log.exception("Не удалось получить итоговый результат дня из лога транзакций")
        self.store.set_final(row["day"], now.isoformat(), equity, gain)
        txt = f"{gain:+.4f} USDT" if gain is not None else "н/д"
        self.store.add_event(row["day"], now.isoformat(), "final",
                             f"все позиции закрыты, результат дня {txt}, баланс {equity:.4f}")
        log.warning("Все позиции закрыты. Итог дня: %s (цель была %.2f%%), баланс %.4f.",
                    txt, self.cfg.target_profit_pct, equity)
        self._pending_final = False

    def _close_all(self, positions: list[PositionView], books: dict[str, dict]) -> None:
        """Закрываем ВСЕ позиции сразу и параллельно; первыми — те, у кого прибыль за последние 1–2 мин выросла сильнее
        всего (импульс может откатиться, их выходим первыми)."""
        now = time.monotonic()
        scored = sorted(((self._momentum(p, now), p) for p in positions), key=lambda t: -t[0])
        if len(scored) > 1:
            order = [p.symbol for _, p in scored[:6]]
            if order != self._last_priority or now - self._priority_at > 10:   # не повторяем строку каждую секунду
                self._last_priority, self._priority_at = order, now
                log.info("Приоритет закрытия (рост прибыли за 1–2 мин): %s",
                         ", ".join(f"{p.symbol} {m:+.2f}" for m, p in scored[:6]))
        try:
            self.closer.close_all([p for _, p in scored], books)
        except Exception:  # одна неудачная лимитка не должна ронять бота
            log.exception("Ошибка при закрытии позиций")

    # ---------- цикл ----------
    def run(self, copier=None) -> None:
        mode = "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "БОЕВОЙ РЕЖИМ"
        log.info("Старт. Режим: %s | testnet=%s | цель +%.2f%% в день | пояс %s | состояние: %s",
                 mode, self.cfg.testnet, self.cfg.target_profit_pct, self.cfg.timezone, self.cfg.state_path)
        while True:
            try:
                self.step()
            except Exception:
                log.exception("Ошибка шага мониторинга")
            fast = self.closing and not self.cfg.dry_run
            end = time.monotonic() + (self.cfg.close_loop_sleep_sec if fast else self.cfg.poll_interval_sec)
            while True:   # между шагами основного счёта копии сверяются каждую секунду
                if copier is not None:
                    try:
                        copier.tick()
                    except Exception:
                        log.exception("Ошибка копирования")
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(self.cfg.copy_tick_sec, remaining) if copier is not None else remaining)
