#!/bin/bash
# Использование: ./run.sh          — один проход (без ордеров)
#                ./run.sh loop     — постоянный мониторинг (DRY_RUN из .env)
#                ./run.sh live     — боевой режим (отправляет ордера!)
#                ./run.sh reset    — сбросить сегодняшний день в БД (dry и live) и выйти
cd "$(dirname "$0")"
if [ ! -f .venv/bin/activate ]; then
  python3 -m venv .venv || { echo "Не удалось создать окружение: установите Python 3 (python.org или 'xcode-select --install')"; exit 1; }
fi
source .venv/bin/activate
python3 -m pip install -q -r requirements.txt
case "$1" in
  loop) python3 main.py ;;
  live) python3 main.py --live ;;
  reset) python3 main.py --reset-day; python3 main.py --reset-day --live ;;
  *)    python3 main.py --once ;;
esac
