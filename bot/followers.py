"""Копирование позиций основного счёта на другие счета, пропорционально equity.

Нужный размер на копии = размер на основном × (equity копии / equity основного) × multiplier.
Входим и выходим постепенно: один PostOnly-лимит за раз на позицию, размер куска — доля глубины стакана
(как при закрытии). Каждую секунду сверяем «нужно» и «есть» и добираем разницу.

У каждой копии своя дневная цель (+TARGET_PROFIT_PCT от её базы дня): достигла — закрывает всё и до 00:00
ничего не открывает, даже если основной счёт ещё в позиции. Пока цель не достигнута, копия повторяет
основной, в том числе его сокращения и закрытия.

ВАЖНО: копия управляется целиком — любая позиция на ней, которой нет на основном счёте, будет закрыта.
Копирующий счёт должен быть выделенным.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal

from .bybit_client import BybitClient, BybitError
from .closer import ChunkPlan, FINAL, _fmt, _floor_to_step, book_depth, fmt_price
from .config import Config
from .monitor import Bot
from .pnl import Key, PositionView, best_prices, parse_position
from .state import StateStore

log = logging.getLogger("copy")

_ctx = threading.local()
RATIO_HYSTERESIS = 0.02       # пропорцию меняем, только если она ушла больше чем на 2%
RATE_LIMIT_CODE = 10006       # Bybit: Too many visits
RATE_LIMIT_PAUSE_SEC = 3.0


class AccountFilter(logging.Filter):
    """Дописывает [имя копии] к сообщениям, которые пишутся из потока этой копии."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = getattr(_ctx, "name", None)
        if name and not getattr(record, "_acct", False):
            record.msg = f"[{name}] {record.msg}"
            record._acct = True
        return True


def install_account_filter() -> None:
    for h in logging.getLogger().handlers:
        h.addFilter(AccountFilter())


# ---------- конфигурация копирующих счетов ----------
@dataclass(frozen=True)
class FollowerSpec:
    name: str
    api_key: str
    api_secret: str
    testnet: bool = False
    base_url: str = ""
    multiplier: float = 1.0   # дополнительный множитель к пропорции по equity
    dry_run: bool = False     # true = только логировать (даже если основной бот боевой)
    enabled: bool = True


def parse_specs(rows: list[dict], where: str = "followers") -> list[FollowerSpec]:
    """Проверяет записи и возвращает включённые копии. Отключённые тоже проходят проверку имени."""
    specs, seen = [], set()
    for r in rows:
        spec = FollowerSpec(
            name=str(r["name"]).strip(), api_key=str(r.get("api_key", "")).strip(),
            api_secret=str(r.get("api_secret", "")).strip(),
            testnet=bool(r.get("testnet", False)), base_url=str(r.get("base_url", "")),
            multiplier=float(r.get("multiplier", 1.0)), dry_run=bool(r.get("dry_run", False)),
            enabled=bool(r.get("enabled", True)))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", spec.name):
            raise ValueError(f"{where}: имя '{spec.name}' — только латиница, цифры, _ и -, до 24 символов")
        if spec.name in seen:
            raise ValueError(f"{where}: имя '{spec.name}' повторяется")
        if spec.multiplier <= 0:
            raise ValueError(f"{where}: multiplier у '{spec.name}' должен быть > 0")
        if spec.enabled and not (spec.api_key and spec.api_secret):
            raise ValueError(f"{where}: у '{spec.name}' пустые api_key/api_secret")
        seen.add(spec.name)
        if spec.enabled:
            specs.append(spec)
    return specs


def load_specs(path: str) -> list[FollowerSpec]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    rows = raw.get("followers", []) if isinstance(raw, dict) else raw
    return parse_specs(rows, path)


# ---------- чистое планирование ----------
@dataclass
class Action:
    side: str
    need: Decimal
    reduce_only: bool


def plan_action(desired: float, current: float, step: Decimal, min_qty: Decimal,
                deadband_pct: float) -> Action | None:
    """Что сделать по одной позиции (ключ symbol+positionIdx): знаковые размеры, + лонг / − шорт."""
    d = _floor_to_step(Decimal(str(abs(desired))), step)
    if d < min_qty:
        d = Decimal(0)
    want = d if desired > 0 else -d
    cur = Decimal(str(current))
    if want == cur:
        return None
    if cur != 0 and (want == 0 or (want > 0) != (cur > 0)):
        # закрыть или развернуть: сначала целиком выходим из текущей (разворот доделается следующим шагом)
        return Action("Sell" if cur > 0 else "Buy", abs(cur), True)
    need = abs(want) - abs(cur)
    reduce = need < 0
    need = abs(need)
    if need < max(min_qty, d * Decimal(str(deadband_pct)) / 100):
        return None   # расхождение в пределах допуска — не дёргаем позицию
    if reduce:
        return Action("Sell" if cur > 0 else "Buy", need, True)
    return Action("Buy" if want > 0 else "Sell", need, False)


def chunk_for(act: Action, book: dict, instrument: dict, cfg: Config) -> ChunkPlan | None:
    """Кусок ордера: доля глубины стакана, цена — лучшая своей стороны (мейкер)."""
    if not book["bids"] or not book["asks"]:
        return None
    bid, ask = best_prices(book)
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if spread_bps > cfg.max_spread_bps:
        log.info("%s: спред %.1f б.п. > %.1f — пропускаем", instrument.get("symbol", ""), spread_bps, cfg.max_spread_bps)
        return None
    step = Decimal(instrument["qty_step"])
    min_qty = Decimal(str(instrument["min_qty"]))
    depth = book_depth(book, act.side, cfg.book_depth_bps)
    qty = max(_floor_to_step(Decimal(str(depth * cfg.book_fraction)), step), min_qty)
    qty = min(qty, act.need)
    if act.need - qty < min_qty:   # не оставляем «пыль»
        qty = act.need
    price = ask if act.side == "Sell" else bid
    if not act.reduce_only and float(qty) * price < instrument.get("min_notional", 5.0):
        if float(act.need) * price < instrument.get("min_notional", 5.0):
            return None   # весь недостающий объём меньше минимальной суммы ордера
        qty = act.need
    return ChunkPlan(side=act.side, qty=qty, price=price)


# ---------- один копирующий счёт ----------
@dataclass
class Pending:
    order_id: str
    side: str
    reduce_only: bool
    ts: float


class Follower:
    def __init__(self, spec: FollowerSpec, cfg: Config, client: BybitClient | None = None):
        self.spec = spec
        self.name = spec.name
        self.cfg = replace(cfg, api_key=spec.api_key, api_secret=spec.api_secret, testnet=spec.testnet,
                           base_url=spec.base_url, dry_run=cfg.dry_run or spec.dry_run)
        self.client = client or BybitClient(spec.api_key, spec.api_secret, spec.testnet, spec.base_url)
        net = "testnet" if spec.testnet else "mainnet"
        mode = "dry" if self.cfg.dry_run else "live"
        # шаги копии идут в потоке пула (по одному за раз — см. busy()), поэтому соединение не привязано к потоку
        self.store = StateStore(f"{cfg.data_dir}/state_{spec.name}_{net}_{mode}.db", check_same_thread=False)
        self.bot = Bot(self.client, self.cfg, self.store)
        self.pending: dict[Key, Pending] = {}
        self.ratio = 0.0
        self._ratio_at = 0.0
        self._last_eval = 0.0
        self._instr: dict[str, dict] = {}
        self._lev: dict[str, float] = {}
        self._logged: dict[Key, str] = {}
        self._seq = 0
        self._margin_checked = False
        self._pause_until = 0.0
        self.future: Future | None = None

    def busy(self) -> bool:
        return self.future is not None and not self.future.done()

    # ---------- вход ----------
    def tick(self, main_positions: list[PositionView], main_equity: float, main_margin: str) -> None:
        if time.monotonic() < self._pause_until:
            return
        _ctx.name = self.name
        try:
            self._tick(main_positions, main_equity, main_margin)
        except BybitError as e:
            if e.code == RATE_LIMIT_CODE:
                self._pause_until = time.monotonic() + RATE_LIMIT_PAUSE_SEC
                log.warning("Лимит запросов Bybit — пауза %.0f с", RATE_LIMIT_PAUSE_SEC)
            else:
                log.warning("Ошибка Bybit в шаге копирования: %s", e)
        except Exception:
            log.exception("Ошибка шага копирования")
        finally:
            _ctx.name = None

    def _tick(self, main_positions: list[PositionView], main_equity: float, main_margin: str) -> None:
        cfg, bot = self.cfg, self.bot
        mono = time.monotonic()
        row = self.store.get_day(bot.clock().date().isoformat())
        if row and row["goal_reached"]:
            # собственная цель выполнена: копирование остановлено, закрываем всё, что есть
            self._cancel_all()
            interval = cfg.close_loop_sleep_sec if bot.closing and not cfg.dry_run else cfg.poll_interval_sec
            if mono - self._last_eval >= interval:
                self._last_eval = mono
                bot.step()
            return
        if row is None or mono - self._last_eval >= cfg.poll_interval_sec:
            self._last_eval = mono
            bot.step()   # считает результат дня копии и фиксирует цель
            row = self.store.get_day(bot.clock().date().isoformat())
            if row and row["goal_reached"]:
                self._cancel_all()
                return
        if not self._margin_checked:
            self._margin_checked = True
            mode = self.client.get_margin_mode()
            if main_margin and mode != main_margin:
                log.warning("Режим маржи копии (%s) отличается от основного (%s) — переключите вручную в Bybit "
                            "(это настройка всего аккаунта)", mode, main_margin)
        self._sync(main_positions, main_equity)

    # ---------- синхронизация ----------
    def _instrument(self, symbol: str) -> dict:
        if symbol not in self._instr:
            self._instr[symbol] = {**self.client.get_instrument(self.cfg.category, symbol), "symbol": symbol}
        return self._instr[symbol]

    def _refresh_ratio(self, main_balance: float) -> None:
        """Пропорция = баланс копии / баланс основного × множитель. Баланс БЕЗ нереализованного PnL: equity прыгает
        вместе с ценой и у двух счетов по-разному, из-за чего пропорция «плыла» и копия то докупала, то сбрасывала.
        Дополнительно гистерезис: меньше ±2% от текущей пропорции — не обновляем."""
        if self.ratio > 0 and time.monotonic() - self._ratio_at < self.cfg.copy_ratio_refresh_sec:
            return
        balance = self.client.get_wallet_balance(self.cfg.account_type)
        if main_balance <= 0 or balance <= 0:
            log.warning("Нельзя посчитать пропорцию: баланс основного %.4f, копии %.4f", main_balance, balance)
            return
        new = balance / main_balance * self.spec.multiplier
        self._ratio_at = time.monotonic()
        if self.ratio > 0 and abs(new / self.ratio - 1) <= RATIO_HYSTERESIS:
            return
        log.info("Пропорция: ×%.4f (баланс копии %.2f / основного %.2f × множитель %.2f, без нереализованного PnL)",
                 new, balance, main_balance, self.spec.multiplier)
        self.ratio = new

    def _sync(self, main_positions: list[PositionView], main_equity: float) -> None:
        cfg = self.cfg
        self._refresh_ratio(main_equity)
        if self.ratio <= 0:
            return
        self._settle_pending()
        current = {p.key: p.signed_size for p in
                   (parse_position(r) for r in self.client.get_positions(cfg.category, cfg.settle_coin))}
        wanted = {p.key: p.signed_size * self.ratio for p in main_positions}
        leverage = {p.symbol: p.leverage for p in main_positions}
        books: dict[str, dict] = {}
        for key in sorted(set(current) | set(wanted)):
            symbol = key[0]
            try:
                inst = self._instrument(symbol)
                act = plan_action(wanted.get(key, 0.0), current.get(key, 0.0), Decimal(inst["qty_step"]),
                                  Decimal(str(inst["min_qty"])), cfg.copy_deadband_pct)
                pend = self.pending.get(key)
                if pend:
                    if act is None or (act.side, act.reduce_only) != (pend.side, pend.reduce_only):
                        self._cancel(key)   # ордер больше не нужен или стал неверным
                    continue
                if act is None:
                    self._logged.pop(key, None)
                    continue
                if symbol not in books:
                    books[symbol] = self.client.get_orderbook(cfg.category, symbol, cfg.book_limit)
                plan = chunk_for(act, books[symbol], inst, cfg)
                if plan is not None:
                    self._place(key, act, plan, leverage.get(symbol, 0.0), current.get(key, 0.0),
                                wanted.get(key, 0.0))
            except BybitError as e:
                log.warning("%s: %s", symbol, e)
            except Exception:
                log.exception("%s: ошибка копирования", symbol)

    def _place(self, key: Key, act: Action, plan: ChunkPlan, lev: float, cur: float, want: float) -> None:
        cfg, symbol = self.cfg, key[0]
        if not act.reduce_only and not cfg.dry_run and lev > 0 and self._lev.get(symbol) != lev:
            self.client.set_leverage(cfg.category, symbol, lev)   # плечо как на основном; не вышло — не открываем
            self._lev[symbol] = lev
        price = fmt_price(plan.price, self._instrument(symbol)["tick_size"])
        text = (f"{symbol} {plan.side}{' reduce' if act.reduce_only else ''} qty={_fmt(plan.qty)} @ {price} "
                f"(нужно {want:+.6g}, есть {cur:+.6g}, плечо {lev:g})")
        if cfg.dry_run:
            if self._logged.get(key) != text:   # в dry-run не спамим одной и той же строкой каждую секунду
                self._logged[key] = text
                log.info("%s  [DRY-RUN]", text)
            return
        log.info(text)
        self._seq += 1
        link = f"cp-{self.name[:10]}-{int(time.time())}-{self._seq}"[:36]
        oid = self.client.place_limit_order(cfg.category, symbol, plan.side, _fmt(plan.qty), price, key[1],
                                            link, act.reduce_only)
        self.pending[key] = Pending(oid, plan.side, act.reduce_only, time.time())

    # ---------- ордера в работе ----------
    def _settle_pending(self) -> None:
        for key, pend in list(self.pending.items()):
            try:
                order = self.client.get_order(self.cfg.category, key[0], pend.order_id)
                if order and order.get("orderStatus") in FINAL:
                    del self.pending[key]
                elif time.time() - pend.ts > self.cfg.order_timeout_sec:
                    self._cancel(key)   # не исполнился — пересчитаем по свежему стакану
            except BybitError as e:
                log.warning("%s: %s", key[0], e)

    def _cancel(self, key: Key) -> None:
        pend = self.pending.pop(key, None)
        if pend:
            self.client.cancel_order(self.cfg.category, key[0], pend.order_id)

    def _cancel_all(self) -> None:
        for key in list(self.pending):
            try:
                self._cancel(key)
            except BybitError as e:
                log.warning("%s: не удалось отменить ордер: %s", key[0], e)


# ---------- все копии ----------
class Copier:
    def __init__(self, main_client: BybitClient, cfg: Config, followers: list[Follower]):
        self.main = main_client
        self.cfg = cfg
        self.followers = followers
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(followers)), thread_name_prefix="copy")
        self._positions: list[PositionView] = []
        self._empty_ticks = 0
        self._equity = 0.0
        self._equity_at = 0.0
        self._margin = ""

    @classmethod
    def from_config(cls, main_client: BybitClient, cfg: Config) -> "Copier | None":
        specs = load_specs(cfg.followers_path)
        if not specs:
            log.info("Копирующих счетов нет (файл %s)", cfg.followers_path)
            return None
        followers = [Follower(s, cfg) for s in specs]
        log.warning("Копирование: %d счетов (%s)%s", len(followers), ", ".join(f.name for f in followers),
                    " [DRY-RUN]" if cfg.dry_run else "")
        return cls(main_client, cfg, followers)

    def tick(self) -> None:
        try:
            raw = [parse_position(r) for r in self.main.get_positions(self.cfg.category, self.cfg.settle_coin)]
            if time.monotonic() - self._equity_at >= self.cfg.copy_ratio_refresh_sec or self._equity <= 0:
                self._equity = self.main.get_wallet_balance(self.cfg.account_type)
                self._equity_at = time.monotonic()
                if not self._margin:
                    self._margin = self.main.get_margin_mode()
        except Exception as e:
            log.warning("Не удалось получить позиции основного счёта (%s) — копии ничего не делают", e)
            return
        # Пустой ответ после непустого принимаем только со второго раза: сбой API не должен закрыть все копии
        if not raw and self._positions:
            self._empty_ticks += 1
            if self._empty_ticks < 2:
                return
        else:
            self._empty_ticks = 0
        self._positions = raw
        for f in self.followers:
            if not f.busy():
                f.future = self.pool.submit(f.tick, raw, self._equity, self._margin)

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        for f in self.followers:
            f.store.close()
