"""Точка входа: python main.py [--once] [--live]"""
import argparse
import dataclasses
import logging
import signal
import sys

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
    args = ap.parse_args()

    cfg = Config.from_env()
    if args.live:
        cfg = dataclasses.replace(cfg, dry_run=False)
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
