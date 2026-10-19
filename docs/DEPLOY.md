# Развёртывание на сервере

Бот — один долгоживущий процесс. Состояние (SQLite) и логи лежат на диске, день считается по Москве независимо от пояса сервера.
Ниже два способа; **не проверялись на реальном сервере** — на первом запуске пройдите шаги по порядку.

## 0. Перед началом
* Выберите VPS в регионе, откуда Bybit доступен и где это допустимо по правилам Bybit и законам вашей страны.
* **API-ключ Bybit**: права только на чтение и торговлю деривативами (Orders/Positions + чтение счёта), **без вывода средств**,
  с привязкой к IP сервера (в настройках ключа). Старый ключ с домашним IP на сервере работать не будет.
* Перед боевым запуском остановите бота на Mac — два бота на одном счёте не нужны. Состояние (`data/*.db`) не переносите, если
  не уверены: новый сервер сам восстановит базу дня по логу транзакций (см. README).

## 1. Вариант A — systemd (Ubuntu/Debian)
```bash
sudo adduser --system --group --home /opt/monitoring_two_persent m2p
sudo apt-get update && sudo apt-get install -y git python3-venv
sudo -u m2p git clone https://github.com/alexhabba/monitoring_two_persent.git /opt/monitoring_two_persent
cd /opt/monitoring_two_persent
sudo -u m2p python3 -m venv .venv && sudo -u m2p .venv/bin/pip install -r requirements.txt
sudo -u m2p cp .env.example .env && sudo -u m2p chmod 600 .env
sudo -u m2p nano .env                 # BYBIT_API_KEY / BYBIT_API_SECRET, DRY_RUN=true на первый запуск
sudo -u m2p .venv/bin/python main.py --diag    # проверка доступа: покажет баланс и сделки за сегодня
sudo -u m2p .venv/bin/python main.py --once    # один проход без ордеров
sudo cp deploy/monitoring_two_persent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now monitoring_two_persent
journalctl -u monitoring_two_persent -f        # живой вывод; файлы логов — /opt/monitoring_two_persent/logs/
```
Боевой режим: в `.env` поставьте `DRY_RUN=false`, затем `sudo systemctl restart monitoring_two_persent`.

## 2. Вариант B — Docker
```bash
git clone https://github.com/alexhabba/monitoring_two_persent.git && cd monitoring_two_persent
cp .env.example .env && chmod 600 .env && nano .env
docker compose run --rm bot python main.py --diag
docker compose up -d --build
docker compose logs -f
```
Состояние и логи — в `./data` (том `/data`); пути `DATA_DIR`/`LOG_DIR` из `.env` внутри контейнера принудительно заменяются на `/data`. Боевой режим: `DRY_RUN=false` в `.env`, затем `docker compose up -d`.

## 3. Обслуживание
* **Обновление:** `git pull`, затем `sudo systemctl restart monitoring_two_persent` (или `docker compose up -d --build`).
* **Резервная копия:** `data/state_*.db` — один файл на режим; копируйте при остановленном боте или через `sqlite3 файл ".backup копия.db"`.
* **Сбросить сегодняшний день:** остановить бота, `python main.py --reset-day` (и `--live`), запустить.
* **Время:** держите синхронизацию часов (`timedatectl`); бот дополнительно сверяется со временем Bybit.
* **Логи:** `logs/bot-YYYY-MM-DD.log`, хранятся 7 дней, старше удаляются автоматически.
