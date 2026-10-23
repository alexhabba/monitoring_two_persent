"""Основной цикл: дневная цель от базы дня (00:00 МСК) и закрытие позиций после её достижения."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .bybit_client import BybitClient
from .closer import PositionCloser
from .ladder import LINK_PREFIX as TP_PREFIX, TakeProfitLadder
from .config import Config
from .daypnl import INCLUDED_TYPES, DayPnlTracker
from .pnl import Evaluation, Key, PositionView, evaluate, parse_position
from .state import StateStore
from .timeutil import get_tz

log = logging.getLogger("monitor")


class Bot:
    def __init__(self, client: BybitClient, cfg: Config, store: StateStore,
                 closer: PositionCloser | None = None, clock=None, live_data: bool = True):
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
        self.force_reset = False   # копия: сбросить цель по сигналу основного счёта (своего таймера у копии нет)
        # --- WebSocket и лесенка тейк-профитов (только основной счёт: live_data=False у копий) ---
        self.pub = self.priv = self.ladder = None
        if live_data:
            self.closer.open_orders_fn = self._open_orders      # список ордеров из потока, при сбое — REST
        self._wake = threading.Event()          # «пора проверить прямо сейчас» (исполнился тейк-профит, цель по живым данным)
        self._day_row: dict | None = None
        self._last_realized = 0.0
        self.goal_latched = False
        self._ladder_off = False
        self._last_fast_wake = 0.0
        if live_data and cfg.use_websocket:
            from .stream import PrivateStream, PublicStream
            self.pub = PublicStream(testnet=cfg.testnet)
            self.priv = PrivateStream(cfg.api_key, cfg.api_secret, client, cfg.category, cfg.settle_coin, cfg.testnet,
                                      on_execution=self._on_execution)
            if cfg.tp_ladder and not cfg.dry_run:
                self.ladder = TakeProfitLadder(client, cfg, self.closer.instrument, self._open_orders)

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
        if kind != "midnight" and not self.cfg.base_from_now:
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

        if self.cfg.base_from_now and kind != "midnight":
            note = "; BASE_FROM_NOW: база = состояние на момент старта, сделки/прибыль до этого момента не учитываются"
        self.store.create_day(day, start_equity, unrealised, start_ts, kind, self.cfg.target_profit_pct)
        self.store.add_event(day, now.isoformat(), "day_start",
                             f"{kind}: база дня {start_equity:.4f}, нереализованный {unrealised:.4f}{note}")
        log.warning("Новый день %s: база (баланс) = %.4f USDT, нереализованный PnL на старте = %+.4f [%s]. "
                    "Цель +%.2f%% = %+.4f USDT результата за день%s",
                    day, start_equity, unrealised, kind, self.cfg.target_profit_pct,
                    start_equity * self.cfg.target_profit_pct / 100, note)
        self._pending_final = False
        return self.store.get_day(day)

    def start_streams(self) -> None:
        if self.pub is not None:
            self.pub.start()
            self.priv.start()
            log.info("WebSocket включён (стакан/позиции/исполнения), лесенка тейк-профитов: %s",
                     f"вкл, пачка на {self.cfg.tp_goal_coef}× цели дня" if self.ladder else "выкл")

    def stop_streams(self) -> None:
        for st in (self.pub, self.priv):
            if st is not None:
                st.stop()

    def _on_execution(self, e: dict) -> None:
        if str(e.get("orderLinkId", "")).startswith(TP_PREFIX):
            self._wake.set()                    # тейк-профит исполнился — сразу пересчитать результат дня

    def _streams_ok(self) -> bool:
        return (self.pub is not None and self.priv is not None and self.priv.healthy(35.0)
                and self.pub.healthy(self.cfg.stream_max_age_sec))

    def _open_orders(self) -> list[dict]:
        if self.priv is not None and self.priv.healthy(35.0):
            return self.priv.open_orders()
        return self.client.get_open_orders(self.cfg.category, self.cfg.settle_coin)

    def _live_positions(self) -> list[PositionView] | None:
        """Позиции из потока с ценой mark из тикера (нереализованный PnL считаем сами). None — данным нельзя доверять."""
        if not self._streams_ok():
            return None
        raws = self.priv.positions()
        self.pub.ensure({r["symbol"] for r in raws})
        out = []
        for r in raws:
            tk = self.pub.ticker(r["symbol"], self.cfg.stream_max_age_sec)
            if tk is None:
                return None
            size, avg, mark = float(r["size"]), float(r.get("avgPrice") or 0), tk["markPrice"]
            raw = dict(r)
            raw.update(markPrice=str(mark), unrealisedPnl=str((mark - avg) * size if r["side"] == "Buy" else (avg - mark) * size))
            out.append(parse_position(raw))
        return out

    def _books(self, symbols: set[str]) -> dict[str, dict]:
        if self.pub is not None and symbols:
            self.pub.ensure(symbols)
            got = {s: self.pub.book(s, self.cfg.stream_max_age_sec) for s in symbols}
            if all(b is not None for b in got.values()):
                return got                      # все стаканы из потока — без запросов REST
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

        self.force_reset = False
        equity = self.client.get_equity(cfg.account_type)
        positions = [parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin)]
        self._remember(positions)
        day = self._ensure_day(now, equity, sum(p.unrealised_pnl for p in positions))
        if self._pause_over(day, now):     # пауза после цели прошла, хотя цель «мигнула» и не держится — всё равно новый цикл
            self._reset_cycle(day, now, equity, sum(p.unrealised_pnl for p in positions))
            day = self.store.get_day(day["day"])

        ev, books = self._evaluate(day, now, positions, equity)
        self._day_row, self._last_realized = day, ev.realized

        goal = ev.pct >= cfg.target_profit_pct
        if goal:
            pause = (now + timedelta(minutes=cfg.goal_cooldown_min)).isoformat() if cfg.goal_cooldown_min > 0 else None
            self.store.mark_goal(day["day"], now.isoformat(), equity, ev.gain, ev.pct, pause)
            self.store.add_event(day["day"], now.isoformat(), "goal",
                                 f"цель достигнута: {ev.gain:+.4f} USDT = {ev.pct:.3f}% от базы {ev.start_equity:.4f}")
            log.warning("ЦЕЛЬ ДНЯ ДОСТИГНУТА: результат %+.4f USDT = %.3f%% >= %.2f%% (база %.4f). "
                        "Записано в БД. Закрываем все позиции и все новые на %.0f мин.%s",
                        ev.gain, ev.pct, cfg.target_profit_pct, ev.start_equity, cfg.goal_cooldown_min,
                        " [DRY-RUN: ордера не отправляются]" if cfg.dry_run else "")
        ev.goal = goal
        self.goal_latched = goal
        self.closing = goal and bool(positions)

        log.info("%s | результат дня %+.4f (%+.3f%% / цель %.2f%%) = реализ. %+.4f + нереализ. Δ %+.4f + выход %+.4f"
                 " | позиций %d | баланс %.4f (справочно)%s",
                 day["day"], ev.gain, ev.pct, cfg.target_profit_pct, ev.realized, ev.unrealised_delta,
                 ev.adjustment, len(positions), equity, " | ЦЕЛЬ ВЫПОЛНЕНА" if goal else "")

        if goal and positions:
            self._pending_final = True
            self._close_all(positions, books, ev.pct >= cfg.target_profit_pct)
        return ev

    def _goal_step(self, row: dict, now: datetime) -> Evaluation:
        """Цель дня уже отмечена. Перед КАЖДЫМ куском закрытия результат дня пересчитывается заново: если он
        опустился ниже цели — закрытие останавливается, отметка о цели снимается и бот снова следит за результатом.
        Если позиций нет — раз в шаг тоже проверяем, что результат (уже реализованный) не ниже цели."""
        cfg = self.cfg
        positions = [parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin)]
        self._remember(positions)
        ev, books = self._evaluate(row, now, positions, 0.0)
        self._day_row, self._last_realized = row, ev.realized
        self.goal_latched = ev.pct >= cfg.target_profit_pct
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
        if not positions and self._cooldown_over(row, now):
            if self._pending_final:
                self._record_final(row, now)
            self._reset_cycle(row, now)
            ev.goal = False
            return ev
        if positions:
            self.closing = True
            self._pending_final = True
            if time.monotonic() - self._goal_log_at >= 5:      # раз в 5 с, а не каждую секунду
                self._goal_log_at = time.monotonic()
                log.info("%s | цель дня выполнена (результат %+.4f = %.3f%% >= %.2f%%) — закрываем позиции (%d)",
                         row["day"], ev.gain, ev.pct, cfg.target_profit_pct, len(positions))
            self._close_all(positions, books, ev.pct >= cfg.target_profit_pct)
            return ev
        self.closing = False
        if self._pending_final:
            self._record_final(row, now)
        else:
            log.info("%s | цель выполнена (%+.4f = %.3f%%), позиций нет — пауза до %s",
                     row["day"], ev.gain, ev.pct, self._cooldown_end(row).strftime("%H:%M"))
        return ev

    def _cooldown_end(self, row: dict) -> datetime:
        if row.get("pause_until"):
            return datetime.fromisoformat(row["pause_until"])
        return datetime.fromisoformat(row["goal_ts"]) + timedelta(minutes=self.cfg.goal_cooldown_min)

    def _pause_over(self, row: dict, now: datetime) -> bool:
        return self.cfg.goal_cooldown_min > 0 and bool(row.get("pause_until")) and now >= self._cooldown_end(row)

    def _cooldown_over(self, row: dict, now: datetime) -> bool:
        """Пауза после цели прошла (сброс только когда всё закрыто). 0 = пауза до 00:00, как раньше."""
        if self.force_reset:
            return True
        return (self.cfg.goal_cooldown_min > 0 and bool(row.get("goal_ts")) and now >= self._cooldown_end(row))   # 0 = пауза до 00:00

    def _reset_cycle(self, row: dict, now: datetime, equity: float | None = None, unrealised: float = 0.0) -> None:
        """Цель сбрасывается: новая база = текущий баланс, результат считается с этого момента, торговля снова разрешена.
        Открытые позиции входят в базу своим нереализованным PnL — считается только их дальнейшее изменение."""
        if equity is None:
            equity = self.client.get_equity(self.cfg.account_type)
        self.store.rebase_day(row["day"], now.isoformat(), equity, unrealised, self.cfg.target_profit_pct)
        self._day_row = self.store.get_day(row["day"])
        self._last_realized = 0.0
        self.goal_latched = False
        self.closing = False
        self._pending_final = False
        self.force_reset = False
        log.warning("%s | ПАУЗА %.0f мин после цели закончилась — цель сброшена, новая база %.4f USDT, "
                    "снова ищем +%.2f%% (торговля разрешена)",
                    row["day"], self.cfg.goal_cooldown_min, equity, self.cfg.target_profit_pct)

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

    def _close_all(self, positions: list[PositionView], books: dict[str, dict], allow_market: bool = False) -> None:
        """Закрываем ВСЕ позиции сразу и параллельно; первыми — те, у кого прибыль за последние 1–2 мин выросла сильнее
        всего (импульс может откатиться, их выходим первыми)."""
        if self.ladder is not None:
            self._ladder_off = True
            self.ladder.cancel_all()    # пачку тейк-профитов снимаем ДО первого закрывающего ордера (иначе reduce-only превысят позицию)
        now = time.monotonic()
        scored = sorted(((self._momentum(p, now), p) for p in positions), key=lambda t: -t[0])
        if len(scored) > 1:
            order = [p.symbol for _, p in scored[:6]]
            if order != self._last_priority or now - self._priority_at > 10:   # не повторяем строку каждую секунду
                self._last_priority, self._priority_at = order, now
                log.info("Приоритет закрытия (рост прибыли за 1–2 мин): %s",
                         ", ".join(f"{p.symbol} {m:+.2f}" for m, p in scored[:6]))
        try:
            self.closer.close_all([p for _, p in scored], books, allow_market)
        except Exception:  # одна неудачная лимитка не должна ронять бота
            log.exception("Ошибка при закрытии позиций")

    # ---------- быстрая проверка по живым данным (WebSocket) ----------
    def fast_tick(self) -> None:
        """Раз в `fast_tick_sec`: цель по живым ценам (если достигнута — сразу полный шаг) и лесенка тейк-профитов."""
        cfg, row = self.cfg, self._day_row
        if row is None or not self._streams_ok():
            return
        positions = self._live_positions()
        if positions is None:
            return
        self.priv.maybe_resync()
        target_gain = row["start_equity"] * cfg.target_profit_pct / 100
        current = self._last_realized + sum(p.unrealised_pnl for p in positions) - row["start_unrealised"]
        need = target_gain - current
        if positions and not self.goal_latched and need <= 0 and time.monotonic() - self._last_fast_wake > 1.0:
            self._last_fast_wake = time.monotonic()
            log.info("Цель по живым данным: результат %+.4f >= %+.4f USDT — проверяем и закрываем сразу", current, target_gain)
            self._wake.set()
        if self.ladder is None:
            return
        if self.goal_latched:                   # цель достигнута: лесенка снимается, дальше закрывает обычный механизм
            if not self._ladder_off:
                self._ladder_off = True
                self.ladder.cancel_all()
            return
        self._ladder_off = False
        books = {p.symbol: self.pub.book(p.symbol, cfg.stream_max_age_sec) for p in positions}
        # пачка стоит там, где результат дня = коэффициент × цель дня (а не на самой цели)
        self.ladder.tick(positions, books, target_gain * cfg.tp_goal_coef - current)

    # ---------- цикл ----------
    def run(self, copier=None) -> None:
        mode = "DRY-RUN (ордера не отправляются)" if self.cfg.dry_run else "БОЕВОЙ РЕЖИМ"
        log.info("Старт. Режим: %s | testnet=%s | цель +%.2f%% в день | пояс %s | состояние: %s",
                 mode, self.cfg.testnet, self.cfg.target_profit_pct, self.cfg.timezone, self.cfg.state_path)
        row = self.store.get_day(self.clock().date().isoformat())
        if row:
            self._day_row, self.goal_latched = row, bool(row["goal_reached"])
        last_copy = 0.0
        while True:
            self._wake.clear()
            try:
                self.step()
            except Exception:
                log.exception("Ошибка шага мониторинга")
            fast = self.closing and not self.cfg.dry_run
            end = time.monotonic() + (self.cfg.close_loop_sleep_sec if fast else self.cfg.poll_interval_sec)
            while True:   # между шагами: копии сверяются раз в секунду, быстрая проверка/лесенка — несколько раз в секунду
                now = time.monotonic()
                if copier is not None and now - last_copy >= self.cfg.copy_tick_sec:
                    last_copy = now
                    try:
                        day = self.clock().date().isoformat()
                        copier.tick(self.store.has_goal_event(day), self.store.last_reset_ts(day))
                    except Exception:
                        log.exception("Ошибка копирования")
                if self.pub is not None:
                    try:
                        self.fast_tick()
                    except Exception:
                        log.exception("Ошибка быстрой проверки")
                remaining = end - time.monotonic()
                if remaining <= 0 or self._wake.is_set():
                    break
                tick = self.cfg.fast_tick_sec if self.pub is not None else (
                    self.cfg.copy_tick_sec if copier is not None else remaining)
                self._wake.wait(min(tick, remaining))
