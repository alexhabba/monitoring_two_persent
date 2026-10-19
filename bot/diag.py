"""Диагностика: что Bybit отдаёт за сегодня (лог транзакций, закрытые позиции, позиции, баланс). Ничего не меняет."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .bybit_client import BybitClient
from .config import Config
from .daypnl import INCLUDED_TYPES
from .pnl import parse_position


def _t(ms, tz) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz).strftime("%H:%M:%S")


def run_diag(client: BybitClient, cfg: Config, tz, out=print) -> None:
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(midnight.timestamp() * 1000)
    client.sync_time()
    end_ms = int(now.timestamp() * 1000) + client.time_offset_ms + 60_000

    equity = client.get_equity(cfg.account_type)
    positions = [parse_position(r) for r in client.get_positions(cfg.category, cfg.settle_coin)]
    out(f"Сегодня с {midnight.isoformat()} (пояс {cfg.timezone})")
    out(f"Баланс (totalEquity): {equity:.4f} | открытых позиций: {len(positions)}"
        + (f" | нереализованный PnL: {sum(p.unrealised_pnl for p in positions):+.4f}" if positions else ""))

    rows = client.get_transaction_log(cfg.account_type, None, cfg.settle_coin, start_ms, end_ms)
    rows = [r for r in rows if int(r.get("transactionTime") or 0) >= start_ms]
    out(f"\nЛог транзакций ({cfg.settle_coin}, все категории) за сегодня: {len(rows)} записей")
    agg = defaultdict(lambda: [0, 0.0])
    for r in rows:
        k = (r.get("type"), r.get("category") or "-")
        agg[k][0] += 1
        agg[k][1] += float(r.get("change") or 0)
    total_all = sum(v[1] for v in agg.values())
    total_trade = sum(v[1] for (typ, _), v in agg.items() if typ in INCLUDED_TYPES and _ == cfg.category)
    for (typ, cat), (n, ch) in sorted(agg.items(), key=lambda kv: -abs(kv[1][1])):
        mark = "← считается" if typ in INCLUDED_TYPES and cat == cfg.category else ""
        out(f"  {typ:<22} {cat:<8} записей {n:>4}  сумма change {ch:+12.4f} {mark}")
    out(f"  ИТОГО все типы: {total_all:+.4f} | из них торговый результат (категория {cfg.category}): {total_trade:+.4f}")
    est_mid = equity - total_all
    est_cap = equity - total_trade
    base = max(est_mid, est_cap)
    out(f"  Баланс на 00:00 (баланс − все изменения): {est_mid:.4f}"
        + ("   <-- на 00:00 счёт был пуст / пополнен днём" if est_mid <= 0 else ""))
    out(f"  Капитал без результата дня (баланс − торговый результат): {est_cap:.4f}")
    if base > 0:
        out(f"  База для процента = {base:.4f}. Результат дня по логу: {total_trade:+.4f} = "
            f"{total_trade / base * 100:+.3f}% (цель {cfg.target_profit_pct}%) -> "
            + ("ЦЕЛЬ ВЫПОЛНЕНА" if total_trade / base * 100 >= cfg.target_profit_pct else "цель не выполнена"))
    else:
        out("  База не определена (≤ 0).")

    out("\nПоследние записи лога:")
    for r in sorted(rows, key=lambda r: int(r.get("transactionTime") or 0))[-15:]:
        out(f"  {_t(r['transactionTime'], tz)} {str(r.get('type')):<14} {str(r.get('symbol') or '-'):<12} "
            f"{str(r.get('category') or '-'):<7} change {float(r.get('change') or 0):+10.4f} "
            f"(cashFlow {r.get('cashFlow')}, funding {r.get('funding')}, fee {r.get('fee')})")

    closed = client.get_closed_pnl(cfg.category, start_ms, end_ms)
    closed = [r for r in closed if int(r.get("updatedTime") or r.get("createdTime") or 0) >= start_ms]
    out(f"\nЗакрытые позиции за сегодня ({cfg.category}): {len(closed)}; сумма closedPnl = "
        f"{sum(float(r.get('closedPnl') or 0) for r in closed):+.4f}")
    for r in closed[:15]:
        out(f"  {_t(r.get('updatedTime') or r.get('createdTime'), tz)} {r.get('symbol'):<12} {r.get('side'):<5} "
            f"closedPnl {float(r.get('closedPnl') or 0):+10.4f} (комиссии: открытие {r.get('openFee')}, закрытие {r.get('closeFee')})")
    out("\nГотово. Ничего не изменено.")
