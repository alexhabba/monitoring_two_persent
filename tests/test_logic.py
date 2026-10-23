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
    def get_open_orders(self, category, settle): return []      # ордера этого фейка исполняются мгновенно
    def amend_order(self, *a): pass
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


def test_new_position_after_goal_is_closed_while_result_stays_above_target(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    for _ in range(12):
        bot.step()
    n_before = len(cl.orders)
    cl.legs[5] = ["Buy", 2.0, 103.0]          # новая позиция около рынка: результат дня остаётся выше цели
    for _ in range(8):
        bot.step()
    assert len(cl.orders) > n_before and cl.legs[5][1] <= 1e-9


def test_new_losing_position_after_goal_is_not_closed_and_goal_is_cleared(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    for _ in range(12):
        bot.step()
    n_before = len(cl.orders)
    cl.legs[5] = ["Buy", 20.0, 110.0]         # большой минус: результат дня падает ниже цели
    bot.step()
    assert len(cl.orders) == n_before and cl.legs[5][1] == 20.0
    assert store.get_day("2026-10-19")["goal_reached"] == 0 and not bot.closing


def test_closing_stops_when_result_falls_below_target_and_resumes_when_back(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                       # база 500
    cl.mark = 104.0                  # +12 = 2.4% -> цель, первый кусок закрытия
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1 and len(cl.orders) == 1
    cl.mark = 100.5                  # результат упал ниже 2%: перед следующим куском проверка не проходит
    for _ in range(3):
        bot.step()
    assert len(cl.orders) == 1                          # ни одного нового ордера
    row = store.get_day("2026-10-19")
    assert row["goal_reached"] == 0 and row["goal_ts"] is None and not bot.closing
    cl.mark = 104.0                  # снова выше цели — цель ставится заново, закрытие продолжается
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1 and len(cl.orders) == 2


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


def test_after_goal_without_positions_result_is_rechecked_and_goal_cleared_when_it_drops(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                                               # база 500
    cl.add_realized(15.0, ts_ms=_ms(3))
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    calls = cl.tx_calls
    clock.advance(seconds=10)
    bot.step()
    assert cl.tx_calls > calls                               # результат пересчитывается, цель остаётся
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    cl.add_realized(-10.0, ts_ms=_ms(3))                     # потом результат упал (комиссии/убыток)
    clock.advance(seconds=10)
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 0


def test_after_goal_restart_keeps_goal_if_result_still_above_target(tmp_path):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.add_realized(15.0, ts_ms=_ms(3))
    bot.step()
    store.close()
    store2 = StateStore(str(tmp_path / "state.db"))
    bot2, _, _ = make_bot(cl, tmp_path, clock=clock, store=store2)
    bot2.step()
    assert store2.get_day("2026-10-19")["goal_reached"] == 1


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


def test_goal_closing_pauses_if_transaction_log_fails(tmp_path):
    cl, bot, store, clock = _base_bot(tmp_path)
    cl.mark = 104.0
    bot.step()                      # цель достигнута, пошло закрытие
    n = len(cl.orders)
    cl.get_transaction_log = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down"))
    clock.advance(seconds=10)
    try:
        bot.step()
        assert False, "должно выбросить"
    except RuntimeError:
        pass
    assert len(cl.orders) == n      # без данных о результате новый кусок не ставим


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

    def get_wallet_balance(self, t):
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
    def get_wallet_balance(self, t): return self.get_equity(t)


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


def test_ratio_ignores_floating_pnl_and_small_drift(tmp_path):
    """Прежняя причина «качелей»: equity (с нереализованным PnL) двигал пропорцию, и копия то докупала, то сбрасывала."""
    cl = CopyFake(equity=1000.0)
    f = make_follower(tmp_path, cl)
    f._refresh_ratio(100.0)
    assert f.ratio == 10.0
    cl.equity = 1015.0                 # дрейф +1.5%: внутри гистерезиса 2% — пропорция не меняется
    f._ratio_at = -1e9
    f._refresh_ratio(100.0)
    assert f.ratio == 10.0
    cl.equity = 1100.0                 # реальное изменение баланса (пополнение) — пропорция обновляется
    f._ratio_at = -1e9
    f._refresh_ratio(100.0)
    assert f.ratio == 11.0


def test_rate_limit_pauses_follower(tmp_path):
    from bot.bybit_client import BybitError
    cl = CopyFake(equity=1000.0)
    f = make_follower(tmp_path, cl)

    def boom(*a, **k):
        raise BybitError(10006, "Too many visits", "/x")
    f._sync = boom
    f.tick([], 100.0, "REGULAR_MARGIN")
    assert f._pause_until > 0
    f._sync = lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен вызываться во время паузы"))
    f.tick([], 100.0, "REGULAR_MARGIN")      # во время паузы шаг пропускается


# ---------- быстрое закрытие: все позиции сразу, по приоритету, с переставлением ордера ----------
import time as _time  # noqa: E402

from bot.bybit_client import BybitError  # noqa: E402


class MultiFake(FakeClient):
    """Несколько монет: каждая «нога» legs[i] — отдельный символ C{i}USDT."""

    def get_positions(self, category, settle):
        out = super().get_positions(category, settle)
        for r in out:
            r["symbol"] = f"C{r['positionIdx']}USDT"
        return out


class RestingFake(MultiFake):
    """Ордера на закрытие не исполняются мгновенно, а стоят в стакане (можно переставлять и снимать)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open, self.amended, self.cancelled = {}, [], []

    def place_limit_close(self, category, symbol, side, qty, price, idx, link):
        oid = f"o{len(self.orders) + 1}"
        self.orders.append((side, qty, price, idx))
        self.open[oid] = {"orderId": oid, "symbol": symbol, "positionIdx": idx, "price": price, "qty": qty,
                          "leavesQty": qty, "orderLinkId": link, "reduceOnly": True}
        return oid

    def get_open_orders(self, category, settle): return list(self.open.values())

    def amend_order(self, c, s, oid, price, qty):
        self.amended.append((oid, price, qty))
        self.open[oid].update(price=price, qty=qty)

    def cancel_order(self, c, s, oid):
        self.cancelled.append(oid)
        self.open.pop(oid, None)


def test_all_positions_get_an_order_in_the_same_step(tmp_path):
    cl = MultiFake(legs={0: ["Buy", 3.0, 100.0], 1: ["Buy", 3.0, 100.0], 2: ["Buy", 3.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()                         # база
    cl.add_realized(20.0)              # цель достигнута
    bot.step()
    assert sorted(o[3] for o in cl.orders) == [0, 1, 2]     # по ордеру на КАЖДУЮ позицию за один шаг


def test_closing_priority_goes_to_positions_with_the_biggest_recent_profit_growth(tmp_path):
    cl = MultiFake(legs={0: ["Buy", 3.0, 100.0], 1: ["Buy", 3.0, 100.0], 2: ["Buy", 3.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path, close_workers=1)     # 1 поток — порядок отправки = приоритет
    bot.step()
    cl.mark = 104.0                    # у каждой позиции нереализованный +12
    now = _time.monotonic()
    # 70 секунд назад: у C1 было +2 (рост +10), у C2 +8 (рост +4), у C0 +11.5 (рост +0.5)
    bot._hist.clear()
    bot._hist.append((now - 70, {("C0USDT", 0): 11.5, ("C1USDT", 1): 2.0, ("C2USDT", 2): 8.0}))
    bot.step()
    assert [o[3] for o in cl.orders] == [1, 2, 0]


def test_resting_order_is_kept_then_moved_closer_with_amend_not_cancel(tmp_path):
    cl = RestingFake(legs={0: ["Buy", 50.0, 100.0]})
    c = cfg(close_workers=1)
    closer = PositionCloser(cl, c, sleep=lambda s: None)
    p = pos(size=50.0)
    p = PositionView("C0USDT", "Buy", 50.0, 100.0, 100.0, 0.0, 0, 10.0)
    closer.close_all([p], {"C0USDT": make_book(100.0)})
    assert len(cl.orders) == 1 and cl.orders[0][2] == "100.1"               # встали на лучшую цену продажи
    closer.close_all([p], {"C0USDT": make_book(100.0)})
    assert len(cl.orders) == 1 and not cl.amended                           # цена та же — ничего не трогаем
    closer.close_all([p], {"C0USDT": make_book(99.8)})                      # рынок ушёл вниз: лучший ask теперь 99.9
    assert len(cl.orders) == 1 and cl.amended and cl.amended[0][1] == "99.9" and not cl.cancelled


def test_unlatching_goal_cancels_resting_close_orders(tmp_path):
    cl = RestingFake(legs={0: ["Buy", 3.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    cl.mark = 104.0
    bot.step()                         # цель, ордер на закрытие стоит в стакане
    assert cl.open and bot.closer.resting
    cl.mark = 100.5                    # результат упал ниже цели
    bot.step()
    assert not cl.open and cl.cancelled and not bot.closer.resting
    assert store.get_day("2026-10-19")["goal_reached"] == 0


def test_not_whitelisted_symbol_is_reported_clearly_and_does_not_crash(tmp_path, caplog):
    cl = RestingFake(legs={0: ["Buy", 3.0, 100.0]})

    def reject(*a, **k):
        raise BybitError(10029, "The requested symbol is not whitelisted.", "/v5/order/create")
    cl.place_limit_close = reject
    closer = PositionCloser(cl, cfg(close_workers=1), sleep=lambda s: None)
    p = PositionView("C0USDT", "Buy", 3.0, 100.0, 100.0, 0.0, 0, 10.0)
    with caplog.at_level("ERROR"):
        closer.close_all([p], {"C0USDT": make_book(100.0)})
        closer.close_all([p], {"C0USDT": make_book(100.0)})    # вторая попытка: тот же текст в лог не повторяется
    msgs = [r.getMessage() for r in caplog.records if "не удалось закрывать" in r.getMessage()]
    assert len(msgs) == 1 and "whitelist" in msgs[0] and not closer.resting


def test_base_from_now_ignores_profit_earned_before_start(tmp_path):
    def run(flag):
        cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
        bot, store, clock = make_bot(cl, tmp_path / ("a" if flag else "b"), base_from_now=flag, target_profit_pct=0.13)
        (tmp_path / ("a" if flag else "b")).mkdir(exist_ok=True)
        cl.add_realized(20.0, ts_ms=_ms(3))          # прибыль, заработанная сегодня ДО старта бота
        ev = bot.step()
        return cl, bot, store, ev
    cl, bot, store, ev = run(True)
    row = store.get_day("2026-10-19")
    assert abs(row["start_equity"] - cl.get_equity("")) < 1e-9 and row["goal_reached"] == 0 and abs(ev.pct) < 1e-9
    cl.add_realized(1.0)                             # +1 USDT от базы ~520 = 0.19% >= 0.13%
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    cl2, bot2, store2, ev2 = run(False)              # без флага ранняя прибыль восстанавливается и цель сразу достигнута
    assert store2.get_day("2026-10-19")["goal_reached"] == 1


# ---------- копия отсоединяется после цели основного ----------
def test_follower_does_not_mirror_main_reductions_after_main_goal(tmp_path):
    cl = CopyFake(equity=1000.0, size=100.0)                 # на копии 100, основной уже сократился до 4 (нужно 40)
    f = make_follower(tmp_path, cl)
    main = [PositionView("BTCUSDT", "Buy", 4.0, 100.0, 100.0, 0.0, 0, 1.0, leverage=5.0)]
    f._tick(main, 100.0, "REGULAR_MARGIN", main_goal_today=True)
    assert cl.placed == []                                    # сокращения основного не повторяем
    f._tick(main, 100.0, "REGULAR_MARGIN", main_goal_today=False)    # без признака цели обычное зеркалирование
    assert cl.placed and cl.placed[0][0] == "Sell" and cl.placed[0][3] is True


def test_goal_event_survives_unlatch_and_is_cleared_by_day_reset(tmp_path):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path)
    bot.step()
    assert not store.has_goal_event("2026-10-19")
    cl.mark = 104.0
    bot.step()                                                # цель достигнута
    assert store.has_goal_event("2026-10-19")
    cl.mark = 100.5
    bot.step()                                                # цель снята, но «сегодня цель была» остаётся
    assert store.get_day("2026-10-19")["goal_reached"] == 0 and store.has_goal_event("2026-10-19")
    store.delete_day("2026-10-19")                            # сброс дня — день «с нуля»
    assert not store.has_goal_event("2026-10-19")


def test_account_label_reaches_closing_worker_threads(tmp_path):
    from bot import logctx
    seen = []
    cl = RestingFake(legs={0: ["Buy", 3.0, 100.0], 1: ["Buy", 3.0, 100.0]})
    orig = cl.place_limit_close

    def spy(*a, **k):
        seen.append(getattr(logctx.ctx, "name", None))
        return orig(*a, **k)
    cl.place_limit_close = spy
    closer = PositionCloser(cl, cfg(close_workers=2), sleep=lambda s: None)
    closer.account = "acc2"
    ps = [PositionView("C0USDT", "Buy", 3.0, 100.0, 100.0, 0.0, 0, 10.0),
          PositionView("C1USDT", "Buy", 3.0, 100.0, 100.0, 0.0, 1, 10.0)]
    closer.close_all(ps, {"C0USDT": make_book(100.0), "C1USDT": make_book(100.0)})
    assert seen == ["acc2", "acc2"]


# ---------- WebSocket: обработка сообщений ----------
from decimal import Decimal as _D  # noqa: E402

from bot.ladder import LINK_PREFIX, TakeProfitLadder, plan_ladder  # noqa: E402
from bot.stream import PrivateStream, PublicStream  # noqa: E402


def test_public_stream_book_snapshot_delta_and_ticker_merge():
    st = PublicStream()
    st.handle({"topic": "orderbook.50.XUSDT", "type": "snapshot", "data": {
        "s": "XUSDT", "b": [["99", "5"], ["98", "7"]], "a": [["101", "4"], ["102", "6"]], "u": 10}})
    st.handle({"topic": "orderbook.50.XUSDT", "type": "delta", "data": {
        "s": "XUSDT", "b": [["99", "0"], ["99.5", "2"]], "a": [["101", "9"]], "u": 11}})
    b = st.book("XUSDT")
    assert b["bids"] == [(99.5, 2.0), (98.0, 7.0)] and b["asks"] == [(101.0, 9.0), (102.0, 6.0)]
    st.handle({"topic": "tickers.XUSDT", "type": "snapshot", "data": {"symbol": "XUSDT", "markPrice": "100.2", "lastPrice": "100.1"}})
    st.handle({"topic": "tickers.XUSDT", "type": "delta", "data": {"symbol": "XUSDT", "lastPrice": "100.3"}})   # дельта — только изменённые поля
    t = st.ticker("XUSDT")
    assert t["markPrice"] == 100.2 and t["lastPrice"] == 100.3
    st._book_ts["XUSDT"] = 0                                    # устарел — потоку не доверяем
    assert st.book("XUSDT") is None


def test_public_stream_crossed_book_is_rejected():
    st = PublicStream()
    st.handle({"topic": "orderbook.50.XUSDT", "type": "snapshot", "data": {
        "s": "XUSDT", "b": [["102", "1"]], "a": [["101", "1"]], "u": 5}})
    assert st.book("XUSDT") is None


class _RestStub:
    def get_positions(self, c, s): return [{"symbol": "XUSDT", "side": "Buy", "size": "10", "avgPrice": "100", "positionIdx": 1}]
    def get_open_orders(self, c, s): return []


def test_private_stream_positions_and_orders_apply_updates():
    execs = []
    st = PrivateStream("k", "s", _RestStub(), on_execution=execs.append)
    st.resync()
    assert st.positions()[0]["size"] == "10"
    st.handle({"topic": "position.linear", "data": [{"symbol": "XUSDT", "positionIdx": 1, "side": "Buy", "size": "4",
                                                      "entryPrice": "100.5", "markPrice": "101"}]})
    p = st.positions()[0]
    assert p["size"] == "4" and p["avgPrice"] == "100.5"        # entryPrice из потока → avgPrice как в REST
    st.handle({"topic": "position.linear", "data": [{"symbol": "XUSDT", "positionIdx": 1, "side": "", "size": "0"}]})
    assert st.positions() == []
    st.handle({"topic": "order.linear", "data": [{"orderId": "o1", "symbol": "XUSDT", "orderStatus": "New", "price": "1", "qty": "1"}]})
    assert [o["orderId"] for o in st.open_orders()] == ["o1"]
    st.handle({"topic": "order.linear", "data": [{"orderId": "o1", "orderStatus": "Filled"}]})
    assert st.open_orders() == []
    st.handle({"topic": "execution.linear", "data": [{"orderLinkId": LINK_PREFIX + "1"}]})
    assert execs and execs[0]["orderLinkId"].startswith(LINK_PREFIX)


# ---------- лесенка тейк-профитов ----------
LADDER_INSTR = {"min_qty": 1.0, "qty_step": "1", "tick_size": "0.1", "min_notional": 5.0}


def ladder_book(mark=100.0):
    return {"bids": [(mark - 0.1 * i, 500.0) for i in range(1, 30)],
            "asks": [(mark + 0.1 * i, 100.0 + 50 * (i % 3)) for i in range(1, 80)]}


def test_ladder_prices_follow_needed_gain_for_long_and_short():
    c = cfg(tp_max_orders=2, tp_book_fraction=0.2, tp_density_window_bps=1)
    # лонг 100 шт, mark 100, не хватает 10 USDT -> движение цены 0.1 (+комиссия): уровни ~100.1 и ~100.13
    longs = plan_ladder("Buy", 100.0, 100.0, 10.0, ladder_book(100.0), LADDER_INSTR, c, maker_fee=0.0)
    assert longs and all(s.price > 100 for s in longs)
    assert sum(s.qty for s in longs) == _D(100)                  # весь объём позиции разложен по уровням
    shorts = plan_ladder("Sell", 100.0, 100.0, 10.0, ladder_book(100.0), LADDER_INSTR, c, maker_fee=0.0)
    assert shorts and all(s.price < 100 for s in shorts)         # шорт закрываем покупкой НИЖЕ рынка
    assert sum(s.qty for s in shorts) == _D(100)
    far = plan_ladder("Buy", 100.0, 100.0, 40.0, ladder_book(100.0), LADDER_INSTR, c, maker_fee=0.0)
    assert min(s.price for s in far) > min(s.price for s in longs)          # чем больше нужно, тем дальше уровень


def test_ladder_density_limits_slice_to_book_fraction_and_spreads_over_levels():
    c = cfg(tp_max_orders=6, tp_book_fraction=0.2, tp_density_window_bps=30)
    book = ladder_book(100.0)
    out = plan_ladder("Buy", 300.0, 100.0, 15.0, book, LADDER_INSTR, c, maker_fee=0.0)
    assert len(out) > 1                                          # объём размазан по нескольким уровням стакана
    assert sum(s.qty for s in out) == _D(300)
    assert len({s.price for s in out}) == len(out)


def test_ladder_empty_when_goal_already_reached_or_no_size():
    c = cfg()
    assert plan_ladder("Buy", 10.0, 100.0, 0.0, ladder_book(), LADDER_INSTR, c) == []
    assert plan_ladder("Buy", 10.0, 100.0, -5.0, ladder_book(), LADDER_INSTR, c) == []
    assert plan_ladder("Buy", 0.0, 100.0, 5.0, ladder_book(), LADDER_INSTR, c) == []


def test_ladder_price_is_passive_never_inside_the_market():
    c = cfg()
    out = plan_ladder("Buy", 100.0, 100.0, 0.001, ladder_book(100.0), LADDER_INSTR, c, maker_fee=0.0)   # цель почти достигнута
    assert out and all(s.price > _D("99.9") for s in out)        # выше лучшего бида — не пересекает рынок


class LadderFake:
    def __init__(self):
        self.orders, self.cancelled, self.amended, self.n = {}, [], [], 0

    def place_limit_order(self, c, symbol, side, qty, price, idx, link, reduce_only):
        self.n += 1
        oid = f"t{self.n}"
        assert reduce_only
        self.orders[oid] = {"orderId": oid, "symbol": symbol, "side": side, "qty": qty, "leavesQty": qty, "price": price,
                            "positionIdx": idx, "orderLinkId": link, "reduceOnly": True}
        return oid

    def amend_order(self, c, s, oid, price, qty):
        self.amended.append(oid)
        self.orders[oid].update(price=price, qty=qty, leavesQty=qty)

    def cancel_order(self, c, s, oid):
        self.cancelled.append(oid)
        self.orders.pop(oid, None)


def make_ladder(fake, **kw):
    c = cfg(tp_max_orders=4, tp_book_fraction=0.3, tp_density_window_bps=20, **kw)
    return TakeProfitLadder(fake, c, lambda s: LADDER_INSTR, lambda: list(fake.orders.values()), lambda s: 0.0)


def lpos(size=100.0, mark=100.0, side="Buy", idx=0):
    return PositionView("XUSDT", side, size, 100.0, mark, 0.0, idx, 10.0)


def test_ladder_places_then_keeps_then_amends_and_cancels():
    fake = LadderFake()
    lad = make_ladder(fake)
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 10.0)
    first = dict(fake.orders)
    assert first and all(o["side"] == "Sell" and o["orderLinkId"].startswith(LINK_PREFIX) for o in first.values())
    assert sum(float(o["qty"]) for o in first.values()) == 100.0
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 10.0)                  # ничего не изменилось
    assert fake.orders == first and not fake.amended and not fake.cancelled
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 10.05)                 # крошечный дрейф: в пределах допуска — не трогаем
    assert not fake.amended and not fake.cancelled
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 40.0)                  # недостающее выросло — уровни уходят дальше
    assert fake.amended and not fake.cancelled
    lad.tick([lpos(size=0.5)], {"XUSDT": ladder_book(100.0)}, 40.0) if False else None
    lad.tick([], {}, 40.0)                                                   # позиция закрылась — ордера сняты
    assert not fake.orders and fake.cancelled


def test_ladder_is_cancelled_when_goal_is_reached():
    fake = LadderFake()
    lad = make_ladder(fake)
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 10.0)
    assert fake.orders
    lad.tick([lpos()], {"XUSDT": ladder_book(100.0)}, 0.0)                   # недостающего нет — цель достигнута
    assert not fake.orders


# ---------- Bot.fast_tick с потоками-заглушками ----------
class _Pub:
    def __init__(self, mark=100.0): self.mark, self.ensured = mark, set()
    def healthy(self, a=3.0): return True
    def ensure(self, s): self.ensured |= set(s)
    def ticker(self, sym, max_age=3.0): return {"markPrice": self.mark}
    def book(self, sym, max_age=3.0): return ladder_book(self.mark)


class _Priv:
    def __init__(self, positions): self._p = positions
    def healthy(self, a=5.0): return True
    def positions(self): return self._p
    def open_orders(self): return []
    def maybe_resync(self, every=60.0): pass


def fast_bot(tmp_path, tp=True, **kw):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path, target_profit_pct=2.0, **kw)
    bot.step()                                              # создаёт день: база 500, цель +10 USDT
    bot.pub, bot.priv = _Pub(100.0), _Priv([{"symbol": "XUSDT", "side": "Buy", "size": "100", "avgPrice": "100", "positionIdx": 0}])
    fake = LadderFake()
    bot.ladder = make_ladder(fake) if tp else None
    return bot, fake


def test_fast_tick_places_ladder_while_below_goal_and_wakes_when_goal_reached(tmp_path):
    bot, fake = fast_bot(tmp_path)
    bot.fast_tick()
    assert fake.orders and not bot._wake.is_set()          # цель не достигнута: лесенка стоит, будить шаг не нужно
    bot.pub.mark = 100.2                                   # +20 по позиции 100 шт => результат выше цели (+10)
    bot._last_fast_wake = 0.0
    bot.fast_tick()
    assert bot._wake.is_set()                              # цель по живым ценам — сразу полный шаг


def test_fast_tick_cancels_ladder_when_goal_is_latched(tmp_path):
    bot, fake = fast_bot(tmp_path)
    bot.fast_tick()
    assert fake.orders
    bot.goal_latched = True
    bot.fast_tick()
    assert not fake.orders


def test_ladder_is_a_tight_cluster_at_goal_coef_times_the_daily_goal(tmp_path):
    """Цель 2% от базы 500 = 10 USDT, коэффициент 1.3 -> пачка на результате дня 13 USDT; результат пока 0 -> недостающее 13."""
    bot, fake = fast_bot(tmp_path)
    bot.cfg = cfg(target_profit_pct=2.0, tp_goal_coef=1.3, tp_max_orders=4, tp_book_fraction=0.3, tp_density_window_bps=20)
    bot.ladder.cfg = bot.cfg
    bot.fast_tick()
    prices = sorted(float(o["price"]) for o in fake.orders.values())
    assert len(prices) >= 2                                   # пачка из нескольких лимиток, а не одна заявка
    assert prices[0] >= 100.13 - 1e-9                         # первая — на уровне 1.3 x цели (13 USDT / 100 шт = 0.13)
    assert prices[-1] - prices[0] <= 0.5                      # все вплотную друг к другу
    assert sum(float(o["qty"]) for o in fake.orders.values()) == 100.0


def test_start_streams_logs_and_starts_both_streams_with_ladder_enabled(tmp_path, caplog):
    cl = FakeClient(legs={0: ["Buy", 0.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path, use_websocket=True, tp_ladder=True)
    assert bot.pub is not None and bot.priv is not None and bot.ladder is not None
    started = []
    bot.pub.start = lambda: started.append("pub")
    bot.priv.start = lambda: started.append("priv")
    with caplog.at_level("INFO"):
        bot.start_streams()
    assert started == ["pub", "priv"] and any("пачка на 2.0" in r.getMessage() for r in caplog.records)


def test_ladder_is_cancelled_before_the_closing_engine_places_orders(tmp_path):
    bot, fake = fast_bot(tmp_path)
    bot.fast_tick()
    assert fake.orders                                        # пачка стоит
    seen = []
    orig = bot.closer.close_all
    bot.closer.close_all = lambda ps, books, *a: (seen.append(len(fake.orders)), orig(ps, books, *a))[1]
    bot._close_all([lpos()], {"XUSDT": ladder_book(100.0)})
    assert seen == [0]                                        # к моменту первого закрывающего ордера пачки уже нет


# ---------- закрытие пачкой: вся позиция сразу ----------
from bot.ladder import CLOSE_PREFIX, ClosePack, plan_close_pack  # noqa: E402


def test_close_pack_covers_whole_position_starting_at_best_price_long_and_short():
    c = cfg(close_pack_max_orders=5, close_pack_window_bps=30, close_pack_fraction=0.3)
    longp = plan_close_pack("Buy", 300.0, ladder_book(100.0), LADDER_INSTR, c)
    assert len(longp) >= 2 and sum(s.qty for s in longp) == _D(300)
    assert longp[0].price == _D("100.1")                       # первая часть — на лучшей цене продажи (best ask), мейкер
    assert all(s.price >= _D("100.1") for s in longp) and len(longp) <= 5
    shortp = plan_close_pack("Sell", 300.0, ladder_book(100.0), LADDER_INSTR, c)
    assert shortp[0].price == _D("99.9") and all(s.price <= _D("99.9") for s in shortp)   # шорт: покупаем у best bid и ниже
    assert sum(s.qty for s in shortp) == _D(300)
    assert plan_close_pack("Buy", 300.0, None, LADDER_INSTR, c) == []


def test_close_pack_manager_places_all_then_follows_best_price_with_amend():
    fake = LadderFake()
    c = cfg(close_pack=True, close_pack_max_orders=4, close_pack_window_bps=30, close_pack_fraction=0.3)
    pack = ClosePack(fake, c, lambda s: LADDER_INSTR, lambda: list(fake.orders.values()))
    pos = lpos(size=300.0)
    pack.tick([pos], {"XUSDT": ladder_book(100.0)}, 1.0)
    assert fake.orders and all(o["orderLinkId"].startswith(CLOSE_PREFIX) for o in fake.orders.values())
    assert sum(float(o["qty"]) for o in fake.orders.values()) == 300.0          # вся позиция сразу, а не кусок
    first = dict(fake.orders)
    pack.tick([pos], {"XUSDT": ladder_book(100.0)}, 1.0)                        # рынок не двигался — ничего не трогаем
    assert fake.orders == first and not fake.amended
    pack.tick([pos], {"XUSDT": ladder_book(99.6)}, 1.0)                         # рынок ушёл вниз — переставляем вслед
    assert fake.amended and min(float(o["price"]) for o in fake.orders.values()) < 100.0


class PackFake(MultiFake):
    """Позиции MultiFake + ордера пачки (стоят в стакане, не исполняются)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pk, self.n, self.amended, self.cancelled = {}, 0, [], []

    def place_limit_order(self, c, symbol, side, qty, price, idx, link, reduce_only):
        self.n += 1
        oid = f"p{self.n}"
        self.pk[oid] = {"orderId": oid, "symbol": symbol, "side": side, "qty": qty, "leavesQty": qty, "price": price,
                        "positionIdx": idx, "orderLinkId": link, "reduceOnly": True}
        return oid

    def get_open_orders(self, category, settle): return list(self.pk.values())

    def amend_order(self, c, s, oid, price, qty):
        self.amended.append(oid)
        self.pk[oid].update(price=price, qty=qty, leavesQty=qty)

    def cancel_order(self, c, s, oid):
        self.cancelled.append(oid)
        self.pk.pop(oid, None)


def test_goal_closes_every_position_with_a_pack_in_one_step_and_cancel_removes_it(tmp_path):
    cl = PackFake(legs={0: ["Buy", 3.0, 100.0], 1: ["Buy", 3.0, 100.0], 2: ["Buy", 3.0, 100.0]})
    bot, store, clock = make_bot(cl, tmp_path, close_pack=True)
    bot.step()                          # база
    cl.add_realized(20.0)               # цель достигнута
    bot.step()
    by_idx = {}
    for o in cl.pk.values():
        by_idx[o["positionIdx"]] = by_idx.get(o["positionIdx"], 0.0) + float(o["qty"])
    assert by_idx == {0: 3.0, 1: 3.0, 2: 3.0}          # на КАЖДУЮ позицию выставлена пачка на весь её объём
    assert all(o["orderLinkId"].startswith(CLOSE_PREFIX) for o in cl.pk.values())
    bot.closer.cancel_all()                            # цель снята — пачка снимается
    assert not cl.pk


# ---------- пачка без данных стакана и пачка копии ----------
def test_ladder_beyond_the_visible_book_is_still_a_cluster_of_adjacent_ticks():
    c = cfg(tp_max_orders=5, tp_density_window_bps=20)
    short_book = {"bids": [(99.9, 500.0)], "asks": [(100.1, 100.0), (100.2, 100.0)]}      # стакан доходит только до 100.2
    out = plan_ladder("Buy", 100.0, 100.0, 40.0, short_book, LADDER_INSTR, c, maker_fee=0.0)   # уровень далеко за стаканом
    assert len(out) == 5 and sum(s.qty for s in out) == _D(100)          # пачка из нескольких лимиток, а не один ордер
    prices = [s.price for s in out]
    assert prices == sorted(prices) and prices[0] >= _D("100.2") and prices[-1] - prices[0] <= _D("1.5")
    short = plan_ladder("Sell", 100.0, 100.0, 40.0, short_book, LADDER_INSTR, c, maker_fee=0.0)
    assert len(short) == 5 and all(s.price < _D("99.9") for s in short)


class CopyLadderFake(CopyFake):
    """Копия с хранением ордеров пачки."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.tp = {}

    def place_limit_order(self, c, s, side, qty, price, idx, link, reduce_only):
        oid = super().place_limit_order(c, s, side, qty, price, idx, link, reduce_only)
        if str(link).startswith("m2p-tp-"):
            self.tp[oid] = {"orderId": oid, "symbol": s, "side": side, "qty": qty, "leavesQty": qty, "price": price,
                            "positionIdx": idx, "orderLinkId": link, "reduceOnly": True}
        return oid

    def get_open_orders(self, c, s): return list(self.tp.values())

    def amend_order(self, c, s, oid, price, qty): self.tp[oid].update(price=price, qty=qty, leavesQty=qty)

    def cancel_order(self, c, s, oid):
        self.tp.pop(oid, None)
        super().cancel_order(c, s, oid)


def ladder_follower(tmp_path, client):
    f = make_follower(tmp_path, client, tp_ladder=True, tp_ladder_followers=True)
    f.bot._day_row = f.store.get_day(f.bot.clock().date().isoformat())      # база 1000, цель 2% = 20, пачка на 1.3 x = 26
    f.bot._last_realized = 0.0
    return f


MAIN10 = [PositionView("BTCUSDT", "Buy", 10.0, 100.0, 100.0, 0.0, 0, 1.0, leverage=5.0)]


def test_follower_gets_its_own_take_profit_pack_when_synced(tmp_path):
    cl = CopyLadderFake(equity=1000.0, size=100.0)        # на копии ровно столько, сколько нужно (10 x ×10)
    f = ladder_follower(tmp_path, cl)
    assert f.ladder is not None
    f._sync(MAIN10, 100.0)
    assert cl.tp and all(o["side"] == "Sell" for o in cl.tp.values())
    assert abs(sum(float(o["qty"]) for o in cl.tp.values()) - 100.0) < 1e-6 and len(cl.tp) >= 2
    assert min(float(o["price"]) for o in cl.tp.values()) > 100.0         # выше рынка (уровень 1.3 x цели копии)


def test_follower_pack_is_skipped_for_a_position_the_sync_is_still_building(tmp_path):
    cl = CopyLadderFake(equity=1000.0, size=40.0)         # нужно 100, есть 40: синхронизация докупает
    f = ladder_follower(tmp_path, cl)
    f._sync(MAIN10, 100.0)
    assert cl.placed and not cl.tp                         # синхронизация работает, пачки на этой позиции нет


def test_follower_take_profit_fill_triggers_goal_check_instead_of_rebuying(tmp_path):
    cl = CopyLadderFake(equity=1000.0, size=100.0)
    f = ladder_follower(tmp_path, cl)
    steps = []
    f.bot.step = lambda: steps.append(1)
    f._sync(MAIN10, 100.0)                                # пачка выставлена
    assert cl.tp
    cl.size = 70.0                                         # тейк-профит исполнился: позиция уменьшилась без участия синхронизации
    placed_before = len(cl.placed)
    f._sync(MAIN10, 100.0)
    assert steps == [1] and len(cl.placed) == placed_before       # проверили цель и НЕ докупили обратно


# ---------- пауза после цели: через GOAL_COOLDOWN_MIN цель сбрасывается ----------
def _goal_and_flat(tmp_path, **kw):
    cl = FakeClient()
    bot, store, clock = make_bot(cl, tmp_path, **kw)
    bot.step()
    cl.mark = 104.0
    for _ in range(15):
        bot.step()
        if all(l[1] <= 1e-9 for l in cl.legs.values()):
            break
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    return cl, bot, store, clock


def test_goal_is_kept_during_cooldown(tmp_path):
    cl, bot, store, clock = _goal_and_flat(tmp_path, goal_cooldown_min=90)
    clock.advance(minutes=89)
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1
    assert store.has_goal_event("2026-10-19")


def test_goal_resets_after_cooldown_with_new_base(tmp_path):
    cl, bot, store, clock = _goal_and_flat(tmp_path, goal_cooldown_min=90)
    clock.advance(minutes=91)
    bot.step()
    row = store.get_day("2026-10-19")
    assert row["goal_reached"] == 0 and row["start_kind"] == "cycle"
    assert abs(row["start_equity"] - cl.get_equity("")) < 1e-9
    assert not store.has_goal_event("2026-10-19")
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 0


def test_follower_has_no_own_timer_resets_only_on_signal(tmp_path):
    cl, bot, store, clock = _goal_and_flat(tmp_path, goal_cooldown_min=0)
    clock.advance(minutes=300)
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 1       # своего таймера нет
    bot.force_reset = True                                         # основной сбросил цель
    bot.step()
    assert store.get_day("2026-10-19")["goal_reached"] == 0


# ---------- копии: маленькие рыночные порции поверх лимиток ----------
def test_trickle_is_small_fraction_of_depth():
    from bot.closer import plan_trickle
    c = cfg(trickle_fraction=0.02)
    q = plan_trickle(pos(size=100.0), make_book(), INSTR, c)
    assert q is not None and 0 < float(q) <= 10.0 * 0.02 + 1e-9        # не больше 2% глубины (10)


def test_trickle_skips_wide_spread_and_dust():
    from bot.closer import plan_trickle
    wide = {"bids": [(100.0, 5.0)], "asks": [(101.0, 5.0)]}
    assert plan_trickle(pos(size=100.0), wide, INSTR, cfg()) is None
    assert plan_trickle(pos(size=0.2), make_book(), INSTR, cfg()) is None   # остаток после порции меньше лота


def test_trickle_market_sent_only_when_enabled_and_throttled(tmp_path):
    class Cl(FakeClient):
        def __init__(self): super().__init__(); self.mkt = []
        def place_market_close(self, c, s, side, qty, idx, link): self.mkt.append((side, qty, link))
    cl = Cl()
    cl.clock = Clock(datetime(2026, 10, 19, 14, 0, tzinfo=MSK))
    c = cfg(close_pack=False)
    cl_closer = PositionCloser(cl, c, sleep=lambda s: None)
    p = pos(size=100.0)
    cl_closer.close_all([p], {p.symbol: make_book()})
    assert cl.mkt == []                                   # у основного счёта рынка нет
    cl_closer.trickle = True
    cl_closer.close_all([p], {p.symbol: make_book()})
    assert len(cl.mkt) == 1 and cl.mkt[0][0] == "Sell"
    cl_closer.close_all([p], {p.symbol: make_book()})
    assert len(cl.mkt) == 1                               # не чаще, чем раз в trickle_interval_sec


def test_trickle_market_only_while_result_not_below_target(tmp_path):
    class Cl(FakeClient):
        def __init__(self): super().__init__(); self.mkt = []
        def place_market_close(self, c, s, side, qty, idx, link): self.mkt.append(qty)
    cl = Cl()
    cl.clock = Clock(datetime(2026, 10, 19, 14, 0, tzinfo=MSK))
    closer = PositionCloser(cl, cfg(trickle_interval_sec=0.0), sleep=lambda s: None)
    closer.trickle = True
    p = pos(size=100.0)
    closer.close_all([p], {p.symbol: make_book()}, allow_market=False)
    assert cl.mkt == []
    closer.close_all([p], {p.symbol: make_book()}, allow_market=True)
    assert len(cl.mkt) == 1
