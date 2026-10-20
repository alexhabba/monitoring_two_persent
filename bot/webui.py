"""Веб-интерфейс настройки ключей: основной счёт и список копирующих. Кнопка «Сохранить» перезапускает бота.

Включается, только если задан UI_PASSWORD. Слушает UI_HOST:UI_PORT (по умолчанию 127.0.0.1:8080) —
наружу в интернет его выставлять нельзя (HTTP без шифрования): заходите через SSH-туннель.
Доступ — HTTP Basic (логин любой, пароль UI_PASSWORD). Секреты браузеру не возвращаются.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import accounts
from .bybit_client import BybitClient

log = logging.getLogger("webui")
PAGE = Path(__file__).with_name("webui.html")


def check_keys(path: str, body: dict) -> dict:
    """Проверка ключей запросом баланса. Пустой секрет — берём сохранённый у счёта с тем же именем."""
    key, secret = str(body.get("api_key", "")).strip(), str(body.get("api_secret", "")).strip()
    if not secret:
        raw = accounts.read_raw(path)
        name = body.get("name")
        src = raw.get("main") if name in (None, "", "main") else next(
            (f for f in raw.get("followers", []) if f.get("name") == name), {})
        secret = (src or {}).get("api_secret", "")
    if not key or not secret:
        return {"ok": False, "error": "Укажите API Key и API Secret"}
    try:
        equity = BybitClient(key, secret, bool(body.get("testnet", False)), timeout=8.0).get_equity("UNIFIED")
        return {"ok": True, "equity": equity}
    except Exception as e:  # noqa: BLE001 — показываем причину пользователю
        return {"ok": False, "error": str(e)[:300]}


def make_handler(path: str, password: str, restart):
    class Handler(BaseHTTPRequestHandler):
        server_version = "m2p"

        def log_message(self, fmt, *args):   # не пишем запросы в лог (в них нет секретов, но и шума не нужно)
            pass

        def _authed(self) -> bool:
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    given = base64.b64decode(h[6:]).decode().partition(":")[2]
                    if hmac.compare_digest(given.encode(), password.encode()):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.5)   # притормаживаем подбор пароля
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="m2p"')
            self.end_headers()
            return False

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def do_GET(self):
            if not self._authed():
                return
            if self.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/config":
                self._json(200, accounts.public_view(accounts.read_raw(path)))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            if self.headers.get("X-Requested-With") != "m2p":   # защита от CSRF
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if self.path == "/api/check":
                return self._json(200, check_keys(path, body))
            if self.path == "/api/config":
                try:
                    merged = accounts.save(path, body)
                except (ValueError, KeyError, TypeError) as e:
                    return self._json(400, {"error": str(e)})
                log.warning("Настройки счетов сохранены из веб-интерфейса (копий: %d). Перезапуск бота.",
                            len(merged["followers"]))
                self._json(200, {"ok": True, "restarting": True})
                restart()
                return
            self._json(404, {"error": "not found"})

    return Handler


def restart_process() -> None:
    """Чистая остановка по SIGTERM; Docker (restart: unless-stopped) или systemd поднимают бота заново."""
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


def start(path: str, password: str, host: str, port: int, restart=restart_process) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(path, password, restart))
    threading.Thread(target=srv.serve_forever, name="webui", daemon=True).start()
    log.warning("Веб-интерфейс настроек: http://%s:%d (доступ по паролю UI_PASSWORD)", host, port)
    return srv
