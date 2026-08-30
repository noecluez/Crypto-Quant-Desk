"""Signed Bybit V5 REST client -- the ONLY module in this app that talks to
Bybit with credentials or places/modifies/cancels anything. Every other
module (feeds/bybit_feed.py, analysis/*) only ever reads free, keyless,
public market data.

Auth scheme (Bybit V5, confirmed against bybit-exchange.github.io/docs/v5
2026-08-30):
    pre-sign string = timestamp + api_key + recv_window + payload
        GET:  payload = query string, no leading "?"
        POST: payload = the exact JSON body bytes that get sent, compact
              (`separators=(",", ":")`) -- the signature will not match if
              the body sent differs at all (key order, spacing) from the
              body signed, so we build the JSON string once and reuse it.
    signature = HMAC-SHA256(api_secret, pre_sign_string) as lowercase hex.
    headers: X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN, X-BAPI-SIGN-TYPE=2,
             X-BAPI-RECV-WINDOW.

Every response is checked for Bybit's own `retCode` (0 == success) -- a
200 HTTP status from Bybit only means "we understood your request", not
"it worked". A non-zero retCode (bad qty, insufficient margin, symbol not
found, etc.) raises BybitAPIError with the code and message intact so the
caller can decide what to do, rather than silently treating a rejected
order as a success.

Nothing here retries automatically. An order is not a GET request -- a
naive retry on a timeout could double-submit a market order, which is a
strictly worse failure mode than surfacing the error and doing nothing.
Callers (execution/order_manager.py) decide what, if anything, is safe to
retry (e.g. a read-only reconciliation fetch is safe to retry; POST
/v5/order/create is not).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("bybit_client")

RECV_WINDOW_MS = "5000"


class BybitAPIError(RuntimeError):
    def __init__(self, ret_code: int, ret_msg: str, endpoint: str):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        super().__init__(f"Bybit API error on {endpoint}: retCode={ret_code} retMsg={ret_msg!r}")


@dataclass
class BybitClient:
    api_key: str
    api_secret: str
    base_url: str  # config.bybit_rest_base -- testnet or mainnet
    timeout: float = 10.0

    def _headers(self, payload: str, timestamp: str) -> dict:
        pre_sign = timestamp + self.api_key + RECV_WINDOW_MS + payload
        signature = hmac.new(
            self.api_secret.encode("utf-8"), pre_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-RECV-WINDOW": RECV_WINDOW_MS,
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        # Bybit signs the query string in whatever order it's sent, and
        # requests preserves dict insertion order when building the query
        # string itself -- but to make the signed string and the sent string
        # provably identical we build it once and pass it as the raw path.
        query = "&".join(f"{k}={v}" for k, v in params.items())
        timestamp = str(int(time.time() * 1000))
        headers = self._headers(query, timestamp)
        url = f"{self.base_url}{endpoint}" + (f"?{query}" if query else "")
        resp = requests.get(url, headers=headers, timeout=self.timeout)
        return self._unwrap(resp, endpoint)

    def _post(self, endpoint: str, body: dict) -> dict:
        payload = json.dumps(body, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        headers = self._headers(payload, timestamp)
        resp = requests.post(f"{self.base_url}{endpoint}", headers=headers, data=payload, timeout=self.timeout)
        return self._unwrap(resp, endpoint)

    @staticmethod
    def _unwrap(resp: requests.Response, endpoint: str) -> dict:
        resp.raise_for_status()  # transport-level failure (DNS, 5xx, etc.)
        data = resp.json()
        ret_code = data.get("retCode")
        if ret_code != 0:
            raise BybitAPIError(ret_code, data.get("retMsg", ""), endpoint)
        return data.get("result", {})

    # -----------------------------------------------------------------
    # Account
    # -----------------------------------------------------------------

    def wallet_balance(self, account_type: str = "UNIFIED", coin: str = "USDT") -> dict:
        """Returns the raw list entry for `coin`'s wallet, including
        `equity`, `availableToWithdraw`, `walletBalance`. Callers should use
        `equity` for risk-sizing math (it reflects unrealized P&L on open
        positions; `walletBalance` does not)."""
        result = self._get("/v5/account/wallet-balance", {"accountType": account_type})
        accounts = result.get("list", [])
        if not accounts:
            raise BybitAPIError(-1, "empty wallet-balance response", "/v5/account/wallet-balance")
        for c in accounts[0].get("coin", []):
            if c.get("coin") == coin:
                return c
        raise BybitAPIError(-1, f"coin {coin} not found in wallet-balance response", "/v5/account/wallet-balance")

    def equity_usdt(self) -> float:
        row = self.wallet_balance()
        val = row.get("equity") or row.get("walletBalance")
        if val in (None, ""):
            raise BybitAPIError(-1, "no equity/walletBalance field present", "/v5/account/wallet-balance")
        return float(val)

    # -----------------------------------------------------------------
    # Instrument metadata (qty/price precision, leverage bounds)
    # -----------------------------------------------------------------

    def instrument_info(self, symbol: str, category: str = "linear") -> dict:
        result = self._get("/v5/market/instruments-info", {"category": category, "symbol": symbol})
        rows = result.get("list", [])
        if not rows:
            raise BybitAPIError(-1, f"no instrument info for {symbol}", "/v5/market/instruments-info")
        return rows[0]

    # -----------------------------------------------------------------
    # Positions
    # -----------------------------------------------------------------

    def positions(self, category: str = "linear", symbol: str | None = None) -> list[dict]:
        result = self._get("/v5/position/list", {"category": category, "symbol": symbol, "settleCoin": "USDT" if symbol is None else None})
        return result.get("list", [])

    def set_leverage(self, symbol: str, leverage: float, category: str = "linear") -> None:
        lev = str(leverage)
        try:
            self._post("/v5/position/set-leverage", {
                "category": category, "symbol": symbol,
                "buyLeverage": lev, "sellLeverage": lev,
            })
        except BybitAPIError as exc:
            # 110043 == "leverage not modified" -- already at this leverage,
            # not an error worth surfacing.
            if exc.ret_code == 110043:
                return
            raise

    def switch_isolated_margin(self, symbol: str, leverage: float, category: str = "linear") -> None:
        lev = str(leverage)
        try:
            self._post("/v5/position/switch-isolated", {
                "category": category, "symbol": symbol,
                "tradeMode": 1,  # 1 = isolated, 0 = cross
                "buyLeverage": lev, "sellLeverage": lev,
            })
        except BybitAPIError as exc:
            # 110026 == "cross/isolated margin mode is not modified" -- already
            # isolated, not an error worth surfacing.
            if exc.ret_code == 110026:
                return
            raise

    # -----------------------------------------------------------------
    # Orders
    # -----------------------------------------------------------------

    def place_market_order(
        self, *, symbol: str, side: str, qty: str, category: str = "linear",
        stop_loss: str | None = None, take_profit: str | None = None,
        reduce_only: bool = False, position_idx: int = 0,
    ) -> dict:
        """`side` is "Buy" or "Sell". `qty` and `stop_loss`/`take_profit`
        must already be pre-formatted to the instrument's qty/price step
        (see execution/order_manager.py's rounding helpers) -- Bybit rejects
        a qty/price with excess precision rather than rounding it for you."""
        body = {
            "category": category, "symbol": symbol, "side": side,
            "orderType": "Market", "qty": qty, "timeInForce": "IOC",
            "positionIdx": position_idx, "reduceOnly": reduce_only,
        }
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
            body["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            body["takeProfit"] = take_profit
            body["tpTriggerBy"] = "MarkPrice"
        if stop_loss is not None or take_profit is not None:
            body["tpslMode"] = "Full"
        return self._post("/v5/order/create", body)

    def set_trading_stop(
        self, *, symbol: str, category: str = "linear", position_idx: int = 0,
        stop_loss: str | None = None, take_profit: str | None = None,
        trailing_stop: str | None = None, active_price: str | None = None,
    ) -> dict:
        """Modifies SL/TP/trailing-stop on an OPEN position. Pass "0" for
        any of stop_loss/take_profit/trailing_stop to cancel it.

        Deliberately never called with both a non-zero stop_loss and a
        non-zero trailing_stop in the same request from this codebase (see
        order_manager.py's handoff logic) -- Bybit's own docs don't fully
        specify which one wins if both are live simultaneously, so we avoid
        the ambiguity entirely by treating trailing-stop activation as an
        explicit hand-off: cancel the fixed stop, then set the trailing one,
        as two sequential calls, so there is a well-defined single active
        stop at all times (and if the process dies between the two calls,
        the fixed stop -- being set first and cancelled second -- is what's
        left protecting the position, never nothing)."""
        body = {"category": category, "symbol": symbol, "positionIdx": position_idx, "tpslMode": "Full"}
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
            body["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            body["takeProfit"] = take_profit
            body["tpTriggerBy"] = "MarkPrice"
        if trailing_stop is not None:
            body["trailingStop"] = trailing_stop
        if active_price is not None:
            body["activePrice"] = active_price
        return self._post("/v5/position/trading-stop", body)

    def close_position_market(self, *, symbol: str, side_to_close: str, qty: str, category: str = "linear", position_idx: int = 0) -> dict:
        """`side_to_close` is the side of the OPEN position ("Buy"/"Sell") --
        this places the opposite side, reduce-only, to flatten it."""
        closing_side = "Sell" if side_to_close == "Buy" else "Buy"
        return self.place_market_order(
            symbol=symbol, side=closing_side, qty=qty, category=category,
            reduce_only=True, position_idx=position_idx,
        )
