"""Точка входа: python main.py [--once] [--live]"""
import argparse
import dataclasses
import logging
import signal
import sys
from datetime import datetime

from bot.bybit_client import BybitClient
from bot.config import Config
from bot.logsetup import setup_logging
from bot.monitor import Bot
from bot.state import StateStore
from bot.timeutil import get_tz


def main() -> None:
    ap = argparse.ArgumentParser(description="Bybit: дневная цель прибыли и закрытие позиций")
    ap.add_argument("--once", action="store_true", help="один проход оценки и выход")
    ap.add_argument("--live", action="store_true", help="боевой режим (иначе DRY_RUN из .env)")
    ap.add_argument("--reset-day", action="store_true",
                    help="сбросить сегодняшний день в БД (база и цель пересчитаются при следующем запуске) и выйти")
    ap.add_argument("--diag", action="store_true", help="показать, что Bybit отдаёт за сегодня (ничего не меняет) и выйти")
    args = ap.parse_args()

    cfg = Config.from_env()
    if args.live:
        cfg = dataclasses.replace(cfg, dry_run=False)
    if args.reset_day:
        store = StateStore(cfg.state_path)
        today = datetime.now(get_tz(cfg.timezone)).date().isoformat()
        print(f"{cfg.state_path}: день {today} " + ("сброшен" if store.delete_day(today) else "не найден (нечего сбрасывать)"))
        store.close()
        return
    if args.diag:
        from bot.diag import run_diag
        if not cfg.api_key or not cfg.api_secret:
            sys.exit("Заполните BYBIT_API_KEY и BYBIT_API_SECRET в .env")
        run_diag(BybitClient(cfg.api_key, cfg.api_secret, cfg.testnet, cfg.base_url), cfg, get_tz(cfg.timezone))
        return
    setup_logging(cfg.log_dir, get_tz(cfg.timezone), cfg.log_retention_days)
    if not cfg.api_key or not cfg.api_secret:
        sys.exit("Заполните BYBIT_API_KEY и BYBIT_API_SECRET в .env")

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # чистая остановка на сервере
    store = StateStore(cfg.state_path)
    client = BybitClient(cfg.api_key, cfg.api_secret, cfg.testnet, cfg.base_url)
    bot = Bot(client, cfg, store)
    try:
        if args.once:
            bot.step()
        else:
            bot.run()
    except KeyboardInterrupt:
        logging.getLogger("main").info("Остановлено.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
