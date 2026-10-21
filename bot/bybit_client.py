"""Тонкий клиент Bybit V5 (REST) на requests с HMAC-подписью."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import requests

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"

# Ошибки, при которых отмену/повторную отмену можно спокойно игнорировать
ORDER_GONE_CODES = {110001, 170213, 170142}
RATE_LIMIT_CODE = 10006   # Too many visits
# Плечо уже такое, какое просили
LEVERAGE_NOT_MODIFIED = 110043


class BybitError(Exception):
    def __init__(self, code: int, msg: str, path: str = ""):
        super().__init__(f"Bybit {path} retCode={code}: {msg}")
        self.code = code
        self.msg = msg


class BybitClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        base_url: str = "",
        recv_window: int = 20000,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = (base_url or (TESTNET if testnet else MAINNET)).rstrip("/")
        self.recv_window = str(recv_window)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.time_offset_ms = 0  # серверное время минус локальное
        self._synced = False

    def sync_time(self) -> None:
        """Поправка на расхождение часов с сервером Bybit."""
        t0 = int(time.time() * 1000)
        res = self._request("GET", "/v5/market/time", auth=False)
        t1 = int(time.time() * 1000)
        server_ms = int(res["timeNano"]) // 1_000_000 if res.get("timeNano") else int(res["timeSecond"]) * 1000
        self.time_offset_ms = server_ms - (t0 + t1) // 2
        self._synced = True

    # ---------- низкий уровень ----------
    def _sign(self, ts: str, payload: str) -> str:
        msg = f"{ts}{self.api_key}{self.recv_window}{payload}"
        return hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, auth: bool = True, _retry: bool = True) -> dict[str, Any]:
        if auth and not self._synced:
            self.sync_time()
        headers = {"Content-Type": "application/json"}
        url = self.base_url + path
        data = None
        payload = ""
        if method == "GET":
            qs = urlencode({k: v for k, v in (params or {}).items() if v is not None})
            payload = qs
            if qs:
                url = f"{url}?{qs}"
        else:
            data = json.dumps(body or {}, separators=(",", ":"))
            payload = data
        if auth:
            ts = str(int(time.time() * 1000) + self.time_offset_ms)
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": self._sign(ts, payload),
            })
        resp = self.session.request(method, url, headers=headers, data=data, timeout=self.timeout)
        resp.raise_for_status()
        js = resp.json()
        if js.get("retCode") == 10002 and auth and _retry:  # часы разошлись — пересинхронизируем
            self.sync_time()
            return self._request(method, path, params, body, auth, _retry=False)
        if js.get("retCode") == RATE_LIMIT_CODE and auth and _retry:   # лимит запросов — короткая пауза и один повтор
            time.sleep(0.6)
            return self._request(method, path, params, body, auth, _retry=False)
        if js.get("retCode") != 0:
            raise BybitError(js.get("retCode", -1), js.get("retMsg", ""), path)
        return js.get("result", {})

    # ---------- аккаунт ----------
    def get_positions(self, category: str, settle_coin: str) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            res = self._request("GET", "/v5/position/list", {
                "category": category, "settleCoin": settle_coin,
                "limit": 200, "cursor": cursor,
            })
            out.extend(p for p in res.get("list", []) if float(p.get("size") or 0) > 0)
            cursor = res.get("nextPageCursor")
            if not cursor:
                return out

    def get_equity(self, account_type: str) -> float:
        res = self._request("GET", "/v5/account/wallet-balance", {"accountType": account_type})
        acc = res["list"][0]
        return float(acc.get("totalEquity") or 0)

    def get_wallet_balance(self, account_type: str) -> float:
        """Баланс счёта БЕЗ нереализованного PnL (equity − UPL). Он не прыгает вместе с ценой, в отличие от equity."""
        acc = self._request("GET", "/v5/account/wallet-balance", {"accountType": account_type})["list"][0]
        return float(acc.get("totalEquity") or 0) - float(acc.get("totalPerpUPL") or 0)

    def get_transaction_log(self, account_type: str, category: str | None, currency: str,
                            start_ms: int, end_ms: int) -> list[dict]:
        """Записи лога транзакций UTA за интервал (≤7 дней), все страницы."""
        out: list[dict] = []
        cursor = None
        while True:
            res = self._request("GET", "/v5/account/transaction-log", {
                "accountType": account_type, "category": category, "currency": currency,
                "startTime": start_ms, "endTime": end_ms, "limit": 50, "cursor": cursor,
            })
            out.extend(res.get("list", []))
            cursor = res.get("nextPageCursor")
            if not cursor:
                return out

    def get_closed_pnl(self, category: str, start_ms: int, end_ms: int) -> list[dict]:
        """Закрытые позиции (closed PnL) за интервал — для диагностики."""
        out: list[dict] = []
        cursor = None
        while True:
            res = self._request("GET", "/v5/position/closed-pnl", {
                "category": category, "startTime": start_ms, "endTime": end_ms, "limit": 100, "cursor": cursor})
            out.extend(res.get("list", []))
            cursor = res.get("nextPageCursor")
            if not cursor:
                return out

    def get_fee_rates(self, category: str, symbol: str) -> tuple[float, float]:
        """(maker, taker) для символа с учётом VIP-уровня аккаунта."""
        res = self._request("GET", "/v5/account/fee-rate", {"category": category, "symbol": symbol})
        row = res["list"][0]
        return float(row["makerFeeRate"]), float(row["takerFeeRate"])

    # ---------- рынок ----------
    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> dict:
        res = self._request("GET", "/v5/market/orderbook",
                            {"category": category, "symbol": symbol, "limit": limit}, auth=False)
        return {
            "bids": [(float(p), float(q)) for p, q in res.get("b", [])],
            "asks": [(float(p), float(q)) for p, q in res.get("a", [])],
        }

    def get_instrument(self, category: str, symbol: str) -> dict:
        res = self._request("GET", "/v5/market/instruments-info",
                            {"category": category, "symbol": symbol}, auth=False)
        row = res["list"][0]
        return {
            "min_qty": float(row["lotSizeFilter"]["minOrderQty"]),
            "qty_step": row["lotSizeFilter"]["qtyStep"],  # строкой — для Decimal
            "tick_size": row["priceFilter"]["tickSize"],
            "min_notional": float(row["lotSizeFilter"].get("minNotionalValue") or 5),
        }

    # ---------- ордера ----------
    def place_limit_order(self, category: str, symbol: str, side: str, qty: str, price: str,
                          position_idx: int, link_id: str, reduce_only: bool) -> str:
        """Post-only лимитка: всегда мейкер. reduce_only=True — не может открыть/перевернуть позицию."""
        body = {
            "category": category, "symbol": symbol, "side": side,
            "orderType": "Limit", "qty": qty, "price": price,
            "timeInForce": "PostOnly", "positionIdx": position_idx, "orderLinkId": link_id,
        }
        if reduce_only:
            body["reduceOnly"] = True
        return self._request("POST", "/v5/order/create", body=body)["orderId"]

    def place_limit_close(self, category: str, symbol: str, side: str, qty: str,
                          price: str, position_idx: int, link_id: str) -> str:
        """Post-only reduce-only лимитка: гарантированно мейкер и не может открыть позицию."""
        return self.place_limit_order(category, symbol, side, qty, price, position_idx, link_id, True)

    def set_leverage(self, category: str, symbol: str, leverage: float) -> None:
        lev = format(leverage, "g")
        try:
            self._request("POST", "/v5/position/set-leverage", body={
                "category": category, "symbol": symbol, "buyLeverage": lev, "sellLeverage": lev})
        except BybitError as e:
            if e.code != LEVERAGE_NOT_MODIFIED:
                raise

    def get_margin_mode(self) -> str:
        """REGULAR_MARGIN (кросс) | ISOLATED_MARGIN | PORTFOLIO_MARGIN."""
        return self._request("GET", "/v5/account/info").get("marginMode", "")

    def get_open_orders(self, category: str, settle_coin: str) -> list[dict]:
        """Все активные ордера по аккаунту (одним запросом, все страницы)."""
        out: list[dict] = []
        cursor = None
        while True:
            res = self._request("GET", "/v5/order/realtime", {
                "category": category, "settleCoin": settle_coin, "openOnly": 0, "limit": 50, "cursor": cursor})
            out.extend(res.get("list", []))
            cursor = res.get("nextPageCursor")
            if not cursor:
                return out

    def amend_order(self, category: str, symbol: str, order_id: str, price: str, qty: str) -> None:
        """Изменить цену/объём живого ордера (одним запросом, без потери ордера при ошибке)."""
        self._request("POST", "/v5/order/amend", body={
            "category": category, "symbol": symbol, "orderId": order_id, "price": price, "qty": qty})

    def cancel_order(self, category: str, symbol: str, order_id: str) -> None:
        try:
            self._request("POST", "/v5/order/cancel",
                          body={"category": category, "symbol": symbol, "orderId": order_id})
        except BybitError as e:
            if e.code not in ORDER_GONE_CODES:
                raise

    def get_order(self, category: str, symbol: str, order_id: str) -> dict | None:
        res = self._request("GET", "/v5/order/realtime", {
            "category": category, "symbol": symbol, "orderId": order_id, "openOnly": 0,
        })
        rows = res.get("list", [])
        return rows[0] if rows else None
