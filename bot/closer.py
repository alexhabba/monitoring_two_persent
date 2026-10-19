"""Постепенное закрытие позиции post-only reduce-only лимитками.

Размер одного ордера ограничен долей ликвидности стакана рядом с лучшей ценой,
чтобы не сдвигать рынок. Ордер ставится на лучшую цену своей стороны
(лонг продаём по best ask, шорт покупаем по best bid) — это мейкер, он
не «съедает» стакан.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Callable

from .bybit_client import BybitClient
from .config import Config
from .pnl import PositionView, best_prices

log = logging.getLogger("closer")

FINAL = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}


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

    def instrument(self, symbol: str) -> dict:
        if symbol not in self._instruments:
            self._instruments[symbol] = self.client.get_instrument(self.cfg.category, symbol)
        return self._instruments[symbol]

    def close_chunk(self, p: PositionView, book: dict, fraction: float | None = None) -> float:
        """Ставит один лимитный ордер на кусок позиции. Возвращает исполненный объём."""
        plan = plan_chunk(p, book, self.instrument(p.symbol), self.cfg, fraction)
        if plan is None:
            return 0.0
        price_str = repr(plan.price) if plan.price != int(plan.price) else str(int(plan.price))
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
