#!/bin/bash
# Использование: ./run.sh          — один проход (без ордеров)
#                ./run.sh loop     — постоянный мониторинг (DRY_RUN из .env)
#                ./run.sh live     — боевой режим (отправляет ордера!)
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
case "$1" in
  loop) python main.py ;;
  live) python main.py --live ;;
  *)    python main.py --once ;;
esac
