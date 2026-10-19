import os
from datetime import date, datetime, timedelta
from decimal import Decimal

from bot.closer import PositionCloser, book_depth, plan_chunk
from bot.config import Config
from bot.logsetup import DailyFileHandler
from bot.monitor import Bot
from bot.daypnl import DayPnlTracker
from bot.pnl import PositionView, exit_adjustment, evaluate
from bot.state import StateStore
from bot.timeutil import get_tz

MSK = get_tz("Europe/Moscow")
INSTR = {"min_qty": 0.001, "qty_step": "0.001", "tick_size": "0.1"}


def make_book(mark=100.0):
    return {
        "bids": [(mark, 5.0), (mark - 0.05, 5.0), (mark - 1.0, 100.0)],
        "asks": [(mark + 0.1, 4.0), (mark + 0.15, 6.0), (mark + 1.0, 100.0)],
    }


BOOK = make_book(100.0)


def pos(side="Buy", size=1.0, avg=90.0, idx=0, sym="BTCUSDT", mark=100.0):
    return PositionView(sym, side, size, avg, mark, 0.0, idx, 10.0)


def cfg(**kw):
    base = dict(dry_run=False, target_profit_pct=2.0)
    base.update(kw)
    return Config(**base)


# ---------- расчёт ----------
def test_exit_adjustment_long_and_short():
    # лонг: выход по bid 99.9 при mark 100 -> -0.1*size, минус комиссия
    adj = exit_adjustment(pos(size=2.0), 99.9, 100.0, 0.00055, 0)
    assert abs(adj - (-0.2 - 99.9 * 2 * 0.00055)) < 1e-9
    # шорт: выход по ask 100.1 при mark 100
    adj = exit_adjustment(pos(side="Sell", size=2.0), 99.9, 100.1, 0.00055, 0)
    assert abs(adj - (-0.2 - 100.1 * 2 * 0.00055)) < 1e-9


def test_slippage_reduces_projection():
    a = exit_adjustment(pos(), 100.0, 100.1, 0.0005, 0)
    b = exit_adjustment(pos(), 100.0, 100.1, 0.0005, 10)
    assert b < a


def test_evaluate_gain_is_realized_plus_unrealised_delta_plus_exit():
    p = pos(size=2.0)
    p.unrealised_pnl = 25.0
    ev = evaluate(500.0, 10.0, 5.0, [p], {"BTCUSDT": BOOK}, {"BTCUSDT": 0.0}, 0, equity=999.0)
    exit_adj = exit_adjustment(p, 100.0, 100.1, 0.0, 0)
    assert abs(ev.gain - (5.0 + (25.0 - 10.0) + exit_adj)) < 1e-9
    assert abs(ev.pct - ev.gain / 500.0 * 100) < 1e-9


def test_balance_is_not_used_in_gain():
    a = evaluate(500.0, 0.0, 0.0, [], {}, {}, 0, equity=500.0)
    b = evaluate(500.0, 0.0, 0.0, [], {}, {}, 0, equity=99999.0)
    assert a.gain == b.gain == 0.0


def test_book_depth_only_within_bps():
    assert book_depth(BOOK, "Sell", 10) == 10.0
    assert book_depth(BOOK, "Buy", 10) == 10.0


def test_chunk_is_fraction_of_depth_and_joins_own_side():
    plan = plan_chunk(pos(size=50.0), BOOK, INSTR, cfg(book_fraction=0.1))
    assert plan.side == "Sell" and plan.price == 100.1
    assert plan.qty == Decimal("1.000")


def test_chunk_never_exceeds_remaining_and_avoids_dust():
    plan = plan_chunk(pos(size=1.0005), BOOK, {**INSTR, "qty_step": "0.0001"}, cfg(book_fraction=0.1))
    assert plan.qty == Decimal("1.0005")
    plan = plan_chunk(pos(size=0.5), BOOK, INSTR, cfg(book_fraction=0.1))
    assert plan.qty == Decimal("0.5")


def test_wide_spread_skips():
    wide = {"bids": [(100.0, 5)], "asks": [(101.0, 5)]}
    assert plan_chunk(pos(), wide, INSTR, cfg(max_spread_bps=20)) is None


# ---------- фейковая биржа ----------
class FakeClient:
    """legs: idx -> [side, size, avg]. Баланс = база + реализованное + нереализованное + переводы + обеспечение."""

    def __init__(self, legs=None, base=500.0, mark=100.0):
        self.legs = legs if legs is not None else {0: ["Buy", 3.0, 100.0]}
        self.base, self.mark = base, mark
        self.realized = 0.0
        self.transfers = 0.0
        self.collateral_pnl = 0.0
        self.orders = []
        self.tx = []
        self.clock = None
        self.tx_calls = 0
        self._id = 0

    # --- журнал транзакций ---
    def _log(self, typ, change, ts_ms=None):
        self._id += 1
        t = ts_ms if ts_ms is not None else int(self.clock().timestamp() * 1000)
        self.tx.append({"id": f"t{self._id}", "type": typ, "change": str(change),
                        "transactionTime": str(t), "currency": "USDT"})

    def add_realized(self, amount, typ="TRADE", ts_ms=None):
        self.realized += amount
        self._log(typ, amount, ts_ms)

    def deposit(self, amount):
        self.transfers += amount
        self._log("TRANSFER_IN" if amount > 0 else "TRANSFER_OUT", amount)

    def get_transaction_log(self, account_type, category, currency, start_ms, end_ms):
        self.tx_calls += 1
        return [r for r in self.tx if start_ms <= int(r["transactionTime"]) <= end_ms]

    # --- остальное API ---
    def unreal(self):
        return sum((self.mark - a) * z if s == "Buy" else (a - self.mark) * z
                   for s, z, a in self.legs.values() if z > 1e-9)

    def get_equity(self, t):
        return self.base + self.realized + self.unreal() + self.transfers + self.collateral_pnl

    def get_positions(self, category, settle):
        out = []
        for i, (s, z, a) in self.legs.items():
            if z > 1e-9:
                u = (self.mark - a) * z if s == "Buy" else (a - self.mark) * z
                out.append({"symbol": "BTCUSDT", "side": s, "size": str(round(z, 8)), "avgPrice": str(a),
                            "markPrice": str(self.mark), "cumRealisedPnl": "0", "positionIdx": i,
                            "positionIM": "5", "unrealisedPnl": str(u)})
        return out

    def get_fee_rates(self, c, s): return 0.0002, 0.00055
    def get_orderbook(self, c, s, limit=50): return make_book(self.mark)
    def get_instrument(self, c, s): return INSTR

    def place_limit_close(self, category, symbol, side, qty, price, idx, link):
        leg = self.legs[idx]
        q = float(qty)
        self.orders.append((side, qty, price, idx))
        pnl = ((float(price) - leg[2]) if leg[0] == "Buy" else (leg[2] - float(price))) * q
        leg[1] -= q
        self.add_realized(pnl)       # реализованный PnL попадает в лог транзакций
        return f"oid{len(self.orders)}"

    def get_order(self, c, s, oid): return {"orderStatus": "Filled", "cumExecQty": self.orders[-1][1]}
    def cancel_order(self, *a): pass


class Clock:
    def __init__(self, dt): self.dt = dt
    def __call__(self): return self.dt
    def advance(self, **kw): self.dt = self.dt + timedelta(**kw)


def make_bot(client, tmp_path, clock=None, store=None, **kw):
    c = cfg(**kw)
    store = store or StateStore(str(tmp_path / "state.db"))
    clock = clock or Clock(datetime(2026, 10, 19, 14, 0, tzinfo=MSK))
    client.clock = clock
    return Bot(client, c, store, PositionCloser(client, c, sleep=lambda s: None), clock=clock), store, clock


# ---------- день и база ----------
def test_first_run_records_current_equity_as_base(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    row = store.get_day("2026-10-19")
    assert row["start_kind"] == "first_run" and abs(row["start_equity"] - 500.0) < 1e-9
    assert row["goal_reached"] == 0


def test_below_target_no_orders(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 101.0  # +9 из 500 -> ~1.8% до комиссий, ниже цели
    ev = bot.step()
    assert ev.pct < 2.0 and not cl.orders and not store.get_day("2026-10-19")["goal_reached"]


def test_goal_latched_then_everything_closed_and_final_recorded(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                       # база 500
    cl.mark = 104.0                  # +12 -> 2.4%
    for _ in range(12):
        bot.step()
        if all(l[1] <= 1e-9 for l in cl.legs.values()):
            break
    row = store.get_day("2026-10-19")
    assert row["goal_reached"] == 1 and row["goal_pct"] >= 2.0
    assert cl.orders and all(o[0] == "Sell" for o in cl.orders)
    assert all(float(o[1]) <= 1.0 + 1e-9 for o in cl.orders)   # не больше 10% глубины
    bot.step()
    row = store.get_day("2026-10-19")
    assert row["final_gain"] is not None and row["final_gain"] >= 500 * 0.02 - 1.0
    assert row["final_equity"] is not None


def test_new_positions_after_goal_are_closed_even_at_a_loss(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    for _ in range(12):
        bot.step()
    n_before = len(cl.orders)
    cl.legs[5] = ["Buy", 2.0, 110.0]          # новая позиция, уже в минусе
    for _ in range(8):
        bot.step()
    assert len(cl.orders) > n_before and cl.legs[5][1] <= 1e-9


def test_goal_already_earned_without_positions_blocks_new_ones(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                       # база 500, позиций нет
    cl.add_realized(12.0)            # прибыль уже зафиксирована (запись в логе транзакций)
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    cl.legs[1] = ["Buy", 1.0, 100.0]
    for _ in range(3):
        bot.step()
    assert cl.orders and cl.legs[1][1] <= 1e-9


def test_midnight_resets_base_and_goal(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    for _ in range(12):
        bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    clock.dt = datetime(2026, 10, 20, 0, 0, 20, tzinfo=MSK)
    bot.step()
    row = store.get_day("2026-10-20")
    assert row["start_kind"] == "midnight" and row["goal_reached"] == 0
    assert abs(row["start_equity"] - cl.get_equity("")) < 1e-9


def test_late_start_when_bot_was_off_at_midnight(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    clock.dt = datetime(2026, 10, 20, 10, 30, tzinfo=MSK)
    bot.step()
    assert store.get_day("2026-10-20")["start_kind"] == "late_start"


def test_restart_keeps_state(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    bot.step()
    store.close()
    store2 = StateStore(str(tmp_path / "state.db"))
    bot2, _, _ = make_bot(cl, tmp_path, clock=clock, store=store2)
    bot2.step()
    row = store2.get_day("2026-10-19")
    assert row["goal_reached"] == 1 and abs(row["start_equity"] - 500.0) < 1e-9


def test_midnight_is_moscow_time_regardless_of_machine_tz(tmp_path):
    from datetime import timezone
    utc = datetime(2026, 10, 19, 21, 0, 10, tzinfo=timezone.utc)   # = 00:00:10 МСК 20 октября
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path, clock=Clock(utc.astimezone(MSK)))
    bot.step()
    assert store.get_day("2026-10-20") is not None


def test_dry_run_sends_nothing_but_latches_goal(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path, dry_run=True)
    bot.step()
    cl.mark = 104.0
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1 and not cl.orders


def test_state_files_differ_by_mode():
    assert Config(dry_run=True).state_path != Config(dry_run=False).state_path
    assert Config(testnet=True, dry_run=False).state_path != Config(testnet=False, dry_run=False).state_path


# ---------- хедж ----------
def test_hedge_legs_closed_proportionally_with_correct_idx_and_sides(tmp_path):
    cl = FakeClient(legs={1: ["Buy", 3.0, 100.0], 2: ["Sell", 2.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.base = 500.0
    bot.step()                                      # база: 500
    cl.add_realized(12.0)                           # прибыль зафиксирована -> цель достигнута
    bot.step()
    assert len(cl.orders) == 2
    by_idx = {o[3]: o for o in cl.orders}
    assert by_idx[1][0] == "Sell" and by_idx[2][0] == "Buy"
    assert abs(float(by_idx[1][1]) / 3.0 - float(by_idx[2][1]) / 2.0) < 0.01


def test_hedge_both_legs_end_flat(tmp_path):
    cl = FakeClient(legs={1: ["Buy", 3.0, 100.0], 2: ["Sell", 2.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.add_realized(12.0)
    for _ in range(40):
        bot.step()
        if not cl.get_positions("linear", "USDT"):
            break
    assert all(l[1] < 1e-9 for l in cl.legs.values())


# ---------- вариант Б: переводы и обеспечение не влияют ----------
def _base_bot(tmp_path, **kw):
    cl = FakeClient(**kw)
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                      # база дня 500
    return cl, bot, store, clock


def test_deposit_does_not_trigger_goal(tmp_path):
    cl, bot, store, _ = _base_bot(tmp_path)
    cl.deposit(100.0)               # баланс +20%, но это не торговля
    ev = bot.step()
    assert abs(ev.pct) < 0.5 and not cl.orders and not store.get_day("2026-10-19")["goal_reached"]


def test_deposit_with_losing_positions_does_not_hide_loss(tmp_path):
    cl, bot, store, _ = _base_bot(tmp_path)
    cl.mark = 99.0                  # -9 на позициях
    cl.deposit(100.0)
    ev = bot.step()
    assert ev.pct < 0 and not cl.orders


def test_withdrawal_does_not_hide_real_profit(tmp_path):
    cl, bot, store, _ = _base_bot(tmp_path)
    cl.mark = 104.0                 # +12 по позициям
    cl.deposit(-50.0)               # вывели 50: баланс ниже базы, но торговый результат +
    ev = bot.step()
    assert ev.pct >= 2.0 and store.get_day("2026-10-19")["goal_reached"] == 1 and cl.orders


def test_collateral_price_move_does_not_trigger_goal(tmp_path):
    cl, bot, store, _ = _base_bot(tmp_path)
    cl.collateral_pnl = 60.0        # обеспечение в BTC выросло: баланс +12%
    ev = bot.step()
    assert abs(ev.pct) < 0.5 and not cl.orders


def test_unrealised_at_day_start_is_not_counted_as_todays_profit(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 3.0, 96.0]})   # на старте уже +12 нереализованного
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    row = store.get_day("2026-10-19")
    assert abs(row["start_unrealised"] - 12.0) < 1e-9
    ev = bot.step()
    assert abs(ev.unrealised_delta) < 1e-9 and ev.pct < 0.1 and not cl.orders
    cl.mark = 104.0                 # дальше +12 уже сегодняшние
    ev = bot.step()
    assert ev.pct >= 2.0


def test_trades_before_day_start_are_ignored(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(50.0, ts_ms=int(datetime(2026, 10, 19, 3, 0, tzinfo=MSK).timestamp() * 1000))
    bot.step()                      # первый запуск в 14:00: вчерашнее/утреннее не считается
    assert bot.step().realized == 0.0


def test_funding_and_fees_are_in_realized_and_foreign_types_excluded(tmp_path):
    cl, bot, store, _ = _base_bot(tmp_path)
    cl.add_realized(-0.3, "SETTLEMENT")     # фандинг
    cl.add_realized(-0.2, "TRADE")          # комиссия
    cl.add_realized(7.0, "BONUS")           # не торговля
    cl.add_realized(9.0, "CURRENCY_SELL")   # конвертация
    ev = bot.step()
    assert abs(ev.realized - (-0.5)) < 1e-9


def test_incremental_fetch_does_not_double_count(tmp_path):
    cl, bot, store, clock = _base_bot(tmp_path)
    cl.add_realized(2.0)
    for _ in range(5):
        clock.advance(seconds=10)
        ev = bot.step()
    assert abs(ev.realized - 2.0) < 1e-9


def test_full_refresh_picks_up_late_entry(tmp_path):
    cl, bot, store, clock = _base_bot(tmp_path)
    clock.advance(minutes=30)
    bot.step()
    late = int((clock() - timedelta(minutes=20)).timestamp() * 1000)   # запись «задним числом»
    cl.add_realized(3.0, ts_ms=late)
    clock.advance(minutes=11)       # прошёл интервал полного пересчёта
    assert abs(bot.step().realized - 3.0) < 1e-9


def test_goal_closing_continues_if_transaction_log_fails(tmp_path):
    cl, bot, store, clock = _base_bot(tmp_path)
    cl.mark = 104.0
    bot.step()                      # цель достигнута, пошло закрытие
    n = len(cl.orders)
    cl.get_transaction_log = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    clock.advance(seconds=10)
    bot.step()
    assert len(cl.orders) > n       # закрытие не зависит от лога


def test_no_decision_without_transaction_log_before_goal(tmp_path):
    cl, bot, store, clock = _base_bot(tmp_path)
    cl.mark = 104.0
    cl.get_transaction_log = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    clock.advance(seconds=10)
    try:
        bot.step()
        assert False, "должно выбросить"
    except RuntimeError:
        pass
    assert not cl.orders and not store.get_day("2026-10-19")["goal_reached"]


# ---------- логи ----------
def test_log_cleanup_removes_only_older_than_week(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    today = date(2026, 10, 19)
    names = {"bot-2026-10-19.log": True, "bot-2026-10-13.log": True, "bot-2026-10-12.log": True,
             "bot-2026-10-11.log": False, "bot-2026-09-01.log": False}   # True = остаётся (<= 7 дней)
    for n in names:
        (d / n).write_text("x")
    h = DailyFileHandler(str(d), MSK, retention_days=7)
    h.cleanup(today)
    for n, keep in names.items():
        assert (d / n).exists() == keep, n


def test_log_file_per_day_in_moscow_time(tmp_path):
    import logging
    h = DailyFileHandler(str(tmp_path / "logs"), MSK, retention_days=7)
    rec = logging.LogRecord("t", logging.INFO, "", 0, "hello", None, None)
    rec.created = datetime(2026, 10, 19, 21, 30, tzinfo=__import__("datetime").timezone.utc).timestamp()  # уже 20-е по МСК
    h.handle(rec)
    h.close()
    assert os.path.exists(tmp_path / "logs" / "bot-2026-10-20.log")
