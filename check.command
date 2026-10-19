#!/bin/bash
# Двойной клик в Finder: ставит зависимости и делает один безопасный проход (без ордеров).
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
python main.py --once
echo
echo "Готово. Это окно можно закрыть."
read -n 1 -s -r -p "Нажмите любую клавишу..."
