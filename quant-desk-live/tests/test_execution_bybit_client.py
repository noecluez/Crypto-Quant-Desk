"""Tests for execution/bybit_client.py -- the HMAC signing scheme, since
getting this wrong means every single authenticated call fails (or worse,
silently signs the wrong thing). No real network calls; requests.get/post
are monkeypatched with fake responses matching Bybit's V5 response shape.
"""
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.bybit_client import BybitClient, BybitAPIError


def make_client():
    return BybitClient(api_key="testkey", api_secret="testsecret", base_url="https://api-testnet.bybit.com")


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = json.dumps(self._json)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_get_signature_matches_manual_computation(monkeypatch):
    client = make_client()
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeResponse(200, {"retCode": 0, "retMsg": "OK", "result": {"ok": True}})

    monkeypatch.setattr("execution.bybit_client.requests.get", fake_get)
    client._get("/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"})

    headers = captured["headers"]
    timestamp = headers["X-BAPI-TIMESTAMP"]
    query = "category=linear&symbol=BTCUSDT"
    expected_pre_sign = timestamp + "testkey" + "5000" + query
    expected_sig = hmac.new(b"testsecret", expected_pre_sign.encode(), hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected_sig
    assert headers["X-BAPI-API-KEY"] == "testkey"
    assert query in captured["url"]


def test_post_signature_matches_manual_computation(monkeypatch):
    client = make_client()
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["data"] = data
        captured["headers"] = headers
        return FakeResponse(200, {"retCode": 0, "retMsg": "OK", "result": {"orderId": "abc123"}})

    monkeypatch.setattr("execution.bybit_client.requests.post", fake_post)
    result = client.place_market_order(symbol="BTCUSDT", side="Buy", qty="0.01", stop_loss="60000", take_profit="70000")

    headers = captured["headers"]
    timestamp = headers["X-BAPI-TIMESTAMP"]
    body = captured["data"]
    expected_pre_sign = timestamp + "testkey" + "5000" + body
    expected_sig = hmac.new(b"testsecret", expected_pre_sign.encode(), hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected_sig
    # Body must be compact JSON (no spaces) -- signature won't match Bybit's
    # own recomputation otherwise.
    assert ", " not in body and ": " not in body
    assert result["orderId"] == "abc123"


def test_non_zero_retcode_raises_bybit_api_error(monkeypatch):
    client = make_client()

    def fake_post(url, headers=None, data=None, timeout=None):
        return FakeResponse(200, {"retCode": 110007, "retMsg": "insufficient balance", "result": {}})

    monkeypatch.setattr("execution.bybit_client.requests.post", fake_post)
    try:
        client.place_market_order(symbol="BTCUSDT", side="Buy", qty="0.01")
        assert False, "expected BybitAPIError"
    except BybitAPIError as exc:
        assert exc.ret_code == 110007
        assert "insufficient balance" in exc.ret_msg


def test_set_leverage_swallows_already_set_error(monkeypatch):
    client = make_client()

    def fake_post(url, headers=None, data=None, timeout=None):
        return FakeResponse(200, {"retCode": 110043, "retMsg": "leverage not modified", "result": {}})

    monkeypatch.setattr("execution.bybit_client.requests.post", fake_post)
    client.set_leverage("BTCUSDT", 10)  # must not raise


def test_set_leverage_reraises_other_errors(monkeypatch):
    client = make_client()

    def fake_post(url, headers=None, data=None, timeout=None):
        return FakeResponse(200, {"retCode": 10001, "retMsg": "some other error", "result": {}})

    monkeypatch.setattr("execution.bybit_client.requests.post", fake_post)
    try:
        client.set_leverage("BTCUSDT", 10)
        assert False, "expected BybitAPIError to propagate"
    except BybitAPIError as exc:
        assert exc.ret_code == 10001


def test_set_trading_stop_never_sends_stop_loss_and_trailing_together():
    """Regression guard for the deliberate hand-off design (see
    BybitClient.set_trading_stop's docstring): this codebase must never call
    it with both a non-"0" stop_loss and a non-"0" trailing_stop in the same
    request. Scans order_manager.py's source for the two call sites and
    confirms neither passes both params in one call."""
    import inspect
    from execution import order_manager
    source = inspect.getsource(order_manager)
    # crude but effective: every set_trading_stop( call block up to its
    # closing paren must not contain both "stop_loss=" and "trailing_stop="
    # (excluding the docstring/comments, which don't contain the call).
    import re
    calls = re.findall(r"set_trading_stop\((.*?)\)\n", source, re.S)
    for call in calls:
        has_sl = "stop_loss=" in call
        has_trailing = "trailing_stop=" in call
        assert not (has_sl and has_trailing), f"found a set_trading_stop call with both params: {call}"
