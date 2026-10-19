"""Оценка торгового результата дня «как если бы всё закрыли сейчас».

Результат дня = реализованный (лог транзакций: сделки, комиссии, фандинг)
              + изменение нереализованного PnL с начала дня (unrealisedPnl сейчас − на начало дня)
              + поправка на выход.

Баланс счёта в расчёте не участвует (кроме знаменателя — базы дня), поэтому пополнения, выводы,
переводы и изменение цены обеспечения не влияют. Поправка на выход по каждой позиции:

    поправка = (цена выхода − mark) × размер × знак  −  комиссия выхода (тейкер)

Цена выхода консервативна: лонг по best bid, шорт по best ask, со штрафом на проскальзывание.
"""
from __future__ import annotations

from dataclasses import dataclass

Key = tuple[str, int]  # (symbol, positionIdx) — в хедж-режиме лонг и шорт это разные позиции


@dataclass
class PositionView:
    symbol: str
    side: str  # "Buy" = лонг, "Sell" = шорт
    size: float
    avg_price: float
    mark_price: float
    cum_realised: float
    position_idx: int
    position_im: float
    unrealised_pnl: float = 0.0

    @property
    def key(self) -> Key:
        return (self.symbol, self.position_idx)

    @property
    def close_side(self) -> str:
        return "Sell" if self.side == "Buy" else "Buy"


def parse_position(raw: dict) -> PositionView:
    return PositionView(
        symbol=raw["symbol"],
        side=raw["side"],
        size=float(raw["size"]),
        avg_price=float(raw.get("avgPrice") or 0),
        mark_price=float(raw.get("markPrice") or 0),
        cum_realised=float(raw.get("cumRealisedPnl") or 0),
        position_idx=int(raw.get("positionIdx") or 0),
        position_im=float(raw.get("positionIM") or 0),
        unrealised_pnl=float(raw.get("unrealisedPnl") or 0),
    )


def best_prices(book: dict) -> tuple[float, float]:
    return book["bids"][0][0], book["asks"][0][0]


def exit_price(side: str, bid: float, ask: float, slippage_bps: float) -> float:
    k = slippage_bps / 10_000
    return bid * (1 - k) if side == "Buy" else ask * (1 + k)


def exit_adjustment(p: PositionView, bid: float, ask: float, taker_fee: float, slippage_bps: float) -> float:
    px = exit_price(p.side, bid, ask, slippage_bps)
    mark = p.mark_price if p.mark_price > 0 else (bid + ask) / 2
    delta = (px - mark) * p.size if p.side == "Buy" else (mark - px) * p.size
    return delta - px * p.size * taker_fee


@dataclass
class Evaluation:
    equity: float = 0.0          # баланс счёта — только для справки в логе
    start_equity: float = 0.0    # база дня (знаменатель процента)
    realized: float = 0.0
    unrealised: float = 0.0
    unrealised_delta: float = 0.0
    adjustment: float = 0.0
    gain: float = 0.0            # итоговый результат дня с учётом выхода
    pct: float = 0.0
    goal: bool = False


def evaluate(start_equity: float, start_unrealised: float, realized: float,
             positions: list[PositionView], books: dict[str, dict],
             taker_fees: dict[str, float], slippage_bps: float, equity: float = 0.0) -> Evaluation:
    adj = 0.0
    for p in positions:
        bid, ask = best_prices(books[p.symbol])
        adj += exit_adjustment(p, bid, ask, taker_fees[p.symbol], slippage_bps)
    unreal = sum(p.unrealised_pnl for p in positions)
    delta = unreal - start_unrealised
    gain = realized + delta + adj
    pct = gain / start_equity * 100 if start_equity > 0 else 0.0
    return Evaluation(equity, start_equity, realized, unreal, delta, adj, gain, pct)
