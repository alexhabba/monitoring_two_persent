"""Конфигурация из переменных окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv необязателен для тестов
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False
    base_url: str = ""
    dry_run: bool = True
    account_type: str = "UNIFIED"
    category: str = "linear"
    settle_coin: str = "USDT"
    target_profit_pct: float = 2.0
    profit_base: str = "equity"  # equity | margin
    poll_interval_sec: float = 10.0
    close_loop_sleep_sec: float = 1.0
    slippage_buffer_bps: float = 2.0
    book_depth_bps: float = 10.0
    book_fraction: float = 0.10
    max_spread_bps: float = 20.0
    order_timeout_sec: float = 15.0
    book_limit: int = 50

    @classmethod
    def from_env(cls) -> "Config":
        if load_dotenv is not None:
            load_dotenv()
        cfg = cls(
            api_key=_str("BYBIT_API_KEY", ""),
            api_secret=_str("BYBIT_API_SECRET", ""),
            testnet=_bool("BYBIT_TESTNET", False),
            base_url=_str("BYBIT_BASE_URL", ""),
            dry_run=_bool("DRY_RUN", True),
            account_type=_str("ACCOUNT_TYPE", "UNIFIED"),
            category=_str("CATEGORY", "linear"),
            settle_coin=_str("SETTLE_COIN", "USDT"),
            target_profit_pct=_float("TARGET_PROFIT_PCT", 2.0),
            profit_base=_str("PROFIT_BASE", "equity").lower(),
            poll_interval_sec=_float("POLL_INTERVAL_SEC", 10.0),
            close_loop_sleep_sec=_float("CLOSE_LOOP_SLEEP_SEC", 1.0),
            slippage_buffer_bps=_float("SLIPPAGE_BUFFER_BPS", 2.0),
            book_depth_bps=_float("BOOK_DEPTH_BPS", 10.0),
            book_fraction=_float("BOOK_FRACTION", 0.10),
            max_spread_bps=_float("MAX_SPREAD_BPS", 20.0),
            order_timeout_sec=_float("ORDER_TIMEOUT_SEC", 15.0),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.profit_base not in ("equity", "margin"):
            raise ValueError("PROFIT_BASE должен быть 'equity' или 'margin'")
        if not 0 < self.book_fraction <= 1:
            raise ValueError("BOOK_FRACTION должен быть в (0, 1]")
        if self.target_profit_pct <= 0:
            raise ValueError("TARGET_PROFIT_PCT должен быть > 0")
