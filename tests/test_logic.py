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
        cat = "linear" if typ in ("TRADE", "SETTLEMENT", "DELIVERY", "LIQUIDATION", "ADL") else ""
        self.tx.append({"id": f"t{self._id}", "type": typ, "change": str(change), "category": cat,
                        "transactionTime": str(t), "currency": "USDT"})

    def add_realized(self, amount, typ="TRADE", ts_ms=None):
        self.realized += amount
        self._log(typ, amount, ts_ms)

    def deposit(self, amount, ts_ms=None):
        self.transfers += amount
        self._log("TRANSFER_IN" if amount > 0 else "TRANSFER_OUT", amount, ts_ms)

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


def _ms(h, m=0, day=19):
    return int(datetime(2026, 10, day, h, m, tzinfo=MSK).timestamp() * 1000)


def test_first_run_midday_counts_todays_earlier_trades_and_latches_goal(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})          # позиций сейчас нет
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(15.0, ts_ms=_ms(3))                      # утром уже заработали +15 при базе ~500
    ev = bot.step()                                          # первый запуск в 14:00
    row = store.get_day("2026-10-19")
    assert abs(row["start_equity"] - 500.0) < 1e-9           # база восстановлена: 515 − 15
    assert row["start_ts"].startswith("2026-10-19T00:00:00")
    assert row["goal_reached"] == 1 and abs(row["goal_gain"] - 15.0) < 1e-9 and ev.goal


def test_first_run_midday_base_ignores_earlier_transfers(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.deposit(100.0, ts_ms=_ms(5))                          # утренний перевод — не прибыль
    cl.add_realized(4.0, ts_ms=_ms(6))                       # +4 при базе 500 = 0.8%
    bot.step()
    row = store.get_day("2026-10-19")
    # капитал без результата дня = 604 − 4 = 600 (перевод в капитале, а не в прибыли) > баланса на 00:00 (500)
    assert abs(row["start_equity"] - 600.0) < 1e-9 and row["goal_reached"] == 0


def test_account_funded_during_the_day_goal_found_from_todays_trades(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]}, base=0.0)    # в 00:00 счёт был пуст
    bot, store, clock = make_bot(cl, tmp_path)
    cl.deposit(111.79, ts_ms=_ms(10))
    cl.add_realized(4.2855, ts_ms=_ms(11))
    bot.step()
    row = store.get_day("2026-10-19")
    assert abs(row["start_equity"] - 111.79) < 1e-9
    assert row["goal_reached"] == 1 and abs(row["goal_gain"] - 4.2855) < 1e-9


def test_first_run_midday_ignores_trades_from_yesterday(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(50.0, ts_ms=_ms(22, day=18))             # вчера
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 0


def test_first_run_midday_open_positions_closed_after_goal_found(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 2.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(15.0, ts_ms=_ms(3))
    for _ in range(6):
        bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    assert cl.orders and cl.legs[0][1] <= 1e-9


def test_after_goal_no_transaction_log_or_equity_requests(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(15.0, ts_ms=_ms(3))
    bot.step()                                               # цель найдена и записана
    calls = cl.tx_calls
    eq_calls = {"n": 0}
    orig = cl.get_equity
    cl.get_equity = lambda t: (eq_calls.__setitem__("n", eq_calls["n"] + 1), orig(t))[1]
    for _ in range(5):
        clock.advance(seconds=10)
        bot.step()
    assert cl.tx_calls == calls and eq_calls["n"] == 0


def test_after_goal_restart_does_not_query_log_either(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    cl.add_realized(15.0, ts_ms=_ms(3))
    bot.step()
    store.close()
    store2 = StateStore(str(tmp_path / "state.db"))
    bot2, _, _ = make_bot(cl, tmp_path, clock=clock, store=store2)
    calls = cl.tx_calls
    bot2.step()
    assert cl.tx_calls == calls


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


def test_diag_runs_and_reports_unusable_base(tmp_path):
    from bot.diag import run_diag
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]}, base=0.0)
    cl.clock = Clock(datetime(2026, 10, 19, 14, 0, tzinfo=MSK))
    cl._synced = True
    cl.time_offset_ms = 0
    cl.sync_time = lambda: None
    cl.get_closed_pnl = lambda c, a, b: []
    cl.deposit(112.0, ts_ms=_ms(10))          # счёт пополнен сегодня: на 00:00 баланс был 0
    lines = []
    run_diag(cl, cfg(), MSK, out=lines.append)
    text = "\n".join(lines)
    assert "TRANSFER_IN" in text and "на 00:00 счёт был пуст" in text


# ---------- копирование на другие счета ----------
from bot.followers import Action, Follower, FollowerSpec, chunk_for, load_specs, plan_action  # noqa: E402

STEP, MINQ = Decimal("0.001"), Decimal("0.001")


def act(desired, current, db=3.0):
    return plan_action(desired, current, STEP, MINQ, db)


def test_copy_opens_from_zero_long_and_short():
    a = act(10.0, 0.0)
    assert (a.side, a.need, a.reduce_only) == ("Buy", Decimal("10.000"), False)
    a = act(-4.0, 0.0)
    assert (a.side, a.need, a.reduce_only) == ("Sell", Decimal("4.000"), False)


def test_copy_adds_only_the_missing_part_and_respects_deadband():
    a = act(10.0, 6.0)
    assert (a.side, a.need, a.reduce_only) == ("Buy", Decimal("4.000"), False)
    assert act(10.0, 9.9) is None          # 1% < допуска 3%
    assert act(10.0, 10.0) is None


def test_copy_reduces_and_closes_with_reduce_only():
    a = act(4.0, 10.0)
    assert (a.side, a.need, a.reduce_only) == ("Sell", Decimal("6.000"), True)
    a = act(-4.0, -10.0)
    assert (a.side, a.need, a.reduce_only) == ("Buy", Decimal("6.000"), True)
    a = act(0.0, 7.5)                        # основной закрыт — копия закрывается целиком, допуск не мешает
    assert (a.side, a.need, a.reduce_only) == ("Sell", Decimal("7.500"), True)
    assert act(0.0, 0.0) is None


def test_copy_flip_first_exits_whole_position():
    a = act(-5.0, 3.0)
    assert (a.side, a.need, a.reduce_only) == ("Sell", Decimal("3.000"), True)


def test_copy_tiny_target_below_min_qty_is_flat():
    assert act(0.0004, 0.0) is None
    a = act(0.0004, 1.0)
    assert a.reduce_only and a.need == Decimal("1.0")


def test_copy_chunk_is_fraction_of_depth_and_opens_on_own_side():
    c = cfg(book_fraction=0.1)
    plan = chunk_for(Action("Buy", Decimal("50"), False), BOOK, INSTR, c)
    assert plan.side == "Buy" and plan.price == 100.0    # лонг открываем по best bid — мейкер
    assert plan.qty == Decimal("1.000")                  # 10% от глубины asks (10)
    plan = chunk_for(Action("Sell", Decimal("50"), False), BOOK, INSTR, c)
    assert plan.price == 100.1


def test_copy_chunk_skips_below_min_notional_and_wide_spread():
    assert chunk_for(Action("Buy", Decimal("0.01"), False), BOOK, {**INSTR, "min_notional": 5.0}, cfg()) is None
    wide = {"bids": [(100.0, 5)], "asks": [(101.0, 5)]}
    assert chunk_for(Action("Buy", Decimal("1"), False), wide, INSTR, cfg(max_spread_bps=20)) is None


def test_load_specs(tmp_path):
    f = tmp_path / "f.json"
    assert load_specs(str(f)) == []
    f.write_text('{"followers":[{"name":"a1","api_key":"k","api_secret":"s"},'
                 '{"name":"off","api_key":"","api_secret":"","enabled":false}]}')
    specs = load_specs(str(f))
    assert [s.name for s in specs] == ["a1"] and specs[0].multiplier == 1.0
    f.write_text('[{"name":"bad name","api_key":"k","api_secret":"s"}]')
    try:
        load_specs(str(f))
        assert False
    except ValueError:
        pass


class CopyFake:
    """Минимальная биржа для копии: позиции, equity, стакан, ордера."""

    def __init__(self, equity, size=0.0, side="Buy"):
        self.equity, self.size, self.side = equity, size, side
        self.placed, self.leverage_calls = [], []
        self.orders = {}

    def get_equity(self, t):
        return self.equity

    def get_positions(self, category, settle):
        if self.size <= 0:
            return []
        return [{"symbol": "BTCUSDT", "side": self.side, "size": str(self.size), "avgPrice": "100",
                 "markPrice": "100", "positionIdx": 0, "positionIM": "1", "leverage": "5"}]

    def get_orderbook(self, c, s, limit=50):
        return BOOK

    def get_instrument(self, c, s):
        return {**INSTR, "min_notional": 5.0}

    def get_margin_mode(self):
        return "REGULAR_MARGIN"

    def set_leverage(self, c, s, lev):
        self.leverage_calls.append((s, lev))

    def place_limit_order(self, c, s, side, qty, price, idx, link, reduce_only):
        self.placed.append((side, qty, price, reduce_only))
        self.orders[f"o{len(self.placed)}"] = {"orderStatus": "New"}
        return f"o{len(self.placed)}"

    def get_order(self, c, s, oid):
        return self.orders.get(oid)

    def cancel_order(self, c, s, oid):
        self.orders[oid] = {"orderStatus": "Cancelled"}


def make_follower(tmp_path, client, **kw):
    spec = FollowerSpec("acc2", "k", "s")
    f = Follower(spec, cfg(data_dir=str(tmp_path), book_fraction=0.1, **kw), client=client)
    f._last_eval = 1e18       # шаг дневной цели в этих тестах не нужен
    f.store.create_day(f.bot.clock().date().isoformat(), 1000.0, 0.0, "t", "first_run", 2.0)
    return f


def test_follower_opens_proportionally_with_leverage_and_waits_for_pending(tmp_path):
    cl = CopyFake(equity=1000.0)
    f = make_follower(tmp_path, cl)
    main = [PositionView("BTCUSDT", "Buy", 10.0, 100.0, 100.0, 0.0, 0, 1.0, leverage=5.0)]
    f._sync(main, 100.0)                       # equity 1000 / 100 = ×10 -> нужно 100
    assert f.ratio == 10.0
    assert cl.leverage_calls == [("BTCUSDT", 5.0)]
    assert cl.placed == [("Buy", "1", "100", False)]    # кусок 10% глубины, лимит по best bid
    f._sync(main, 100.0)                       # ордер ещё висит — второй не ставим
    assert len(cl.placed) == 1


def test_follower_mirrors_main_close_with_reduce_only(tmp_path):
    cl = CopyFake(equity=1000.0, size=100.0)
    f = make_follower(tmp_path, cl)
    f._sync([], 100.0)                         # основной закрыт
    assert cl.placed and cl.placed[0][0] == "Sell" and cl.placed[0][3] is True


def test_follower_dry_run_places_nothing(tmp_path):
    cl = CopyFake(equity=1000.0)
    f = make_follower(tmp_path, cl, dry_run=True)
    f._sync([PositionView("BTCUSDT", "Buy", 10.0, 100.0, 100.0, 0.0, 0, 1.0, leverage=5.0)], 100.0)
    assert cl.placed == [] and cl.leverage_calls == []


# ---------- хранение ключей и веб-интерфейс ----------
import base64 as _b64  # noqa: E402
import json as _json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from bot import accounts, webui  # noqa: E402


def test_accounts_keep_old_secret_when_blank_and_hide_secrets(tmp_path):
    path = str(tmp_path / "followers.json")
    accounts.save(path, {"main": {"api_key": "MK", "api_secret": "MS"},
                         "followers": [{"name": "a1", "api_key": "K1", "api_secret": "S1", "multiplier": 2}]})
    accounts.save(path, {"main": {"api_key": "MK", "api_secret": ""},
                         "followers": [{"name": "a1", "api_key": "K1b", "api_secret": ""},
                                       {"name": "a2", "api_key": "K2", "api_secret": "S2", "dry_run": True}]})
    raw = accounts.read_raw(path)
    assert raw["main"]["api_secret"] == "MS" and raw["followers"][0]["api_secret"] == "S1"
    assert raw["followers"][0]["api_key"] == "K1b" and raw["followers"][1]["dry_run"] is True
    pub = _json.dumps(accounts.public_view(raw))
    assert "MS" not in pub and "S1" not in pub and "S2" not in pub
    assert accounts.load_main(path) == {"api_key": "MK", "api_secret": "MS", "testnet": False}
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert [s.name for s in load_specs(path)] == ["a1", "a2"]


def test_accounts_validation(tmp_path):
    path = str(tmp_path / "f.json")
    for bad in ({"followers": [{"name": "x y", "api_key": "k", "api_secret": "s"}]},
                {"followers": [{"name": "a", "api_key": "k", "api_secret": "s"},
                               {"name": "a", "api_key": "k", "api_secret": "s"}]},
                {"followers": [{"name": "a", "api_key": "k", "api_secret": ""}]},
                {"main": {"api_key": "only-key"}, "followers": []}):
        try:
            accounts.save(path, bad)
            assert False, bad
        except ValueError:
            pass
    assert not os.path.exists(path)   # при ошибке файл не создаётся


def test_webui_auth_csrf_and_save(tmp_path):
    path = str(tmp_path / "followers.json")
    restarted = []
    srv = webui.start(path, "pw", "127.0.0.1", 0, restart=lambda: restarted.append(1))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    auth = "Basic " + _b64.b64encode(b"u:pw").decode()

    def call(url, body=None, headers=None):
        req = urllib.request.Request(base + url, data=_json.dumps(body).encode() if body is not None else None,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        assert call("/api/config")[0] == 401
        assert call("/api/config", headers={"Authorization": "Basic " + _b64.b64encode(b"u:bad").decode()})[0] == 401
        st, html = call("/", headers={"Authorization": auth})
        assert st == 200 and "Сохранить".encode() in html
        body = {"main": {"api_key": "MK", "api_secret": "MS"}, "followers": []}
        assert call("/api/config", body, {"Authorization": auth})[0] == 403          # без X-Requested-With
        assert restarted == [] and not os.path.exists(path)
        st, out = call("/api/config", body, {"Authorization": auth, "X-Requested-With": "m2p"})
        assert st == 200 and restarted == [1] and accounts.load_main(path)["api_secret"] == "MS"
        st, out = call("/api/config", headers={"Authorization": auth})
        assert st == 200 and b"MS" not in out and _json.loads(out)["main"]["has_secret"] is True
        st, out = call("/api/config", {"followers": [{"name": "bad name"}]},
                       {"Authorization": auth, "X-Requested-With": "m2p"})
        assert st == 400 and restarted == [1]
    finally:
        srv.shutdown()


class MarginFake(FakeClient):
    def get_margin_mode(self): return "REGULAR_MARGIN"


def test_copier_tick_runs_followers_in_worker_threads_without_errors(tmp_path, caplog):
    from bot.followers import Copier
    main, fcl = MarginFake(), MarginFake(legs={0: ["Buy", 0.0, 100.0]}, base=1000.0)
    f = Follower(FollowerSpec("acc2", "k", "s"), cfg(data_dir=str(tmp_path)), client=fcl)
    cp = Copier(main, cfg(data_dir=str(tmp_path)), [f])
    with caplog.at_level("ERROR"):
        cp.tick()
        f.future.result(timeout=10)       # реальный путь: шаг копии выполняется в потоке пула
    cp.close()
    assert not [r for r in caplog.records if "Ошибка" in r.getMessage()], caplog.text
    assert f.store.conn is not None
