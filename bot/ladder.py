"""Лесенка тейк-профитов: заранее выставленные лимитки на закрытие по цене, при которой счёт достигнет цели дня.

Для каждой открытой позиции считается, куда должна «выстрелить» цена, чтобы результат дня достиг цели (остальные позиции
считаем неподвижными). Лимитки стоят в стакане ЗАРАНЕЕ, поэтому при резком импульсе биржа исполняет их сама, без задержки
на опрос и реакцию бота. Уровней несколько (коэффициенты `TP_COEFS`, по умолчанию 1.0 и 1.3 — «во сколько раз дальше от цены,
нужной для цели»), а объём раскладывается по стакану пропорционально плотности заявок рядом с каждым уровнем, чтобы мы не
выделялись крупной заявкой.

Это post-only reduce-only лимитки (мейкер, комиссия ниже, проскальзывания нет, открыть/перевернуть позицию не могут).
Когда цель достигнута — лесенка снимается, дальше работает обычный механизм закрытия.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .bybit_client import BybitClient, BybitError, ORDER_GONE_CODES
from .closer import _floor_to_step, fmt_price
from .config import Config
from .pnl import Key, PositionView

log = logging.getLogger("ladder")

LINK_PREFIX = "m2p-tp-"
ERR_LOG_EVERY_SEC = 30.0


@dataclass
class Slice:
    price: Decimal
    qty: Decimal


def _round_price(price: float, tick: Decimal, up: bool) -> Decimal:
    """Цена кратно шагу: продаём (выше рынка) — вверх, покупаем (ниже рынка) — вниз, чтобы не стать ближе к рынку, чем считали."""
    d = Decimal(repr(price)) / tick
    return d.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


def plan_ladder(side: str, size: float, mark: float, need_gain: float, book: dict | None, instrument: dict,
                cfg: Config, maker_fee: float = 0.0002) -> list[Slice]:
    """Куда и сколько ставить. side — сторона ПОЗИЦИИ: Buy (лонг) закрываем продажей выше рынка, Sell (шорт) — покупкой ниже.

    need_gain — сколько USDT результату дня ещё не хватает до цели (>0). Объём раскладывается по уровням лесенки
    пропорционально плотности стакана у каждого уровня и ограничивается долей `tp_book_fraction` от видимой заявки.
    """
    if need_gain <= 0 or size <= 0 or mark <= 0:
        return []
    long_ = side == "Buy"
    tick = Decimal(str(instrument["tick_size"]))
    step = Decimal(str(instrument["qty_step"]))
    min_qty = Decimal(str(instrument["min_qty"]))
    total = _floor_to_step(Decimal(repr(size)), step)
    if total < min_qty:
        return []
    # движение цены, нужное, чтобы эта позиция одна закрыла недостающее (+ комиссия мейкера на выходе)
    delta = (need_gain + maker_fee * mark * size) / size
    best_bid = book["bids"][0][0] if book else None
    best_ask = book["asks"][0][0] if book else None

    anchors: list[tuple[float, Decimal]] = []
    for c in cfg.tp_coefs:
        raw = mark + (c * delta if long_ else -c * delta)
        px = _round_price(raw, tick, up=long_)
        if book:   # пассивность: продажа должна стоять выше лучшего бида, покупка — ниже лучшего аска
            if long_ and px <= Decimal(repr(best_bid)):
                px = Decimal(repr(best_bid)) + tick
            if not long_ and px >= Decimal(repr(best_ask)):
                px = Decimal(repr(best_ask)) - tick
        if px > 0:
            anchors.append((c, px))
    if not anchors:
        return []

    # плотность стакана вокруг каждого уровня: уровни на «выходной» стороне, начиная с цены якоря и дальше от рынка
    levels = []
    if book:
        side_book = book["asks"] if long_ else book["bids"]
        levels = [(Decimal(repr(p)), Decimal(repr(q))) for p, q in side_book]
    window = Decimal(str(cfg.tp_density_window_bps)) / Decimal(10_000)
    per_anchor: list[list[tuple[Decimal, Decimal]]] = []    # [(цена, ёмкость)]
    for _, px in anchors:
        lim = px * (1 + window) if long_ else px * (1 - window)
        near = [(p, q * Decimal(str(cfg.tp_book_fraction))) for p, q in levels
                if (px <= p <= lim if long_ else lim <= p <= px)]
        near.sort(key=lambda x: x[0], reverse=not long_)               # ближе к рынку сначала
        per_anchor.append(near or [(px, total)])                      # стакан не доходит до уровня — один ордер на уровне

    # доли между уровнями лесенки ∝ суммарной ёмкости стакана около каждого
    caps = [sum(q for _, q in lv) for lv in per_anchor]
    caps = [c if c > 0 else total for c in caps]
    tot_cap = sum(caps)
    slots = max(1, cfg.tp_max_orders // len(anchors))
    out: dict[Decimal, Decimal] = {}
    for lv, cap in zip(per_anchor, caps):
        share = total * cap / tot_cap
        lv = sorted(lv, key=lambda x: -x[1])[:slots]                     # самые плотные уровни, не больше slots
        lcap = sum(q for _, q in lv) or share
        for p, q in lv:
            out[p] = out.get(p, Decimal(0)) + _floor_to_step(share * (q / lcap), step)
    # остаток округления — на ближайший к рынку уровень; слишком мелкие куски сливаем с ближайшим
    order = sorted(out, reverse=not long_)
    rest = total - sum(out.values())
    if rest > 0:
        out[order[0]] += _floor_to_step(rest, step)
    small = [p for p in order if out[p] < min_qty]
    for p in small:
        q = out.pop(p)
        keep = [x for x in sorted(out, key=lambda x: abs(x - p))]
        if keep:
            out[keep[0]] += q
        else:
            out[p] = q
    return [Slice(p, out[p]) for p in sorted(out, reverse=not long_) if out[p] >= min_qty]


class TakeProfitLadder:
    """Держит на бирже лесенку тейк-профитов: ставит, переставляет (с гистерезисом) и снимает."""

    def __init__(self, client: BybitClient, cfg: Config, instrument_fn, open_orders_fn, fee_fn=None):
        self.client = client
        self.cfg = cfg
        self.instrument = instrument_fn          # symbol -> {tick_size, qty_step, min_qty, ...}
        self.open_orders = open_orders_fn        # () -> список активных ордеров аккаунта
        self.fee = fee_fn or (lambda symbol: 0.0002)
        self._seq = 0
        self._err_at: dict[str, float] = {}
        self.active_keys: set[Key] = set()
        self._cache: dict[Key, tuple[list[float], float, list[Slice]]] = {}   # план на позицию: (якоря, размер, уровни)

    # ---------- основной шаг ----------
    def tick(self, positions: list[PositionView], books: dict[str, dict | None], need_gain: float) -> None:
        mine = self._my_orders()
        if need_gain <= 0 or not positions:
            self.cancel_all(mine)                 # цель достигнута (или позиций нет): лесенка не нужна
            return
        live_keys = {p.key for p in positions}
        for k in [k for k in self._cache if k not in live_keys]:
            del self._cache[k]
        for key, orders in mine.items():           # позиция закрылась — ордера лишние
            if key not in live_keys:
                for o in orders:
                    self._cancel(o)
        for p in positions:
            try:
                self._reconcile(p, books.get(p.symbol), need_gain, mine.get(p.key, []))
            except BybitError as e:
                self._error(p.symbol, e)
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка лесенки", p.symbol)
        self.active_keys = live_keys

    def cancel_all(self, mine: dict[Key, list[dict]] | None = None) -> None:
        mine = mine if mine is not None else self._my_orders()
        for orders in mine.values():
            for o in orders:
                self._cancel(o)

    # ---------- внутреннее ----------
    def _my_orders(self) -> dict[Key, list[dict]]:
        out: dict[Key, list[dict]] = {}
        for o in self.open_orders():
            if str(o.get("orderLinkId", "")).startswith(LINK_PREFIX):
                out.setdefault((o["symbol"], int(o.get("positionIdx") or 0)), []).append(o)
        return out

    def _cancel(self, o: dict) -> None:
        try:
            self.client.cancel_order(self.cfg.category, o["symbol"], o["orderId"])
        except BybitError as e:
            if e.code not in ORDER_GONE_CODES:
                self._error(o["symbol"], e)

    def _error(self, symbol: str, e: BybitError) -> None:
        now = time.monotonic()
        if now - self._err_at.get(symbol, -1e9) >= ERR_LOG_EVERY_SEC:
            self._err_at[symbol] = now
            log.error("%s: лесенка тейк-профитов — %s%s", symbol, e,
                      "  (ключ не имеет права торговать этой монетой)" if e.code == 10029 else "")

    def _reconcile(self, p: PositionView, book: dict | None, need_gain: float, existing: list[dict]) -> None:
        inst = self.instrument(p.symbol)
        tick = Decimal(str(inst["tick_size"]))
        want = self._plan(p, book, need_gain, inst, tick)
        if not want:
            for o in existing:
                self._cancel(o)
            return
        close_side = p.close_side
        free = list(existing)
        keep_tol = tick * self.cfg.tp_replace_ticks
        to_place: list[Slice] = []
        amends: list[tuple[dict, Slice]] = []
        for sl in want:
            best = min(free, key=lambda o: abs(Decimal(str(o["price"])) - sl.price), default=None)
            if best is None:
                to_place.append(sl)
                continue
            free.remove(best)
            same_price = abs(Decimal(str(best["price"])) - sl.price) <= keep_tol
            q_old = Decimal(str(best.get("leavesQty") or best["qty"]))
            same_qty = abs(q_old - sl.qty) <= max(sl.qty * Decimal("0.4"), Decimal(str(inst["min_qty"])))   # объём — с широким допуском: перекладка по плотности не должна терять очередь
            if not (same_price and same_qty):
                amends.append((best, sl))
        budget = self.cfg.tp_actions_per_tick
        for o in free:                           # лишние снимаем в первую очередь (освобождают объём под reduce-only)
            self._cancel(o)
        for o, sl in amends[:budget]:
            self._amend(p, o, sl)
        budget = max(0, budget - len(amends))
        for sl in to_place[: max(budget, 1)]:
            self._place(p, close_side, sl)

    def _plan(self, p: PositionView, book: dict | None, need_gain: float, inst: dict, tick: Decimal) -> list[Slice]:
        """План уровней с гистерезисом: пока якоря сместились не больше допуска, а размер позиции тот же — прежний план."""
        fee = self.fee(p.symbol)
        delta = (need_gain + fee * p.mark_price * p.size) / p.size if p.size > 0 else 0.0
        sign = 1 if p.side == "Buy" else -1
        anchors = [p.mark_price + sign * c * delta for c in self.cfg.tp_coefs]
        tol = float(tick) * self.cfg.tp_replace_ticks
        old = self._cache.get(p.key)
        if old and old[1] == p.size and len(old[0]) == len(anchors) and all(abs(a - b) <= tol for a, b in zip(anchors, old[0])):
            return old[2]
        want = plan_ladder(p.side, p.size, p.mark_price, need_gain, book, inst, self.cfg, fee)
        self._cache[p.key] = (anchors, p.size, want)
        return want

    def _amend(self, p: PositionView, o: dict, sl: Slice) -> None:
        tick = self.instrument(p.symbol)["tick_size"]
        try:
            self.client.amend_order(self.cfg.category, p.symbol, o["orderId"], fmt_price(float(sl.price), tick),
                                    format(sl.qty.normalize(), "f"))
        except BybitError as e:
            if e.code not in ORDER_GONE_CODES:
                self._error(p.symbol, e)

    def _place(self, p: PositionView, close_side: str, sl: Slice) -> None:
        self._seq += 1
        tick = self.instrument(p.symbol)["tick_size"]
        link = f"{LINK_PREFIX}{int(time.time())}-{self._seq}"
        self.client.place_limit_order(self.cfg.category, p.symbol, close_side, format(sl.qty.normalize(), "f"),
                                      fmt_price(float(sl.price), tick), p.position_idx, link, True)
        log.info("%s %s: тейк-профит qty=%s @ %s (недостающее до цели по позиции)", p.symbol, close_side,
                 format(sl.qty.normalize(), "f"), fmt_price(float(sl.price), tick))
