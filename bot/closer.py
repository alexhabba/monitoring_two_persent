"""Постепенное закрытие позиции post-only reduce-only лимитками.

Размер одного ордера ограничен долей ликвидности стакана рядом с лучшей ценой,
чтобы не сдвигать рынок. Ордер ставится на лучшую цену своей стороны
(лонг продаём по best ask, шорт покупаем по best bid) — это мейкер, он
не «съедает» стакан.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Callable

from . import logctx
from .bybit_client import BybitClient, BybitError, ORDER_GONE_CODES
from .config import Config
from .pnl import Key, PositionView, best_prices

log = logging.getLogger("closer")

FINAL = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}


@dataclass
class Resting:
    """Наш живой ордер на закрытие позиции."""
    order_id: str
    price: float
    qty: Decimal


ERR_LOG_EVERY_SEC = 30.0


@dataclass
class ChunkPlan:
    side: str
    qty: Decimal
    price: float


def book_depth(book: dict, close_side: str, within_bps: float) -> float:
    """Ликвидность, которую «задели» бы, закрываясь агрессивно.

    Продаём (закрываем лонг) -> смотрим биды; покупаем (закрываем шорт) -> смотрим аски.
    """
    levels = book["bids"] if close_side == "Sell" else book["asks"]
    if not levels:
        return 0.0
    best = levels[0][0]
    k = within_bps / 10_000
    limit = best * (1 - k) if close_side == "Sell" else best * (1 + k)
    total = 0.0
    for price, qty in levels:
        if (close_side == "Sell" and price < limit) or (close_side == "Buy" and price > limit):
            break
        total += qty
    return total


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def plan_chunk(p: PositionView, book: dict, instrument: dict, cfg: Config,
               fraction: float | None = None) -> ChunkPlan | None:
    if not book["bids"] or not book["asks"]:
        return None
    bid, ask = best_prices(book)
    spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
    if spread_bps > cfg.max_spread_bps:
        log.info("%s: спред %.1f б.п. > %.1f — пропускаем", p.symbol, spread_bps, cfg.max_spread_bps)
        return None

    step = Decimal(instrument["qty_step"])
    min_qty = Decimal(str(instrument["min_qty"]))
    remaining = Decimal(str(p.size))

    if fraction is not None:  # парная нога хеджа: та же доля позиции, что закрыта на первой ноге
        qty = _floor_to_step(remaining * Decimal(str(fraction)), step)
    else:
        depth = book_depth(book, p.close_side, cfg.book_depth_bps)
        qty = _floor_to_step(Decimal(str(depth * cfg.book_fraction)), step)
    if fraction is not None and qty < min_qty:
        return None  # слишком мало для биржи — догоним на следующем шаге
    qty = max(qty, min_qty)
    qty = min(qty, remaining)
    if remaining - qty < min_qty:  # не оставляем «пыль», которую нельзя закрыть
        qty = remaining
    if qty <= 0:
        return None

    price = ask if p.close_side == "Sell" else bid  # встаём в очередь своей стороны
    return ChunkPlan(side=p.close_side, qty=qty, price=price)


def fmt_price(price: float, tick: str) -> str:
    """Цена строкой, кратной шагу цены монеты (без хвостов плавающей точки вроде 99.89999999999999)."""
    t = Decimal(str(tick))
    d = (Decimal(repr(price)) / t).to_integral_value() * t
    return format(d.normalize(), "f")


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s


class PositionCloser:
    def __init__(self, client: BybitClient, cfg: Config,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.cfg = cfg
        self.sleep = sleep
        self._instruments: dict[str, dict] = {}
        self._seq = 0
        self.resting: dict[Key, Resting] = {}
        self._err_at: dict[str, float] = {}
        self._dry_logged: dict[Key, str] = {}
        self.account: str | None = None      # имя копирующего счёта — для меток в логе

    def instrument(self, symbol: str) -> dict:
        if symbol not in self._instruments:
            self._instruments[symbol] = self.client.get_instrument(self.cfg.category, symbol)
        return self._instruments[symbol]

    def close_chunk(self, p: PositionView, book: dict, fraction: float | None = None) -> float:
        """Ставит один лимитный ордер на кусок позиции. Возвращает исполненный объём."""
        plan = plan_chunk(p, book, self.instrument(p.symbol), self.cfg, fraction)
        if plan is None:
            return 0.0
        price_str = fmt_price(plan.price, self.instrument(p.symbol)["tick_size"])
        log.info("%s %s: chunk qty=%s @ %s (остаток %s)%s", p.symbol, plan.side, _fmt(plan.qty),
                 price_str, p.size, "  [DRY-RUN]" if self.cfg.dry_run else "")
        if self.cfg.dry_run:
            return 0.0

        self._seq += 1
        link_id = f"m2p-{int(time.time())}-{self._seq}"
        order_id = self.client.place_limit_close(
            self.cfg.category, p.symbol, plan.side, _fmt(plan.qty), price_str, p.position_idx, link_id)

        deadline = time.time() + self.cfg.order_timeout_sec
        order = None
        while time.time() < deadline:
            self.sleep(1.0)
            order = self.client.get_order(self.cfg.category, p.symbol, order_id)
            if order and order.get("orderStatus") in FINAL:
                break
        else:
            self.client.cancel_order(self.cfg.category, p.symbol, order_id)
            self.sleep(0.3)
            order = self.client.get_order(self.cfg.category, p.symbol, order_id)

        filled = float(order.get("cumExecQty") or 0) if order else 0.0
        log.info("%s: исполнено %.8g из %s", p.symbol, filled, _fmt(plan.qty))
        return filled

    # ---------- непрерывное закрытие: все позиции сразу, проверка каждую секунду ----------
    def close_all(self, positions: list[PositionView], books: dict[str, dict]) -> None:
        """Один «тик» закрытия (вызывается раз в секунду). positions — уже по приоритету (первые — важнее).

        По каждой позиции параллельно: нет живого ордера — ставим post-only reduce-only лимитку на лучшую цену своей стороны;
        есть, но цена ушла (мы уже не первые в очереди) — переставляем ближе к цене одним запросом amend;
        стоит на лучшей цене — не трогаем (сохраняем место в очереди). Исполнение не ждём: статус смотрим на следующем тике.
        """
        cfg = self.cfg
        if not positions:
            return
        fractions = self._hedge_fractions(positions, books)
        if cfg.dry_run:
            for p in positions:
                plan = plan_chunk(p, books[p.symbol], self.instrument(p.symbol), cfg, fractions.get(p.key))
                if plan is not None:
                    text = f"{p.symbol} {plan.side}: chunk qty={_fmt(plan.qty)} @ {plan.price} (остаток {p.size})"
                    if self._dry_logged.get(p.key) != text:
                        self._dry_logged[p.key] = text
                        log.info("%s  [DRY-RUN]", text)
            return
        self._sync_open_orders()
        workers = max(1, min(cfg.close_workers, len(positions)))
        if workers == 1:
            for p in positions:
                self._safe_work(p, books[p.symbol], fractions.get(p.key))
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="close",
                                initializer=logctx.set_account, initargs=(self.account,)) as ex:
            futs = [ex.submit(self._safe_work, p, books[p.symbol], fractions.get(p.key)) for p in positions]
            for f in futs:
                f.result()

    def _hedge_fractions(self, positions: list[PositionView], books: dict[str, dict]) -> dict[Key, float]:
        """Обе ноги одного символа закрываем в одной и той же доле, чтобы не остаться с перекошенным хеджем."""
        by_symbol: dict[str, list[PositionView]] = defaultdict(list)
        for p in positions:
            by_symbol[p.symbol].append(p)
        out: dict[Key, float] = {}
        for sym, legs in by_symbol.items():
            if len(legs) < 2:
                continue
            fr = []
            for p in legs:
                plan = plan_chunk(p, books[sym], self.instrument(sym), self.cfg)
                if plan is None:
                    fr = []
                    break
                fr.append(min(float(plan.qty) / p.size, 1.0))
            if fr:
                for p in legs:
                    out[p.key] = min(fr)
        return out

    def _sync_open_orders(self) -> None:
        """Сверяем свои ордера с биржей: исполненные/отменённые забываем, оставшиеся от прошлого запуска подхватываем."""
        orders = self.client.get_open_orders(self.cfg.category, self.cfg.settle_coin)
        open_ids = {o["orderId"]: o for o in orders}
        for key, rest in list(self.resting.items()):
            if rest.order_id not in open_ids:
                del self.resting[key]
        for o in orders:
            key = (o["symbol"], int(o.get("positionIdx") or 0))
            if (key not in self.resting and str(o.get("orderLinkId", "")).startswith("m2p-")
                    and str(o.get("reduceOnly")).lower() == "true"):
                self.resting[key] = Resting(o["orderId"], float(o["price"]), Decimal(str(o.get("leavesQty") or o["qty"])))

    def _safe_work(self, p: PositionView, book: dict, fraction: float | None) -> None:
        try:
            self._work(p, book, fraction)
        except BybitError as e:
            if e.code in ORDER_GONE_CODES:
                self.resting.pop(p.key, None)
            now = time.monotonic()
            if now - self._err_at.get(p.symbol, -1e9) >= ERR_LOG_EVERY_SEC:   # не засыпаем лог одной и той же ошибкой
                self._err_at[p.symbol] = now
                log.error("%s: не удалось закрывать — %s%s", p.symbol, e,
                          "  (ключ не имеет права торговать этой монетой — закройте вручную или поправьте whitelist ключа)"
                          if e.code == 10029 else "")
        except Exception:
            log.exception("Ошибка при закрытии %s", p.symbol)

    def _work(self, p: PositionView, book: dict, fraction: float | None) -> None:
        plan = plan_chunk(p, book, self.instrument(p.symbol), self.cfg, fraction)
        rest = self.resting.get(p.key)
        if plan is None:
            return
        price_str = fmt_price(plan.price, self.instrument(p.symbol)["tick_size"])
        if rest is not None:
            behind = rest.price > plan.price if p.close_side == "Sell" else rest.price < plan.price
            too_big = rest.qty > Decimal(str(p.size))
            if not behind and not too_big:
                return    # стоим на лучшей цене — не трогаем, чтобы не терять очередь
            try:
                self.client.amend_order(self.cfg.category, p.symbol, rest.order_id, price_str, _fmt(plan.qty))
                log.info("%s %s: цена %s → %s, qty %s (остаток %s)", p.symbol, plan.side, rest.price, price_str,
                         _fmt(plan.qty), p.size)
                rest.price, rest.qty = plan.price, plan.qty
                return
            except BybitError as e:
                if e.code not in ORDER_GONE_CODES:   # например post-only пересёк рынок: снимем и поставим на следующем тике
                    self.resting.pop(p.key, None)
                    self.client.cancel_order(self.cfg.category, p.symbol, rest.order_id)
                    log.info("%s: amend не прошёл (%s) — ордер снят, поставим заново", p.symbol, e)
                    return
                self.resting.pop(p.key, None)    # ордер уже исполнился/исчез — ставим новый ниже
        self._seq += 1
        link_id = f"m2p-{int(time.time())}-{self._seq}"
        order_id = self.client.place_limit_close(
            self.cfg.category, p.symbol, plan.side, _fmt(plan.qty), price_str, p.position_idx, link_id)
        self.resting[p.key] = Resting(order_id, plan.price, plan.qty)
        log.info("%s %s: chunk qty=%s @ %s (остаток %s)", p.symbol, plan.side, _fmt(plan.qty), price_str, p.size)

    def cancel_all(self) -> None:
        """Снять все наши ордера на закрытие (когда цель снята и закрывать больше не нужно)."""
        if self.cfg.dry_run:
            return
        try:
            self._sync_open_orders()   # подхватит и «осиротевшие» ордера от прошлого запуска
        except BybitError:
            pass
        for key, rest in list(self.resting.items()):
            try:
                self.client.cancel_order(self.cfg.category, key[0], rest.order_id)
                log.info("%s: ордер на закрытие снят", key[0])
            except BybitError as e:
                log.warning("%s: не удалось снять ордер: %s", key[0], e)
            self.resting.pop(key, None)
