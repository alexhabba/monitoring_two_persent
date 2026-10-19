from decimal import Decimal

from bot.closer import PositionCloser, book_depth, plan_chunk
from bot.config import Config
from bot.monitor import Bot
from bot.pnl import PositionView, evaluate, position_net_pnl

INSTR = {"min_qty": 0.001, "qty_step": "0.001", "tick_size": "0.1"}
BOOK = {
    "bids": [(100.0, 5.0), (99.95, 5.0), (99.0, 100.0)],
    "asks": [(100.1, 4.0), (100.15, 6.0), (101.0, 100.0)],
}


def pos(side="Buy", size=1.0, avg=90.0, cum=-0.05, idx=0, sym="BTCUSDT", im=10.0):
    return PositionView(sym, side, size, avg, 100.0, cum, idx, im)


def cfg(**kw):
    return Config(**{"dry_run": False, **kw})


def test_long_net_pnl_includes_fees_and_funding():
    # выход по bid 100, slippage 0, taker 0.055%; вход 90; уже реализовано -0.5 (комиссия входа + фандинг)
    net = position_net_pnl(pos(cum=-0.5), 100.0, 100.1, 0.00055, 0)
    assert abs(net - (10.0 - 0.5 - 100.0 * 0.00055)) < 1e-9


def test_short_net_pnl():
    net = position_net_pnl(pos(side="Sell", avg=110.0, cum=0.2), 100.0, 100.1, 0.00055, 0)
    assert abs(net - ((110.0 - 100.1) + 0.2 - 100.1 * 0.00055)) < 1e-9


def test_slippage_reduces_pnl():
    a = position_net_pnl(pos(), 100.0, 100.1, 0.0005, 0)
    b = position_net_pnl(pos(), 100.0, 100.1, 0.0005, 10)
    assert b < a


def test_evaluate_pct_and_locked():
    ev = evaluate([pos()], {"BTCUSDT": BOOK}, {"BTCUSDT": 0.0}, 500.0,
                  {("ETHUSDT", 0): 2.0}, 0)
    assert abs(ev.total_net - (10.0 - 0.05 + 2.0)) < 1e-9
    assert abs(ev.pct - ev.total_net / 500 * 100) < 1e-9


def test_book_depth_only_within_bps():
    # 10 б.п. от 100.0 = 99.9 -> учитываются 100.0 и 99.95
    assert book_depth(BOOK, "Sell", 10) == 10.0
    assert book_depth(BOOK, "Buy", 10) == 10.0  # 100.1 и 100.15 (<=100.2)


def test_chunk_is_fraction_of_depth_and_joins_own_side():
    plan = plan_chunk(pos(size=50.0), BOOK, INSTR, cfg(book_fraction=0.1))
    assert plan.side == "Sell" and plan.price == 100.1  # лонг: встаём на best ask
    assert plan.qty == Decimal("1.000")  # 10% от 10


def test_chunk_never_exceeds_remaining_and_avoids_dust():
    plan = plan_chunk(pos(size=1.0005), BOOK, {**INSTR, "qty_step": "0.0001"}, cfg(book_fraction=0.1))
    assert plan.qty == Decimal("1.0005")  # остаток после куска < min_qty -> берём всё
    plan = plan_chunk(pos(size=0.5), BOOK, INSTR, cfg(book_fraction=0.1))
    assert plan.qty == Decimal("0.5")


def test_wide_spread_skips():
    wide = {"bids": [(100.0, 5)], "asks": [(101.0, 5)]}
    assert plan_chunk(pos(), wide, INSTR, cfg(max_spread_bps=20)) is None


# ---------- интеграция на фейковом клиенте ----------
class FakeClient:
    def __init__(self, size=1.0, avg=90.0):
        self.size, self.avg = size, avg
        self.cum = -0.05
        self.orders = []

    def get_positions(self, category, settle):
        if self.size <= 0:
            return []
        return [{"symbol": "BTCUSDT", "side": "Buy", "size": str(self.size), "avgPrice": str(self.avg),
                 "markPrice": "100", "cumRealisedPnl": str(self.cum), "positionIdx": 0, "positionIM": "10"}]

    def get_equity(self, t): return 500.0
    def get_fee_rates(self, c, s): return 0.0002, 0.00055
    def get_orderbook(self, c, s, limit=50): return BOOK
    def get_instrument(self, c, s): return INSTR

    def place_limit_close(self, category, symbol, side, qty, price, idx, link):
        self.orders.append((side, qty, price, idx))
        self.size -= float(qty)
        self.cum += (float(price) - self.avg) * float(qty)  # биржа реализует PnL закрытой части
        return "oid1"

    def get_order(self, c, s, oid): return {"orderStatus": "Filled", "cumExecQty": self.orders[-1][1]}
    def cancel_order(self, *a): pass


def make_bot(client, **kw):
    c = cfg(**kw)
    return Bot(client, c, PositionCloser(client, c, sleep=lambda s: None))


def test_below_target_no_orders():
    cl = FakeClient(avg=99.9)  # прибыль ≈0.1 на 500 = 0.02%
    bot = make_bot(cl, target_profit_pct=2.0)
    ev = bot.step()
    assert ev.pct < 2.0 and not cl.orders and not bot.closing


def test_target_reached_closes_in_chunks_reduce_only_until_flat():
    cl = FakeClient(size=3.0, avg=90.0)  # ≈ +30 на 500 = ~6%
    bot = make_bot(cl, target_profit_pct=2.0)
    for _ in range(10):
        bot.step()
        if cl.size <= 0:
            break
    assert cl.orders and all(o[0] == "Sell" for o in cl.orders)
    assert all(float(o[1]) <= 1.0 + 1e-9 for o in cl.orders)  # не больше 10% глубины
    assert cl.size <= 1e-9
    assert bot.step() is None  # всё закрыто, бот вернулся в ожидание


def test_dry_run_sends_nothing():
    cl = FakeClient(size=3.0, avg=90.0)
    bot = make_bot(cl, dry_run=True)
    bot.step()
    assert bot.closing and not cl.orders


def test_closing_stops_when_pnl_drops_below_target():
    cl = FakeClient(size=3.0, avg=90.0)
    bot = make_bot(cl, target_profit_pct=2.0)
    bot.step()
    cl.avg = 100.0  # прибыль исчезла
    bot.step()
    assert not bot.closing


# ---------- хедж: обе ноги по одному символу ----------
class HedgeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.legs = {1: ["Buy", 3.0, 90.0, -0.05], 2: ["Sell", 2.0, 110.0, -0.05]}

    def get_positions(self, category, settle):
        return [{"symbol": "BTCUSDT", "side": s, "size": str(round(z, 8)), "avgPrice": str(a),
                 "markPrice": "100", "cumRealisedPnl": str(c), "positionIdx": i, "positionIM": "5"}
                for i, (s, z, a, c) in self.legs.items() if z > 1e-9]

    def place_limit_close(self, category, symbol, side, qty, price, idx, link):
        self.orders.append((side, qty, price, idx))
        leg = self.legs[idx]
        leg[1] -= float(qty)
        leg[3] += ((float(price) - leg[2]) if leg[0] == "Buy" else (leg[2] - float(price))) * float(qty)
        return f"oid{len(self.orders)}"

    def get_order(self, c, s, oid): return {"orderStatus": "Filled", "cumExecQty": self.orders[-1][1]}


def test_hedge_legs_closed_proportionally_with_correct_idx_and_sides():
    cl = HedgeClient()
    bot = make_bot(cl, target_profit_pct=1.0)
    bot.step()
    assert len(cl.orders) == 2
    by_idx = {o[3]: o for o in cl.orders}
    assert by_idx[1][0] == "Sell" and by_idx[2][0] == "Buy"
    long_frac = float(by_idx[1][1]) / 3.0
    short_frac = float(by_idx[2][1]) / 2.0
    assert abs(long_frac - short_frac) < 0.01  # хедж не перекошен


def test_hedge_both_legs_end_flat():
    cl = HedgeClient()
    bot = make_bot(cl, target_profit_pct=1.0)
    for _ in range(40):
        bot.step()
        if not cl.get_positions("linear", "USDT"):
            break
    assert all(leg[1] < 1e-9 for leg in cl.legs.values())
