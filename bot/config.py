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


def _floats(name: str, default: tuple) -> tuple:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(float(x) for x in raw.replace(";", ",").split(",") if x.strip())


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
    timezone: str = "Europe/Moscow"
    data_dir: str = "data"
    log_dir: str = "logs"
    log_retention_days: int = 7
    poll_interval_sec: float = 10.0
    close_loop_sleep_sec: float = 1.0
    slippage_buffer_bps: float = 2.0
    book_depth_bps: float = 10.0
    book_fraction: float = 0.10
    max_spread_bps: float = 20.0
    order_timeout_sec: float = 15.0
    book_limit: int = 50
    # --- WebSocket и лесенка тейк-профитов ---
    use_websocket: bool = False          # стакан/позиции/исполнения по WebSocket (при сбое — обычные запросы REST)
    stream_max_age_sec: float = 3.0      # данные старше — считаем устаревшими и идём в REST
    fast_tick_sec: float = 0.25          # частота быстрой проверки (цель по живым данным, лесенка)
    tp_ladder: bool = False              # заранее выставленные тейк-профиты на цену, где счёт достигнет цели
    tp_goal_coef: float = 1.3            # пачка тейк-профитов стоит там, где результат дня = коэффициент × цель дня
    tp_book_fraction: float = 0.2        # не больше этой доли видимой заявки стакана на одном уровне
    tp_density_window_bps: float = 20.0  # окно (б.п.) от уровня, где ищем плотность стакана
    tp_max_orders: int = 6               # не больше стольких тейк-профитов на одну позицию
    tp_replace_ticks: int = 3            # переставляем ордер, только если уровень ушёл больше чем на N шагов цены
    tp_actions_per_tick: int = 4         # не больше стольких операций с ордерами лесенки за тик на позицию
    # --- закрытие по цели пачкой (вся позиция сразу несколькими лимитками) ---
    close_pack: bool = False             # True: после цели ставим на закрытие сразу всю позицию пачкой, а не по куску за секунду
    close_pack_max_orders: int = 5       # не больше стольких ордеров на позицию
    close_pack_window_bps: float = 15.0  # пачка лежит в пределах N б.п. от лучшей цены
    close_pack_fraction: float = 0.3     # не больше этой доли видимой заявки на уровне (если хватает места)
    close_replace_ticks: int = 1         # переставляем, когда лучшая цена ушла больше чем на N шагов
    close_actions_per_tick: int = 6      # не больше стольких операций с ордерами за тик на позицию
    close_workers: int = 8               # сколько позиций закрываем параллельно
    momentum_windows_sec: tuple = (60.0, 120.0)   # окна для приоритета: у кого прибыль выросла сильнее — закрываем первой
    # --- копирование позиций на другие счета ---
    followers_file: str = ""            # JSON со списком счетов; пусто = {data_dir}/followers.json
    copy_tick_sec: float = 1.0
    copy_ratio_refresh_sec: float = 60.0
    base_from_now: bool = False          # тест: база дня = состояние на момент старта, сегодняшняя прибыль не учитывается
    copy_deadband_pct: float = 3.0       # не дёргаем позицию из-за расхождения меньше N% от нужного размера
    ui_password: str = ""               # пусто = веб-интерфейс настроек выключен
    ui_host: str = "127.0.0.1"
    ui_port: int = 8080

    @property
    def state_path(self) -> str:
        net = "testnet" if self.testnet else "mainnet"
        mode = "dry" if self.dry_run else "live"
        # у DRY-RUN и боевого режима разные файлы: «цель выполнена» из теста не должна влиять на боевой режим
        return f"{self.data_dir}/state_{net}_{mode}.db"

    @property
    def followers_path(self) -> str:
        return self.followers_file or f"{self.data_dir}/followers.json"

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
            timezone=_str("TIMEZONE", "Europe/Moscow"),
            data_dir=_str("DATA_DIR", "data"),
            log_dir=_str("LOG_DIR", "logs"),
            log_retention_days=int(_float("LOG_RETENTION_DAYS", 7)),
            poll_interval_sec=_float("POLL_INTERVAL_SEC", 10.0),
            close_loop_sleep_sec=_float("CLOSE_LOOP_SLEEP_SEC", 1.0),
            slippage_buffer_bps=_float("SLIPPAGE_BUFFER_BPS", 2.0),
            book_depth_bps=_float("BOOK_DEPTH_BPS", 10.0),
            book_fraction=_float("BOOK_FRACTION", 0.10),
            max_spread_bps=_float("MAX_SPREAD_BPS", 20.0),
            order_timeout_sec=_float("ORDER_TIMEOUT_SEC", 15.0),
            followers_file=_str("FOLLOWERS_FILE", ""),
            copy_tick_sec=_float("COPY_TICK_SEC", 1.0),
            copy_ratio_refresh_sec=_float("COPY_RATIO_REFRESH_SEC", 60.0),
            base_from_now=_bool("BASE_FROM_NOW", False),
            use_websocket=_bool("USE_WEBSOCKET", False),
            stream_max_age_sec=_float("STREAM_MAX_AGE_SEC", 3.0),
            fast_tick_sec=_float("FAST_TICK_SEC", 0.25),
            tp_ladder=_bool("TP_LADDER", False),
            tp_goal_coef=_float("TP_GOAL_COEF", 1.3),
            tp_book_fraction=_float("TP_BOOK_FRACTION", 0.2),
            tp_density_window_bps=_float("TP_DENSITY_WINDOW_BPS", 20.0),
            tp_max_orders=int(_float("TP_MAX_ORDERS", 6)),
            tp_replace_ticks=int(_float("TP_REPLACE_TICKS", 3)),
            tp_actions_per_tick=int(_float("TP_ACTIONS_PER_TICK", 4)),
            close_pack=_bool("CLOSE_PACK", False),
            close_pack_max_orders=int(_float("CLOSE_PACK_MAX_ORDERS", 5)),
            close_pack_window_bps=_float("CLOSE_PACK_WINDOW_BPS", 15.0),
            close_pack_fraction=_float("CLOSE_PACK_FRACTION", 0.3),
            close_replace_ticks=int(_float("CLOSE_REPLACE_TICKS", 1)),
            close_actions_per_tick=int(_float("CLOSE_ACTIONS_PER_TICK", 6)),
            copy_deadband_pct=_float("COPY_DEADBAND_PCT", 3.0),
            ui_password=_str("UI_PASSWORD", ""),
            ui_host=_str("UI_HOST", "127.0.0.1"),
            ui_port=int(_float("UI_PORT", 8080)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.tp_goal_coef < 1:
            raise ValueError("TP_GOAL_COEF должен быть >= 1 (пачка стоит не ближе цели дня)")
        if not 0 < self.book_fraction <= 1:
            raise ValueError("BOOK_FRACTION должен быть в (0, 1]")
        if self.target_profit_pct <= 0:
            raise ValueError("TARGET_PROFIT_PCT должен быть > 0")
