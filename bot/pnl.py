"""Расчёт чистого PnL по позициям: с комиссией на выход и учётом фандинга.

Чистый PnL позиции = (цена выхода - цена входа) * размер   [для шорта наоборот]
                   + cumRealisedPnl                          [уже содержит комиссию за вход и фандинг]
                   - комиссия за выход

cumRealisedPnl в Bybit V5 — накопленный реализованный PnL текущей позиции,
включая комиссию открытия, фандинг и уже закрытые части.
Цена выхода считается консервативно: по противоположной стороне стакана
(для лонга — best bid, для шорта — best ask) со штрафом на проскальзывание,
комиссия — по тейкер-ставке, даже если реально закрываем мейкером.
"""
from __future__ import annotations

from dataclasses import dataclass, field

Key = tuple[str, int]  # (symbol, positionIdx)


@dataclass
class PositionView:
    symbol: str
    side: str  # "Buy" = лонг, "Sell" = шорт
    size: float
    avg_price: float
    mark_price: float
    cum_realised: float
    position_idx: int
    position_im: float  # начальная маржа

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
    )


def best_prices(book: dict) -> tuple[float, float]:
    return book["bids"][0][0], book["asks"][0][0]


def exit_price(side: str, bid: float, ask: float, slippage_bps: float) -> float:
    k = slippage_bps / 10_000
    return bid * (1 - k) if side == "Buy" else ask * (1 + k)


def position_net_pnl(p: PositionView, bid: float, ask: float,
                     taker_fee: float, slippage_bps: float) -> float:
    px = exit_price(p.side, bid, ask, slippage_bps)
    gross = (px - p.avg_price) * p.size if p.side == "Buy" else (p.avg_price - px) * p.size
    exit_fee = px * p.size * taker_fee
    return gross + p.cum_realised - exit_fee


@dataclass
class Evaluation:
    per_position: dict[Key, float] = field(default_factory=dict)
    locked: float = 0.0  # уже зафиксировано полностью закрытыми позициями
    total_net: float = 0.0
    base: float = 0.0
    pct: float = 0.0


def profit_base(mode: str, equity: float, positions: list[PositionView]) -> float:
    if mode == "margin":
        return sum(p.position_im for p in positions)
    return equity


def evaluate(positions: list[PositionView], books: dict[str, dict],
             taker_fees: dict[str, float], base: float,
             locked: dict[Key, float], slippage_bps: float) -> Evaluation:
    ev = Evaluation(locked=sum(locked.values()), base=base)
    for p in positions:
        bid, ask = best_prices(books[p.symbol])
        ev.per_position[p.key] = position_net_pnl(p, bid, ask, taker_fees[p.symbol], slippage_bps)
    ev.total_net = sum(ev.per_position.values()) + ev.locked
    ev.pct = ev.total_net / base * 100 if base > 0 else 0.0
    return ev
