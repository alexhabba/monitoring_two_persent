"""Точка входа: python main.py [--once] [--live]"""
import argparse
import dataclasses
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from bot.bybit_client import BybitClient
from bot.config import Config
from bot.monitor import Bot


def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def main() -> None:
    ap = argparse.ArgumentParser(description="Bybit: закрытие позиций при чистой прибыли >= цели")
    ap.add_argument("--once", action="store_true", help="один проход оценки и выход")
    ap.add_argument("--live", action="store_true", help="боевой режим (иначе используется DRY_RUN из .env)")
    args = ap.parse_args()

    setup_logging()
    cfg = Config.from_env()
    if args.live:
        cfg = dataclasses.replace(cfg, dry_run=False)
    if not cfg.api_key or not cfg.api_secret:
        sys.exit("Заполните BYBIT_API_KEY и BYBIT_API_SECRET в .env")

    client = BybitClient(cfg.api_key, cfg.api_secret, cfg.testnet, cfg.base_url)
    bot = Bot(client, cfg)
    if args.once:
        bot.step()
    else:
        bot.run()


if __name__ == "__main__":
    main()
