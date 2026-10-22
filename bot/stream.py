"""WebSocket Bybit V5: стакан и цены (публичный поток) и позиции/ордера/исполнения аккаунта (приватный поток).

Данные приходят сами, с задержкой порядка 0.1 с, поэтому бот не «слепнет» между опросами REST. Потоки — только источник
данных: если соединение потеряно или данные устарели (`healthy()` ложно), код возвращается к обычным запросам REST.

Обработчики сообщений (`handle`) отделены от сокета, чтобы их можно было проверять без сети.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("stream")

PUBLIC_URL = "wss://stream.bybit.com/v5/public/linear"
PUBLIC_URL_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"
PRIVATE_URL = "wss://stream.bybit.com/v5/private"
PRIVATE_URL_TESTNET = "wss://stream-testnet.bybit.com/v5/private"
FINAL_ORDER = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled", "Triggered"}


class _Stream:
    """Общий каркас: поток с переподключением, пинг-понг, повторная подписка."""

    name = "stream"

    def __init__(self, url: str, ping_sec: float = 20.0):
        self.url = url
        self.ping_sec = ping_sec
        self._lock = threading.RLock()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.last_msg = 0.0
        self.last_pong = 0.0
        self._topics: list[str] = []

    # ----- переопределяется -----
    def _on_open(self) -> None: ...
    def handle(self, msg: dict) -> None: ...

    # ----- управление -----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def send(self, obj: dict) -> None:
        ws = self._ws
        if ws is not None and self.connected:
            ws.send(json.dumps(obj))

    def subscribe(self, topics: list[str]) -> None:
        with self._lock:
            new = [t for t in topics if t not in self._topics]
            self._topics.extend(new)
        if new:
            self.send({"op": "subscribe", "args": new})

    def healthy(self, max_age: float = 5.0) -> bool:
        """Соединение есть и что-то приходило (данные или ответ на ping) не позже max_age секунд назад."""
        return self.connected and (time.time() - max(self.last_msg, self.last_pong)) <= max_age

    # ----- сокет -----
    def _run(self) -> None:
        import websocket  # websocket-client; импорт здесь, чтобы тесты без сети его не требовали
        delay = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.url, on_open=self._open, on_message=self._message, on_close=self._close, on_error=self._error)
            started = time.time()
            try:
                self._ws.run_forever(ping_interval=self.ping_sec, ping_timeout=10)
            except Exception:  # noqa: BLE001
                log.exception("%s: ошибка сокета", self.name)
            self.connected = False
            if self._stop.is_set():
                return
            delay = 1.0 if time.time() - started > 60 else min(delay * 2, 15.0)   # стабильное соединение — сбрасываем паузу
            log.warning("%s: соединение потеряно, переподключение через %.0f с", self.name, delay)
            self._stop.wait(delay)

    def _open(self, ws) -> None:
        self.connected = True
        self.last_msg = self.last_pong = time.time()
        try:
            self._on_open()
            with self._lock:
                topics = list(self._topics)
            if topics:
                self.send({"op": "subscribe", "args": topics})
        except Exception:  # noqa: BLE001
            log.exception("%s: ошибка при открытии", self.name)

    def _message(self, ws, raw) -> None:
        self.last_msg = time.time()
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("op") in ("ping", "pong") or msg.get("ret_msg") == "pong":
            self.last_pong = time.time()
            return
        try:
            self.handle(msg)
        except Exception:  # noqa: BLE001
            log.exception("%s: ошибка обработки сообщения", self.name)

    def _close(self, ws, code=None, reason=None) -> None:
        self.connected = False

    def _error(self, ws, err) -> None:
        log.warning("%s: ошибка %s", self.name, str(err)[:120])


class PublicStream(_Stream):
    """Стаканы (orderbook.50) и тикеры (mark/last/bid/ask) по символам."""

    name = "ws-public"

    def __init__(self, testnet: bool = False, depth: int = 50):
        super().__init__(PUBLIC_URL_TESTNET if testnet else PUBLIC_URL)
        self.depth = depth
        self._bids: dict[str, dict[float, float]] = {}
        self._asks: dict[str, dict[float, float]] = {}
        self._book_ts: dict[str, float] = {}
        self._tick: dict[str, dict] = {}
        self._tick_ts: dict[str, float] = {}

    def ensure(self, symbols) -> None:
        topics = []
        for s in symbols:
            topics += [f"orderbook.{self.depth}.{s}", f"tickers.{s}"]
        self.subscribe(topics)

    def handle(self, msg: dict) -> None:
        topic = str(msg.get("topic", ""))
        data = msg.get("data")
        if not data:
            return
        if topic.startswith("orderbook."):
            self._apply_book(data, snapshot=msg.get("type") == "snapshot")
        elif topic.startswith("tickers."):
            self._apply_ticker(data)

    def _apply_book(self, d: dict, snapshot: bool) -> None:
        sym = d["s"]
        with self._lock:
            if snapshot or d.get("u") == 1:     # u == 1 — сервер Bybit перезапустил стакан: это снова снимок
                self._bids[sym], self._asks[sym] = {}, {}
            bids, asks = self._bids.setdefault(sym, {}), self._asks.setdefault(sym, {})
            for side, rows in ((bids, d.get("b", [])), (asks, d.get("a", []))):
                for p, q in rows:
                    p, q = float(p), float(q)
                    if q == 0:
                        side.pop(p, None)
                    else:
                        side[p] = q
            self._book_ts[sym] = time.time()

    def _apply_ticker(self, d: dict) -> None:
        sym = d["symbol"]
        with self._lock:
            cur = self._tick.setdefault(sym, {})
            for k in ("markPrice", "lastPrice", "bid1Price", "ask1Price", "indexPrice"):
                if d.get(k) not in (None, ""):
                    cur[k] = float(d[k])
            self._tick_ts[sym] = time.time()

    def book(self, symbol: str, max_age: float = 3.0) -> dict | None:
        """{'bids': [(цена, объём)...] от лучшей, 'asks': [...]} как у REST; None — нет свежих данных."""
        with self._lock:
            if time.time() - self._book_ts.get(symbol, 0) > max_age:
                return None
            bids = sorted(self._bids.get(symbol, {}).items(), key=lambda x: -x[0])[: self.depth]
            asks = sorted(self._asks.get(symbol, {}).items(), key=lambda x: x[0])[: self.depth]
        if not bids or not asks or bids[0][0] >= asks[0][0]:      # пустой или «перекрещенный» стакан — не доверяем
            return None
        return {"bids": bids, "asks": asks}

    def ticker(self, symbol: str, max_age: float = 3.0) -> dict | None:
        with self._lock:
            if time.time() - self._tick_ts.get(symbol, 0) > max_age or "markPrice" not in self._tick.get(symbol, {}):
                return None
            return dict(self._tick[symbol])


class PrivateStream(_Stream):
    """Позиции, ордера и исполнения аккаунта. Начальное состояние берётся из REST (поток шлёт только изменения)."""

    name = "ws-private"

    def __init__(self, api_key: str, api_secret: str, client, category: str = "linear", settle_coin: str = "USDT",
                 testnet: bool = False, on_execution: Callable[[dict], None] | None = None, label: str = ""):
        super().__init__(PRIVATE_URL_TESTNET if testnet else PRIVATE_URL)
        self.api_key, self.api_secret = api_key, api_secret
        self.client = client
        self.category, self.settle_coin = category, settle_coin
        self.on_execution = on_execution
        self.name = f"ws-private{':' + label if label else ''}"
        self._positions: dict[tuple[str, int], dict] = {}
        self._orders: dict[str, dict] = {}
        self._ready = False
        self._synced_at = 0.0

    def _on_open(self) -> None:
        self._ready = False
        expires = int((time.time() + 10) * 1000)
        sig = hmac.new(self.api_secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
        self.send({"op": "auth", "args": [self.api_key, expires, sig]})

    def handle(self, msg: dict) -> None:
        if msg.get("op") == "auth":
            if msg.get("success"):
                self.send({"op": "subscribe", "args": ["position.linear", "order.linear", "execution.linear"]})
                self.resync()
            else:
                log.error("%s: авторизация не прошла: %s", self.name, msg.get("ret_msg"))
            return
        topic = str(msg.get("topic", ""))
        data = msg.get("data") or []
        if topic.startswith("position"):
            self._apply_positions(data)
        elif topic.startswith("order"):
            self._apply_orders(data)
        elif topic.startswith("execution"):
            for e in data:
                if self.on_execution:
                    self.on_execution(e)

    def resync(self) -> None:
        """Полный снимок из REST (при подключении и раз в минуту — страховка от пропущенных сообщений)."""
        try:
            pos = self.client.get_positions(self.category, self.settle_coin)
            orders = self.client.get_open_orders(self.category, self.settle_coin)
        except Exception:  # noqa: BLE001
            log.exception("%s: не удалось получить снимок REST", self.name)
            return
        with self._lock:
            self._positions = {(p["symbol"], int(p.get("positionIdx") or 0)): dict(p) for p in pos}
            self._orders = {o["orderId"]: dict(o) for o in orders}
            self._ready = True
            self._synced_at = time.time()

    def _apply_positions(self, rows: list[dict]) -> None:
        with self._lock:
            for r in rows:
                key = (r["symbol"], int(r.get("positionIdx") or 0))
                if float(r.get("size") or 0) <= 0:
                    self._positions.pop(key, None)
                    continue
                cur = dict(r)
                cur["avgPrice"] = r.get("entryPrice") or r.get("avgPrice") or "0"    # в потоке поле называется entryPrice
                self._positions[key] = cur

    def _apply_orders(self, rows: list[dict]) -> None:
        with self._lock:
            for o in rows:
                if o.get("orderStatus") in FINAL_ORDER:
                    self._orders.pop(o["orderId"], None)
                else:
                    self._orders[o["orderId"]] = dict(o)

    def positions(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._positions.values()]

    def open_orders(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._orders.values()]

    def healthy(self, max_age: float = 5.0) -> bool:
        return self._ready and super().healthy(max_age)

    def maybe_resync(self, every: float = 60.0) -> None:
        if self._ready and time.time() - self._synced_at > every:
            self.resync()
