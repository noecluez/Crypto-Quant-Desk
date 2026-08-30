"""Crypto feed: Bybit public REST (universe scan + multi-timeframe history)
+ public WebSocket (live ticks). Zero signup, zero API key needed for any of
it -- market data on Bybit's spot API is fully public.

Two speeds of work happen here:
  - FAST (every WS tick, ~50ms cadence per symbol): price, 24h change, the
    evolving "today" daily bar's RSI/SMA/52w range, and the heat score /
    alert check that combines all of that with the cached deep-analysis
    fields below. This has to be cheap -- it runs constantly.
  - SLOW (every DEEP_REFRESH_SECONDS, default 5 min): re-fetch 15m/1h/4h/1D
    candle history for every deep-watchlist symbol, recompute the full
    indicator suite (MACD, Bollinger, Stochastic, ATR, VWAP) per timeframe,
    multi-timeframe confluence, RSI/price divergence, fractal support/
    resistance, Fibonacci retracement, and the aggregated signal backtest
    library. This is genuinely more expensive (swing detection, backtesting
    a basket of symbols), so it's decoupled from the tick path entirely and
    just caches its results onto SymbolState for the fast path to read.

There's also a periodic (default 30 min) scan of the wider Bybit spot
market to find which non-pinned pairs are worth adding to the deep
watchlist -- see discover_universe()/pick_deep_watchlist().
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import websockets

from analysis.indicators import (
    rsi, rsi_series, sma, score_setup, pct, fmt_price,
    macd, bollinger, stochastic, atr, vwap_anchored_daily,
    detect_divergence, find_swing_levels, cluster_levels, fibonacci_levels,
    timeframe_bias, confluence,
)
from analysis.backtest import find_signal_occurrences, aggregate_signal_stats
from analysis.spotlight import interpret_spotlight, ENTRY_TFS, MICRO_TFS, MACRO_TFS
from analysis.derivatives import (
    interpret_funding, interpret_open_interest, interpret_long_short_ratio,
    summarize_liquidations, positioning_read,
)
from analysis.costs import summarize_costs
from alerts.whatsapp import maybe_alert
from config import config
from state import state

log = logging.getLogger("bybit")

REST_KLINE = "https://api.bybit.com/v5/market/kline"
REST_TICKERS = "https://api.bybit.com/v5/market/tickers"
REST_FUNDING = "https://api.bybit.com/v5/market/funding/history"
REST_OPEN_INTEREST = "https://api.bybit.com/v5/market/open-interest"
REST_ACCOUNT_RATIO = "https://api.bybit.com/v5/market/account-ratio"

# Which market we analyze. "linear" (USDT perpetuals) is the default because
# that's where leverage -- and therefore funding, open interest, liquidations
# and the long/short account ratio -- actually exists; spot has none of them.
# Everything here is still public, keyless, read-only market data either way.
CATEGORY = config.BYBIT_CATEGORY if config.BYBIT_CATEGORY in ("linear", "spot", "inverse") else "linear"
WS_URL = f"wss://stream.bybit.com/v5/public/{CATEGORY}"
MAX_TOPICS_PER_SUBSCRIBE = 10  # Bybit's per-subscribe topic limit

TIMEFRAME_INTERVALS = {"15m": "15", "1h": "60", "4h": "240", "1D": "D"}
TIMEFRAME_LIMITS = {"15m": 200, "1h": 200, "4h": 200, "1D": 260}

# --- Spotlight: one symbol at a time, ultra-frequent, much finer timeframe ---
# ladder than the rest of the desk -- built for actively trading a single
# pair on low timeframes with leverage, where you need to act fast.
SPOTLIGHT_NATIVE_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "12h": "720",
}  # Bybit has no native 10-minute kline interval -- "10m" is synthesized by
   # resampling the 5m series two candles at a time, see _resample_10m().
SPOTLIGHT_LIMITS = {"1m": 300, "3m": 300, "5m": 400, "15m": 300, "30m": 300, "1h": 300, "4h": 300, "12h": 300}
SPOTLIGHT_TF_ORDER = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "12h"]
# Same "longer timeframes carry more weight" principle as the rest of the
# desk's DEFAULT_TIMEFRAME_WEIGHTS, just extended across 9 timeframes
# instead of 4 -- fine enough that a 1m blip doesn't dominate the overall
# read, but never zero, since for a low-timeframe leveraged trade the fast
# timeframes are exactly what's being acted on.
SPOTLIGHT_TIMEFRAME_WEIGHTS = {
    "1m": 0.3, "3m": 0.4, "5m": 0.6, "10m": 0.8, "15m": 1.0,
    "30m": 1.3, "1h": 1.8, "4h": 2.5, "12h": 3.2,
}

# Spot leveraged tokens (e.g. BTC3LUSDT) and stablecoin-vs-stablecoin pairs
# are never an "interesting mover" -- filter them out of universe scanning.
_LEVERAGED_RE = re.compile(r"(2|3|4|5)(L|S)USDT$")
_STABLE_BASES = {"USDC", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDP", "EUR", "GBP", "AEUR", "USDT"}

_history: dict[str, dict[str, pd.DataFrame]] = {}  # symbol -> timeframe -> OHLCV df, oldest-first
_deep_symbols: list[str] = list(dict.fromkeys(config.CRYPTO_SYMBOLS))  # current deep-analysis watchlist
# Symbols the user opened a paper position on that AREN'T already in the deep
# watchlist -- they still need live price ticks for P&L, but not full
# multi-timeframe analysis. Once added here a symbol stays subscribed for
# the rest of the process's life (harmless — one extra ticker topic — and
# simpler than tracking per-position subscription refcounts).
_extra_position_symbols: set[str] = set()
_resubscribe_needed = asyncio.Event()

# The current Spotlight symbol (None = no Spotlight active) and its own
# candle history, kept entirely separate from `_history` above since it's
# a different timeframe ladder (1m-12h vs. 15m-1D) refreshed on a much
# tighter cadence (SPOTLIGHT_REFRESH_SECONDS, default 3 min, vs. DEEP_
# REFRESH_SECONDS, default 5 min) -- see set_spotlight()/spotlight_loop().
_spotlight_symbol: str | None = None
_spotlight_history: dict[str, "pd.DataFrame"] = {}

# Live liquidation events per symbol, newest appended. Fed by the
# allLiquidation WS topic (derivatives only), trimmed on write, and
# summarized on read -- see _record_liquidation()/_recompute_positioning().
_liquidations: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# REST fetching
# ---------------------------------------------------------------------------

def _fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    resp = requests.get(
        REST_KLINE,
        params={"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
    rows = payload["result"]["list"]  # newest first, per Bybit docs
    if not rows:
        raise RuntimeError(f"Bybit returned no candles for {symbol}/{interval} (check the symbol is a valid spot pair)")
    rows = list(reversed(rows))  # -> oldest first, what our indicators expect
    df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close", "volume", "turnover"])
    for col in ["start", "open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df[["start", "open", "high", "low", "close", "volume"]]


def discover_universe() -> list[dict]:
    """One REST call gets every Bybit spot ticker at once (price, 24h %
    change, 24h turnover) -- cheap enough to do market-wide without a REST
    call per symbol. Returns the filtered, liquid, non-leveraged USDT pairs."""
    resp = requests.get(REST_TICKERS, params={"category": CATEGORY}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
    out = []
    for r in payload["result"]["list"]:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT") or _LEVERAGED_RE.search(sym):
            continue
        base = sym[:-4]
        if base in _STABLE_BASES:
            continue
        try:
            turnover = float(r.get("turnover24h") or 0)
            pct24h = float(r.get("price24hPcnt") or 0) * 100
        except (TypeError, ValueError):
            continue
        if turnover < config.UNIVERSE_MIN_TURNOVER_USDT:
            continue
        out.append({"symbol": sym, "turnover24h": turnover, "pct24h": pct24h})
    return out


def pick_deep_watchlist(universe: list[dict]) -> list[str]:
    """Your pinned CRYPTO_SYMBOLS always make the cut. The remaining slots
    (up to DEEP_WATCHLIST_SIZE total) go to the biggest 24h movers in the
    scanned universe that aren't already pinned -- "most interesting pairs
    for the day" in the plainest sense: the ones actually moving, with real
    liquidity behind the move."""
    pinned = list(dict.fromkeys(config.CRYPTO_SYMBOLS))
    slots = max(config.DEEP_WATCHLIST_SIZE - len(pinned), 0)
    pinned_set = set(pinned)
    candidates = [u for u in universe if u["symbol"] not in pinned_set]
    candidates.sort(key=lambda u: abs(u["pct24h"]), reverse=True)
    discovered = [u["symbol"] for u in candidates[:slots]]
    return pinned + discovered


def fetch_last_price(symbol: str) -> float | None:
    """One-off REST lookup for a single symbol's current price -- used when
    the user opens a paper position on a ticker that isn't already streaming
    (i.e. outside the deep watchlist), so there's an entry price to record
    immediately rather than waiting for a subscription to catch up. Returns
    None (never raises) on any failure -- the caller treats that as "not a
    valid/tradeable Bybit spot symbol right now" and reports it to the user."""
    try:
        resp = requests.get(REST_TICKERS, params={"category": CATEGORY, "symbol": symbol}, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            return None
        rows = payload["result"]["list"]
        if not rows:
            return None
        price = float(rows[0]["lastPrice"])
        return price if price > 0 else None
    except Exception as exc:
        log.warning("fetch_last_price(%s) failed: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Derivatives positioning data (linear/inverse only -- spot has none of this)
# ---------------------------------------------------------------------------

def fetch_funding_history(symbol: str, limit: int = 24) -> list[float]:
    """Recent funding rates, oldest-first. Free and keyless. Returns [] on
    any failure or on spot (where funding doesn't exist) -- never raises,
    since positioning data is a bonus layer and must never break the feed."""
    if not config.is_derivatives:
        return []
    try:
        resp = requests.get(
            REST_FUNDING,
            params={"category": CATEGORY, "symbol": symbol, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            return []
        rows = payload["result"]["list"]  # newest first
        return [float(r["fundingRate"]) for r in reversed(rows)]
    except Exception as exc:
        log.warning("fetch_funding_history(%s) failed: %s", symbol, exc)
        return []


def fetch_open_interest(symbol: str, interval: str = "5min", limit: int = 24) -> list[dict]:
    """Open-interest history, oldest-first, as [{ts, oi}, ...]. Free and
    keyless. Same never-raises contract as funding above."""
    if not config.is_derivatives:
        return []
    try:
        resp = requests.get(
            REST_OPEN_INTEREST,
            params={"category": CATEGORY, "symbol": symbol, "intervalTime": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            return []
        rows = payload["result"]["list"]  # newest first
        return [{"ts": int(r["timestamp"]) / 1000, "oi": float(r["openInterest"])} for r in reversed(rows)]
    except Exception as exc:
        log.warning("fetch_open_interest(%s) failed: %s", symbol, exc)
        return []


def fetch_long_short_ratio(symbol: str, period: str = "1h", limit: int = 1) -> tuple[float | None, float | None]:
    """Fraction of accounts long vs. short. Free and keyless. Returns
    (None, None) rather than raising if unavailable."""
    if not config.is_derivatives:
        return None, None
    try:
        resp = requests.get(
            REST_ACCOUNT_RATIO,
            params={"category": CATEGORY, "symbol": symbol, "period": period, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            return None, None
        rows = payload["result"]["list"]
        if not rows:
            return None, None
        return float(rows[0]["buyRatio"]), float(rows[0]["sellRatio"])
    except Exception as exc:
        log.warning("fetch_long_short_ratio(%s) failed: %s", symbol, exc)
        return None, None


def _refresh_derivatives_symbol(symbol: str) -> None:
    """Pull funding / OI / long-short ratio for one symbol and fold them into
    a single positioning read cached on SymbolState. Liquidations are NOT
    fetched here -- they arrive live over the WebSocket (see the
    allLiquidation topic handling in run()) and are summarized on read."""
    st = state.symbols.get(symbol)
    if st is None or not config.is_derivatives:
        return

    funding_history = fetch_funding_history(symbol)
    latest_funding = funding_history[-1] if funding_history else None
    oi_history = fetch_open_interest(symbol)
    buy_ratio, sell_ratio = fetch_long_short_ratio(symbol)

    st.funding_history = funding_history
    st.funding = interpret_funding(
        latest_funding,
        extreme_annual_pct=config.FUNDING_EXTREME_ANNUAL_PCT,
        elevated_annual_pct=config.FUNDING_ELEVATED_ANNUAL_PCT,
        history=funding_history,
    )

    # Compare the newest OI reading against the oldest one in the window, and
    # the price change over the same window -- the pairing is what makes the
    # four-quadrant read meaningful, so both legs must cover the same period.
    st.open_interest = None
    if len(oi_history) >= 2:
        oi_now, oi_prev = oi_history[-1]["oi"], oi_history[0]["oi"]
        price_change = _price_change_since(symbol, oi_history[0]["ts"])
        if price_change is None:
            price_change = st.change_24h_pct
        st.open_interest = interpret_open_interest(oi_now, oi_prev, price_change)

    st.long_short_ratio = interpret_long_short_ratio(buy_ratio, sell_ratio)
    st.derivatives_updated_at = time.time()
    _recompute_positioning(st)


def _price_change_since(symbol: str, since_ts: float) -> float | None:
    """% price change from the closest available historical point to now,
    using whichever candle series we already have in memory -- no extra REST
    call just to date-align open interest."""
    st = state.symbols.get(symbol)
    if st is None or st.price <= 0:
        return None
    source = _spotlight_history if symbol == _spotlight_symbol else _history.get(symbol, {})
    for tf in ("5m", "15m", "1h"):
        df = source.get(tf)
        if df is None or df.empty:
            continue
        older = df[df["start"] / 1000 <= since_ts]
        if older.empty:
            continue
        return pct(st.price, float(older["close"].iloc[-1]))
    return None


def _recompute_positioning(st) -> None:
    """Refresh the combined positioning read. Called both after a slow
    derivatives refresh and whenever new liquidations arrive, since a
    cascade can change the read within seconds."""
    st.liquidations = summarize_liquidations(
        _liquidations.get(st.symbol, []),
        window_minutes=config.LIQUIDATION_WINDOW_MINUTES,
        cascade_usd=config.LIQUIDATION_CASCADE_USD,
    )
    st.positioning = positioning_read(st.funding, st.open_interest, st.long_short_ratio, st.liquidations)


def _record_liquidation(symbol: str, side: str, price: float, qty: float) -> None:
    """Bybit's liquidation feed reports the side of the *closing order*, so
    a "Buy" liquidation order means a SHORT position was force-closed and a
    "Sell" means a LONG was. That inversion flips the entire interpretation,
    so it's resolved here, once, on ingest."""
    liquidated_side = "short" if side.lower() == "buy" else "long"
    events = _liquidations.setdefault(symbol, [])
    events.append({
        "ts": time.time(),
        "side": side,
        "liquidated_side": liquidated_side,
        "price": price,
        "qty": qty,
        "usd": price * qty,
    })
    # Keep the window bounded: anything far older than the analysis window is
    # dead weight, and this list is appended to on every forced close.
    cutoff = time.time() - max(config.LIQUIDATION_WINDOW_MINUTES * 60 * 4, 3600)
    if len(events) > 500:
        _liquidations[symbol] = [e for e in events if e["ts"] >= cutoff][-500:]


async def _derivatives_refresh_loop() -> None:
    """Funding, OI and the crowd ratio move over hours, so they get their own
    slow loop rather than riding the Spotlight or tick cadence."""
    if not config.is_derivatives:
        log.info("Derivatives positioning disabled (BYBIT_CATEGORY=%s has no funding/OI/liquidations)", CATEGORY)
        return
    while True:
        try:
            symbols = list(dict.fromkeys(_deep_symbols + ([_spotlight_symbol] if _spotlight_symbol else [])))
            await asyncio.get_event_loop().run_in_executor(None, _refresh_derivatives_batch, symbols)
            await state.broadcast()
        except Exception:
            log.warning("Derivatives refresh failed (will retry next cycle)", exc_info=True)
        await asyncio.sleep(config.DERIVATIVES_REFRESH_SECONDS)


def _refresh_derivatives_batch(symbols: list[str]) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_refresh_derivatives_symbol, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                log.warning("Derivatives refresh failed for %s: %s", futures[fut], exc)


def track_extra_symbol(symbol: str, price: float) -> None:
    """Called right after a paper position opens on a symbol outside the
    deep watchlist. Seeds an initial price (so the position's unrealized
    P&L has something to show before the first live tick) and, if it's not
    already streaming, adds it to the WS subscription set and asks the feed
    loop to resubscribe -- same mechanism the universe rescan uses to pick
    up newly-discovered symbols."""
    st = state.ensure(symbol, "crypto")
    if st.price <= 0:
        st.push_price(price)
    if symbol not in _deep_symbols and symbol not in _extra_position_symbols:
        _extra_position_symbols.add(symbol)
        _resubscribe_needed.set()


# ---------------------------------------------------------------------------
# Spotlight: one symbol, 1m-12h, refreshed every SPOTLIGHT_REFRESH_SECONDS
# ---------------------------------------------------------------------------

def _resample_10m(df5m: pd.DataFrame) -> pd.DataFrame:
    """Bybit has no native 10-minute kline interval -- build one by
    resampling the 5-minute series two candles at a time, keyed off the
    actual timestamp (not just row-pairing) so it's still correct if a
    candle or two is missing from the 5m series."""
    if df5m is None or df5m.empty:
        return pd.DataFrame(columns=["start", "open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(df5m["start"], unit="ms", utc=True)
    ts.name = "start"
    work = df5m[["open", "high", "low", "close", "volume"]].set_index(ts)
    agg = work.resample("10min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open"]).reset_index()
    agg["start"] = agg["start"].astype("int64") // 10 ** 6  # back to ms epoch, matching _fetch_klines' shape
    return agg[["start", "open", "high", "low", "close", "volume"]]


def _fetch_spotlight_history(symbol: str) -> dict[str, pd.DataFrame]:
    """Fetches every native Spotlight timeframe concurrently, then derives
    the synthetic 10m series from the 5m data. A timeframe that individually
    fails to fetch is just missing from the result (handled per-timeframe
    downstream, same as the rest of the deep-refresh pipeline) -- only a
    total failure (nothing fetched at all) raises."""
    history: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_klines, symbol, interval, SPOTLIGHT_LIMITS[tf]): tf
            for tf, interval in SPOTLIGHT_NATIVE_INTERVALS.items()
        }
        for fut in as_completed(futures):
            tf = futures[fut]
            try:
                history[tf] = fut.result()
            except Exception as exc:
                log.warning("Spotlight fetch failed for %s %s: %s", symbol, tf, exc)
    if "5m" in history:
        history["10m"] = _resample_10m(history["5m"])
    if not history:
        raise RuntimeError(f"Couldn't fetch any candle data for {symbol}")
    return history


# A daily-anchored VWAP needs enough candles inside the current UTC day to
# mean anything. On a 12h frame that's at most two candles and on 4h at most
# six -- which isn't a VWAP, it's roughly the typical price wearing a VWAP
# label. The regular deep-refresh path already excluded 1D for this reason;
# these are the Spotlight frames coarse enough to deserve the same guard.
VWAP_TOO_COARSE_TFS = {"4h", "12h"}


def _spotlight_tf_summary(df: pd.DataFrame | None, tf: str | None = None) -> dict | None:
    """Full indicator suite for one Spotlight timeframe -- everything the
    regular deep-refresh computes for 1D (divergence, support/resistance,
    Fibonacci), plus MACD/Bollinger/Stochastic/ATR/VWAP, all per timeframe
    instead of just once. That's the "ultra in-depth" part: every one of
    the 9 timeframes gets the full picture, not just the coarse ones."""
    if df is None or len(df) < 30:
        return None
    closes, highs, lows = df["close"], df["high"], df["low"]
    m = macd(closes)
    b = bollinger(closes)
    s = stochastic(highs, lows, closes)
    a = atr(highs, lows, closes)
    vw = None if tf in VWAP_TOO_COARSE_TFS else vwap_anchored_daily(df)
    rsi_tf = rsi(closes)
    sma50_tf = sma(closes, 50)
    price_tf = float(closes.iloc[-1])
    divergence = detect_divergence(closes, rsi_series(closes)) if len(closes) >= 40 else None
    swing_highs, swing_lows = find_swing_levels(highs, lows)
    support, resistance = cluster_levels(swing_highs + swing_lows, price_tf)
    fib = fibonacci_levels(highs, lows)
    bias = timeframe_bias(
        price=price_tf, rsi14=rsi_tf, macd_hist=m["hist"] if m else None,
        sma50=sma50_tf, percent_b=b["percent_b"] if b else None,
    )
    return {
        "price": price_tf, "rsi14": rsi_tf, "macd": m, "bollinger": b, "stochastic": s,
        "atr": a, "vwap": vw, "sma50": sma50_tf, "divergence": divergence,
        "support": support, "resistance": resistance, "fibonacci": fib, "bias": bias,
    }


def _assemble_spotlight_payload(symbol: str, tf_summaries: dict[str, dict]) -> dict:
    """Builds the Spotlight payload from already-computed per-timeframe
    summaries. Split out from the fetch path so the live-kline handler can
    rebuild the payload after a fast candle closes without re-fetching
    anything over REST."""
    biases = {tf: s["bias"] for tf, s in tf_summaries.items() if s.get("bias")}

    def _group_confluence(tfs: list[str]) -> dict:
        sub = {t: biases[t] for t in tfs if t in biases}
        return confluence(sub, weights=SPOTLIGHT_TIMEFRAME_WEIGHTS)

    entry_confluence = _group_confluence(ENTRY_TFS)
    micro_confluence = _group_confluence(MICRO_TFS)
    macro_confluence = _group_confluence(MACRO_TFS)
    overall_confluence = confluence(biases, weights=SPOTLIGHT_TIMEFRAME_WEIGHTS)

    st = state.symbols.get(symbol)
    interpretation = interpret_spotlight(
        symbol=symbol, tf_summaries=tf_summaries,
        entry_confluence=entry_confluence, micro_confluence=micro_confluence,
        macro_confluence=macro_confluence, overall_confluence=overall_confluence,
        rsi_overbought=config.RSI_OVERBOUGHT, rsi_oversold=config.RSI_OVERSOLD,
        positioning=st.positioning if st else None,
        cost_pct=config.round_trip_cost_pct,
    )

    fallback_price = next((tf_summaries[t]["price"] for t in SPOTLIGHT_TF_ORDER if t in tf_summaries), None)
    live_price = st.price if st and st.price > 0 else fallback_price

    return {
        "symbol": symbol,
        "price": live_price,
        "timeframes": tf_summaries,
        "confluence": {
            "entry": entry_confluence, "micro": micro_confluence,
            "macro": macro_confluence, "overall": overall_confluence,
        },
        "interpretation": interpretation,
        "positioning": {
            "funding": st.funding if st else None,
            "open_interest": st.open_interest if st else None,
            "long_short_ratio": st.long_short_ratio if st else None,
            "liquidations": st.liquidations if st else None,
            "read": st.positioning if st else None,
            "available": config.is_derivatives,
        },
        "costs": summarize_costs(config.TAKER_FEE_PCT, config.SLIPPAGE_PCT, config.ASSUMED_LEVERAGE),
        "live_klines": sorted(LIVE_KLINE_TFS) if config.SPOTLIGHT_LIVE_KLINES else [],
        "updated_at": time.time(),
    }


def _spotlight_refresh_symbol(symbol: str) -> dict:
    """The Spotlight equivalent of _deep_refresh_symbol(), but for all 9
    timeframes at once plus the three-tier confluence (entry/micro/macro),
    the derivatives positioning read, and the plain-English interpretation
    (analysis/spotlight.py). Caches the result onto state.spotlight."""
    global _spotlight_history
    history = _fetch_spotlight_history(symbol)
    _spotlight_history = history

    tf_summaries: dict[str, dict] = {}
    for tf in SPOTLIGHT_TF_ORDER:
        summary = _spotlight_tf_summary(history.get(tf), tf)
        if summary is not None:
            tf_summaries[tf] = summary

    payload = _assemble_spotlight_payload(symbol, tf_summaries)
    state.spotlight = payload
    return payload


def set_spotlight(symbol: str) -> dict:
    """Validates the symbol against a real Bybit price, makes sure it's
    getting live ticks (same mechanism paper positions use for a symbol
    outside the deep watchlist), and runs an immediate full refresh so the
    UI has something to show right away instead of waiting up to
    SPOTLIGHT_REFRESH_SECONDS. Raises ValueError on an invalid/untradeable
    symbol -- server.py turns that into a 400."""
    global _spotlight_symbol
    price = fetch_last_price(symbol)
    if price is None:
        raise ValueError(
            f"Couldn't find a live Bybit {CATEGORY} price for \"{symbol}\" — check the symbol (e.g. BTCUSDT)."
        )
    track_extra_symbol(symbol, price)
    previous = _spotlight_symbol
    _spotlight_symbol = symbol
    state.spotlight_symbol = symbol
    state.spotlight = None  # clear the previous symbol's stale data while the fresh fetch runs
    # The live-kline topics are per-symbol, so switching Spotlight means
    # re-subscribing (drop the old symbol's fast frames, pick up the new
    # one's) -- same resubscribe mechanism the watchlist rotation uses.
    if config.SPOTLIGHT_LIVE_KLINES and previous != symbol:
        _resubscribe_needed.set()
    # Positioning data matters most on the symbol you're actively watching,
    # so pull it now rather than waiting for the slow loop's next tick.
    if config.is_derivatives:
        try:
            _refresh_derivatives_symbol(symbol)
        except Exception as exc:
            log.warning("Derivatives fetch failed for new spotlight %s: %s", symbol, exc)
    return _spotlight_refresh_symbol(symbol)


def clear_spotlight() -> None:
    global _spotlight_symbol, _spotlight_history
    had_symbol = _spotlight_symbol is not None
    _spotlight_symbol = None
    _spotlight_history = {}
    state.spotlight_symbol = None
    state.spotlight = None
    if config.SPOTLIGHT_LIVE_KLINES and had_symbol:
        _resubscribe_needed.set()  # drop the now-unused kline topics


async def spotlight_loop() -> None:
    """Runs for the process's lifetime, independent of the Bybit feed's own
    startup/reconnect cycle (this is plain REST, not the WebSocket) -- a
    cheap no-op sleep whenever no Spotlight symbol is set."""
    while True:
        await asyncio.sleep(config.SPOTLIGHT_REFRESH_SECONDS)
        symbol = _spotlight_symbol
        if not symbol:
            continue
        try:
            await asyncio.get_event_loop().run_in_executor(None, _spotlight_refresh_symbol, symbol)
            await state.broadcast()
        except Exception:
            log.warning("Spotlight refresh failed for %s (will retry next cycle)", symbol, exc_info=True)


# ---------------------------------------------------------------------------
# Seeding / deep refresh (multi-timeframe indicators, S/R, Fibonacci, confluence)
# ---------------------------------------------------------------------------

def _seed_symbol_all_timeframes(symbol: str) -> bool:
    ok_any = False
    for tf, interval in TIMEFRAME_INTERVALS.items():
        try:
            df = _fetch_klines(symbol, interval, TIMEFRAME_LIMITS[tf])
            _history.setdefault(symbol, {})[tf] = df
            ok_any = True
        except Exception as exc:
            log.warning("Failed to fetch %s %s candles: %s", symbol, tf, exc)
    return ok_any


def _deep_refresh_symbol(symbol: str) -> None:
    """Recomputes every derived-from-history field (per-timeframe
    indicators, confluence, divergence, support/resistance, Fibonacci) from
    whatever's currently in `_history[symbol]`. Does NOT touch price/RSI-of-
    the-evolving-today-bar/heat/alerts -- those are the fast tick path's job
    (see _recompute) so a 5-minute-old deep refresh never clobbers a
    50ms-fresh live price."""
    st = state.symbols.get(symbol)
    hist = _history.get(symbol)
    if st is None or not hist:
        return

    tf_summaries: dict[str, dict] = {}
    biases: dict[str, tuple[str, float]] = {}
    for tf in TIMEFRAME_INTERVALS:
        df = hist.get(tf)
        if df is None or len(df) < 30:
            continue
        closes, highs, lows = df["close"], df["high"], df["low"]
        m = macd(closes)
        b = bollinger(closes)
        s = stochastic(highs, lows, closes)
        a = atr(highs, lows, closes)
        vw = None if tf == "1D" else vwap_anchored_daily(df)
        rsi_tf = rsi(closes)
        sma50_tf = sma(closes, 50)
        price_tf = float(closes.iloc[-1])
        tf_summaries[tf] = {
            "price": price_tf, "rsi14": rsi_tf, "macd": m, "bollinger": b,
            "stochastic": s, "atr": a, "vwap": vw, "sma50": sma50_tf,
        }
        biases[tf] = timeframe_bias(
            price=price_tf, rsi14=rsi_tf,
            macd_hist=m["hist"] if m else None,
            sma50=sma50_tf, percent_b=b["percent_b"] if b else None,
        )

    st.timeframes = tf_summaries
    st.confluence = confluence(biases) if biases else None

    df1d = hist.get("1D")
    if df1d is not None and len(df1d) >= 40:
        rsis_1d = rsi_series(df1d["close"])
        st.divergence = detect_divergence(df1d["close"], rsis_1d)
        swing_highs, swing_lows = find_swing_levels(df1d["high"], df1d["low"])
        st.support, st.resistance = cluster_levels(swing_highs + swing_lows, float(df1d["close"].iloc[-1]))
        st.fibonacci = fibonacci_levels(df1d["high"], df1d["low"])
    else:
        st.divergence = None
        st.support, st.resistance = [], []
        st.fibonacci = None
    st.deep_updated_at = time.time()


def seed_deep_watchlist(symbols: list[str], is_initial: bool = False) -> int:
    """(Re)fetches multi-timeframe candle history for `symbols` and
    recomputes every derived indicator from it, concurrently (a thread pool
    since `requests` is blocking) so seeding ~20 symbols x 4 timeframes
    doesn't serialize into a minute-long startup. Safe to call repeatedly --
    on the very first call (`is_initial=True`) it also seeds a starting
    price/RSI/SMA/52w so a symbol's row isn't blank before its first live
    tick; later (periodic) calls leave those live-tick-owned fields alone."""
    ok_count = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_seed_symbol_all_timeframes, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                if not fut.result():
                    continue
                ok_count += 1
                st = state.ensure(sym, "crypto")
                st.tier = "pinned" if sym in config.CRYPTO_SYMBOLS else "discovered"
                if is_initial:
                    df1d = _history.get(sym, {}).get("1D")
                    if df1d is not None and not df1d.empty:
                        closes = df1d["close"]
                        st.rsi14 = rsi(closes)
                        st.sma50 = sma(closes, 50)
                        st.sma200 = sma(closes, 200)
                        st.high_52w = float(closes.tail(252).max())
                        st.low_52w = float(closes.tail(252).min())
                        if len(closes) >= 8:
                            st.change_7d_pct = pct(float(closes.iloc[-1]), float(closes.iloc[-8]))
                        avg_vol = float(df1d["volume"].tail(20).mean())
                        if avg_vol > 0:
                            st.vol_ratio = float(df1d["volume"].iloc[-1]) / avg_vol
                        st.push_price(float(closes.iloc[-1]))
                _deep_refresh_symbol(sym)
            except Exception as exc:
                log.warning("Seeding failed entirely for %s: %s", sym, exc)
    return ok_count


# ---------------------------------------------------------------------------
# Fast tick-path helpers
# ---------------------------------------------------------------------------

def _nearest_levels(levels: list[dict], live_price: float, near_pct: float = 1.5) -> tuple[dict | None, dict | None]:
    """Cheap, live re-derivation of "is price near a known S/R level right
    now" from the last deep refresh's cached level *prices* -- the
    expensive part (finding the levels) only happens every DEEP_REFRESH_
    SECONDS, but whether we're currently close to one of them is checked
    fresh on every tick since price moves constantly between refreshes."""
    support, resistance = None, None
    best_sup_dist, best_res_dist = None, None
    for lvl in levels:
        price = lvl["price"]
        dist = pct(price, live_price)
        if price <= live_price:
            if best_sup_dist is None or dist > best_sup_dist:
                best_sup_dist, support = dist, {**lvl, "distance_pct": dist}
        else:
            if best_res_dist is None or dist < best_res_dist:
                best_res_dist, resistance = dist, {**lvl, "distance_pct": dist}
    near_support = support if support and abs(support["distance_pct"]) <= near_pct else None
    near_resistance = resistance if resistance and abs(resistance["distance_pct"]) <= near_pct else None
    return near_support, near_resistance


def _recompute(symbol: str) -> None:
    st = state.symbols.get(symbol)
    df = _history.get(symbol, {}).get("1D")
    if st is None or df is None or st.price <= 0:
        return
    closes = pd.concat([df["close"], pd.Series([st.price])], ignore_index=True)
    st.rsi14 = rsi(closes)
    st.sma50 = sma(closes, 50)
    st.sma200 = sma(closes, 200)
    hist_high = float(df["close"].tail(252).max())
    hist_low = float(df["close"].tail(252).min())
    st.high_52w = max(hist_high, st.price)
    st.low_52w = min(hist_low, st.price)
    if len(df) >= 7:
        st.change_7d_pct = pct(st.price, float(df["close"].iloc[-7]))

    tf1d = (st.timeframes or {}).get("1D")
    macd_cross = tf1d["macd"]["cross"] if tf1d and tf1d.get("macd") else None
    bb_squeeze = bool(tf1d["bollinger"]["squeeze"]) if tf1d and tf1d.get("bollinger") else False
    divergence_kind = st.divergence["kind"] if st.divergence else None
    near_support, near_resistance = _nearest_levels((st.support or []) + (st.resistance or []), st.price)

    result = score_setup(
        symbol=symbol,
        price=st.price,
        change_24h_pct=st.change_24h_pct,
        change_7d_pct=st.change_7d_pct,
        rsi14=st.rsi14,
        sma50=st.sma50,
        sma200=st.sma200,
        high_52w=st.high_52w,
        low_52w=st.low_52w,
        vol_ratio=st.vol_ratio,
        rsi_overbought=config.RSI_OVERBOUGHT,
        rsi_oversold=config.RSI_OVERSOLD,
        macd_cross=macd_cross,
        bb_squeeze=bb_squeeze,
        divergence=divergence_kind,
        near_support=near_support,
        near_resistance=near_resistance,
        confluence_info=st.confluence,
    )
    st.heat, st.likelihood, st.direction, st.tags = (
        result["heat"], result["likelihood"], result["direction"], result["tags"],
    )
    _check_alerts(st)


def _check_alerts(st) -> None:
    # NOTE: never start the message with a literal "$" followed by a digit —
    # CallMeBot's backend silently strips a leading "$<digit>" (looks like it
    # runs text through something that treats "$1" etc. as a template token),
    # which is what was garbling every price in earlier alerts. "USD " is safe.
    price_str = f"USD {fmt_price(st.price)}"

    if st.rsi14 is not None and st.rsi14 >= config.RSI_OVERBOUGHT:
        maybe_alert(
            f"{st.symbol}:rsi_ob",
            f"[Quant Desk] {st.symbol} looks overbought — RSI {st.rsi14:.0f} at {price_str}. "
            f"Momentum is strong, but a reading this stretched often cools off into a pullback or "
            f"sideways chop even inside a genuine uptrend. Not a sell signal on its own — worth "
            f"watching for the momentum to roll over.",
        )
    if st.rsi14 is not None and st.rsi14 <= config.RSI_OVERSOLD:
        maybe_alert(
            f"{st.symbol}:rsi_os",
            f"[Quant Desk] {st.symbol} looks oversold — RSI {st.rsi14:.0f} at {price_str}. "
            f"Selling pressure looks stretched, which often precedes a bounce — but confirm a base "
            f"is actually forming before treating this as a reversal; oversold can stay oversold.",
        )
    if st.high_52w and st.price >= st.high_52w * 0.999:
        maybe_alert(
            f"{st.symbol}:52w_high",
            f"[Quant Desk] {st.symbol} is testing its 52-week high near {price_str}. "
            f"A confirmed push through this level would be a bullish breakout signal; a rejection "
            f"here would likely mean a pullback back into the prior range instead.",
        )
    if st.divergence is not None:
        kind = st.divergence["kind"]
        explain = (
            "price pushed to a new high while momentum (RSI) made a lower high — the rally is losing "
            "steam even as price grinds up, a classic setup ahead of a pullback or reversal"
            if kind == "bearish" else
            "price pushed to a new low while momentum (RSI) made a higher low — selling pressure is "
            "fading even as price grinds down, a classic setup ahead of a bounce or reversal"
        )
        maybe_alert(
            f"{st.symbol}:divergence_{kind}",
            f"[Quant Desk] {st.symbol} {kind} divergence at {price_str}: {explain}.",
        )
    if st.confluence is not None and st.confluence.get("label", "").startswith("Strong"):
        maybe_alert(
            f"{st.symbol}:confluence_{st.confluence['direction']}",
            f"[Quant Desk] {st.symbol} has {st.confluence['agree']}/{st.confluence['total']} timeframes "
            f"aligned {st.confluence['direction']} at {price_str} — a multi-timeframe {st.confluence['direction']} "
            f"confluence like this is a stronger signal than any single chart, since it means shorter-term "
            f"and longer-term trends agree right now.",
        )
    if st.likelihood == "Very High":
        dir_text = {
            "bullish bias": "the broader trend leans bullish, so the higher-probability break is to the upside",
            "bearish bias": "the broader trend leans bearish, so the higher-probability break is to the downside",
            "two-sided": "signals are mixed, so this could break either way — treat it as a heads-up to watch, not a directional call",
        }.get(st.direction, "this could break either way")
        tag_text = ", ".join(st.tags) if st.tags else "several signals lining up at once"
        maybe_alert(
            f"{st.symbol}:very_high",
            f"[Quant Desk] {st.symbol} breakout heat is Very High at {price_str} — {dir_text}. "
            f"Signals: {tag_text}. Worth a closer look for a larger move in the near term.",
        )


# ---------------------------------------------------------------------------
# Background loops: keep the deep watchlist and its indicators fresh
# ---------------------------------------------------------------------------

async def _rescan_and_update() -> None:
    global _deep_symbols
    if not config.UNIVERSE_SCAN_ENABLED:
        return
    universe = await asyncio.get_event_loop().run_in_executor(None, discover_universe)
    state.universe_scanned_count = len(universe)
    new_deep = pick_deep_watchlist(universe)
    added = [s for s in new_deep if s not in _history]
    if added:
        await asyncio.get_event_loop().run_in_executor(None, seed_deep_watchlist, added, True)
    if set(new_deep) != set(_deep_symbols):
        _deep_symbols = new_deep
        state.deep_symbols = set(new_deep)
        _resubscribe_needed.set()
        log.info("Deep watchlist updated: %d symbols (%d newly discovered this scan)", len(new_deep), len(added))
    await state.broadcast()


async def _universe_rescan_loop() -> None:
    while True:
        await asyncio.sleep(config.UNIVERSE_RESCAN_SECONDS)
        try:
            await _rescan_and_update()
        except Exception:
            log.warning("Universe rescan failed (will retry next cycle)", exc_info=True)


async def _deep_refresh_all() -> None:
    symbols = list(_deep_symbols)
    await asyncio.get_event_loop().run_in_executor(None, seed_deep_watchlist, symbols, False)

    occ_by_symbol = {}
    closes_by_symbol = {}
    for sym in symbols:
        df1d = _history.get(sym, {}).get("1D")
        if df1d is not None and len(df1d) >= 40:
            occ_by_symbol[sym] = find_signal_occurrences(df1d, config.RSI_OVERBOUGHT, config.RSI_OVERSOLD)
            closes_by_symbol[sym] = df1d["close"].astype(float)
    if occ_by_symbol:
        # Costs and the unconditional base rate both go in here: without
        # them a win rate is flattering by construction, especially on a
        # watchlist populated with today's biggest movers.
        state.signal_stats = aggregate_signal_stats(
            occ_by_symbol,
            cost_pct=config.round_trip_cost_pct,
            per_symbol_closes=closes_by_symbol,
        )

    for sym in symbols:
        _recompute(sym)  # fold the freshly-updated deep fields into heat/tags/alerts right away

    # Live execution (v8, 2026-08-30): evaluate every deep-watchlist symbol
    # against the entry gate once per cycle, same cadence as everything else
    # here. Deliberately NOT called from the fast tick path -- see
    # execution/engine.py's evaluate_watchlist() docstring. A no-op unless
    # EXECUTION_ENABLED=true.
    if config.EXECUTION_ENABLED:
        try:
            from execution import engine as execution_engine
            await asyncio.get_event_loop().run_in_executor(None, execution_engine.evaluate_watchlist, symbols, state.symbols, config)
        except Exception:
            log.warning("Execution watchlist evaluation failed this cycle (will retry next cycle)", exc_info=True)

    await state.broadcast()


async def _deep_refresh_loop() -> None:
    while True:
        await asyncio.sleep(config.DEEP_REFRESH_SECONDS)
        try:
            await _deep_refresh_all()
        except Exception:
            log.warning("Deep refresh failed (will retry next cycle)", exc_info=True)


# ---------------------------------------------------------------------------
# Live WebSocket loop
# ---------------------------------------------------------------------------

def _apply_ticker_derivatives(st, data: dict) -> None:
    """Linear ticker messages carry `fundingRate` and `openInterest` inline.
    That's the same data the slow REST loop fetches, arriving for free and
    far fresher, so we fold it in on every tick that includes it. Only the
    *interpretation* is recomputed here; the historical series that funding
    trend detection needs still comes from the REST loop."""
    if not config.is_derivatives:
        return
    changed = False

    funding_rate = data.get("fundingRate")
    if funding_rate not in (None, ""):
        try:
            rate = float(funding_rate)
        except (TypeError, ValueError):
            rate = None
        if rate is not None:
            st.funding = interpret_funding(
                rate,
                extreme_annual_pct=config.FUNDING_EXTREME_ANNUAL_PCT,
                elevated_annual_pct=config.FUNDING_ELEVATED_ANNUAL_PCT,
                history=st.funding_history or None,
            )
            changed = True

    oi = data.get("openInterest")
    if oi not in (None, "") and st.open_interest:
        try:
            oi_now = float(oi)
        except (TypeError, ValueError):
            oi_now = None
        # Re-run the quadrant read against the same baseline the REST loop
        # established, so a live OI tick updates the read without needing a
        # fresh history fetch.
        if oi_now and st.open_interest.get("previous"):
            st.open_interest = interpret_open_interest(
                oi_now, st.open_interest["previous"], st.open_interest.get("price_change_pct"),
            ) or st.open_interest
            changed = True

    if changed:
        _recompute_positioning(st)


# ---------------------------------------------------------------------------
# Live WebSocket klines for Spotlight's fast timeframes
# ---------------------------------------------------------------------------

# Only the frames fast enough that a 3-minute REST cycle would make them
# misleading. 15m and slower stay on the REST refresh -- a 15m candle simply
# doesn't change fast enough to justify the extra socket traffic.
LIVE_KLINE_TFS = {"1m": "1", "3m": "3", "5m": "5"}
_WS_INTERVAL_TO_TF = {v: k for k, v in LIVE_KLINE_TFS.items()}


def _spotlight_kline_topics() -> list[str]:
    if not (config.SPOTLIGHT_LIVE_KLINES and _spotlight_symbol):
        return []
    return [f"kline.{interval}.{_spotlight_symbol}" for interval in LIVE_KLINE_TFS.values()]


def _apply_live_kline(topic: str, rows: list[dict]) -> None:
    """Fold a live kline push into `_spotlight_history` so the fast rows are
    genuinely current.

    Bybit sends a candle repeatedly as it forms, with `confirm: false`, then
    once more with `confirm: true` when it closes. We upsert on candle start
    time: an in-progress candle overwrites the last row if it's the same
    candle, and appends a new row when a new candle begins. That keeps the
    dataframe the indicators read from exactly the shape `_fetch_klines`
    would have produced, so nothing downstream needs to know the difference.

    Indicators are only recomputed on candle *close*, not on every in-flight
    tick: recomputing a 9-timeframe suite at socket frequency would burn CPU
    to produce readings that flicker with every tick, and a half-formed
    candle's RSI isn't a number worth acting on anyway.
    """
    parts = topic.split(".")
    if len(parts) != 3:
        return
    _, interval, symbol = parts
    tf = _WS_INTERVAL_TO_TF.get(interval)
    if tf is None or symbol != _spotlight_symbol:
        return

    df = _spotlight_history.get(tf)
    if df is None or df.empty:
        return  # nothing seeded yet; the REST refresh will populate it first

    closed_any = False
    for row in rows:
        try:
            start = float(row["start"])
            candle = {
                "start": start,
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        except (KeyError, TypeError, ValueError):
            continue

        last_start = float(df["start"].iloc[-1])
        if start == last_start:
            for col, val in candle.items():
                df.iloc[-1, df.columns.get_loc(col)] = val
        elif start > last_start:
            df.loc[len(df)] = [candle[c] for c in df.columns]
            if len(df) > SPOTLIGHT_LIMITS.get(tf, 300):
                df = df.iloc[-SPOTLIGHT_LIMITS.get(tf, 300):].reset_index(drop=True)
                _spotlight_history[tf] = df
        else:
            continue  # a late message for an older candle; ignore

        if row.get("confirm"):
            closed_any = True

    if not closed_any:
        return

    # A fast frame just closed -- refresh the 10m synthetic series if this was
    # the 5m frame it's derived from, then recompute just the fast timeframes
    # and the reads that depend on them. The slow frames are left alone.
    if tf == "5m":
        _spotlight_history["10m"] = _resample_10m(_spotlight_history["5m"])
    _recompute_spotlight_from_history(symbol, only_tfs=list(LIVE_KLINE_TFS) + ["10m"])


def _recompute_spotlight_from_history(symbol: str, only_tfs: list[str] | None = None) -> None:
    """Recompute the Spotlight payload from whatever is already in
    `_spotlight_history` -- no network. Used by the live-kline path so a
    closing 1m candle updates the panel immediately instead of waiting for
    the next REST cycle."""
    existing = state.spotlight
    if not existing or existing.get("symbol") != symbol:
        return
    tf_summaries = dict(existing.get("timeframes") or {})
    targets = only_tfs if only_tfs is not None else SPOTLIGHT_TF_ORDER
    for tf in targets:
        summary = _spotlight_tf_summary(_spotlight_history.get(tf), tf)
        if summary is not None:
            tf_summaries[tf] = summary
    state.spotlight = _assemble_spotlight_payload(symbol, tf_summaries)


async def _ping_forever(ws, interval: float = 20.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            return  # socket's dead; the outer loop will notice and reconnect


async def run(broadcast_every: float = 2.0) -> None:
    # IMPORTANT: seeding used to happen *before* the try/except below. If it
    # ever raised something the per-symbol try/except inside seeding didn't
    # catch, that exception would propagate straight out of run() and
    # silently kill this whole background task -- the UI would then sit on
    # "starting" forever with no error anywhere, which is exactly what
    # happened on one run. Seeding now happens *inside* the loop's try block
    # so any failure here is caught, logged, and retried like everything else.
    global _deep_symbols
    seeded = False
    last_broadcast = 0.0
    while True:
        try:
            if not seeded:
                state.feed_status["bybit"] = "scanning market"
                if config.UNIVERSE_SCAN_ENABLED:
                    try:
                        universe = await asyncio.get_event_loop().run_in_executor(None, discover_universe)
                        state.universe_scanned_count = len(universe)
                        _deep_symbols = pick_deep_watchlist(universe)
                    except Exception as exc:
                        log.warning("Universe scan failed, falling back to the pinned watchlist only: %s", exc)
                        _deep_symbols = list(dict.fromkeys(config.CRYPTO_SYMBOLS))
                else:
                    _deep_symbols = list(dict.fromkeys(config.CRYPTO_SYMBOLS))
                state.deep_symbols = set(_deep_symbols)

                state.feed_status["bybit"] = "seeding history"
                await asyncio.get_event_loop().run_in_executor(None, seed_deep_watchlist, _deep_symbols, True)
                seeded = True
                # These run for the lifetime of the process, independent of
                # WS reconnects, keeping the watchlist membership and its
                # multi-timeframe indicators fresh over time.
                asyncio.create_task(_universe_rescan_loop(), name="universe_rescan")
                asyncio.create_task(_deep_refresh_loop(), name="deep_refresh")
                asyncio.create_task(_derivatives_refresh_loop(), name="derivatives_refresh")

            state.feed_status["bybit"] = "connecting"
            # Deep-watchlist symbols + anything the user opened a paper
            # position on that isn't already in that set (see
            # track_extra_symbol) -- both need live ticks, only the former
            # gets full multi-timeframe analysis.
            symbols = list(dict.fromkeys(_deep_symbols + list(_extra_position_symbols)))
            topics = [f"tickers.{s}" for s in symbols]
            # Liquidations only exist on derivatives, and they're the defining
            # mechanic of a leveraged market -- a 2% drop caused by forced
            # closes is a different event from a 2% drop caused by selling.
            if config.is_derivatives:
                topics += [f"allLiquidation.{s}" for s in symbols]
            # Spotlight's fast timeframes come straight off the socket rather
            # than the 3-minute REST cycle, so a "1m" reading is actually
            # current instead of up to three candles stale.
            live_kline_topics = _spotlight_kline_topics()
            topics += live_kline_topics

            async with websockets.connect(WS_URL, ping_interval=None, open_timeout=15, close_timeout=5) as ws:
                # subscribe in batches, per Bybit's per-message topic limit
                for i in range(0, len(topics), MAX_TOPICS_PER_SUBSCRIBE):
                    await ws.send(json.dumps({"op": "subscribe", "args": topics[i:i + MAX_TOPICS_PER_SUBSCRIBE]}))

                ping_task = asyncio.create_task(_ping_forever(ws))
                _resubscribe_needed.clear()
                state.feed_status["bybit"] = "live"
                pinned_n = len(set(config.CRYPTO_SYMBOLS) & set(symbols))
                extra_n = len(_extra_position_symbols)
                log.info(
                    "Bybit %s WebSocket connected (%d symbols: %d pinned + %d discovered + %d position-only; "
                    "%d liquidation topics, %d live-kline topics)",
                    CATEGORY, len(symbols), pinned_n, len(symbols) - pinned_n - extra_n, extra_n,
                    len(symbols) if config.is_derivatives else 0, len(live_kline_topics),
                )

                try:
                    async for raw in ws:
                        if _resubscribe_needed.is_set():
                            log.info("Subscription set changed — reconnecting to update subscriptions")
                            break
                        msg = json.loads(raw)
                        if msg.get("op") == "ping" or msg.get("ret_msg") == "pong":
                            continue
                        topic = msg.get("topic", "")

                        if topic.startswith("tickers."):
                            data = msg.get("data", {})
                            symbol = data.get("symbol") or topic.split(".", 1)[1]
                            last_price = data.get("lastPrice")
                            st = state.ensure(symbol, "crypto")
                            # On linear the ticker stream sends deltas, so any
                            # given message may carry only some fields --
                            # update whatever is present, skip what isn't.
                            if last_price:
                                price = float(last_price)
                                if price > 0:
                                    st.push_price(price)
                                    st.connected = True
                                    pcnt = data.get("price24hPcnt")
                                    if pcnt is not None:
                                        st.change_24h_pct = float(pcnt) * 100
                                    _recompute(symbol)
                            # Linear tickers carry funding and open interest
                            # inline -- free, and fresher than the REST loop.
                            _apply_ticker_derivatives(st, data)

                        elif topic.startswith("allLiquidation."):
                            for row in msg.get("data", []) or []:
                                try:
                                    _record_liquidation(
                                        row.get("s") or topic.split(".", 1)[1],
                                        row.get("S", ""),
                                        float(row.get("p", 0)),
                                        float(row.get("v", 0)),
                                    )
                                except (TypeError, ValueError):
                                    continue
                            liq_symbol = topic.split(".", 1)[1]
                            liq_state = state.symbols.get(liq_symbol)
                            if liq_state is not None:
                                _recompute_positioning(liq_state)

                        elif topic.startswith("kline."):
                            _apply_live_kline(topic, msg.get("data", []) or [])

                        else:
                            continue

                        now = time.time()
                        if now - last_broadcast >= broadcast_every:
                            last_broadcast = now
                            await state.broadcast()
                finally:
                    ping_task.cancel()
        except Exception as exc:
            state.feed_status["bybit"] = f"reconnecting (last error: {exc})"
            log.warning("Bybit feed error — reconnecting in 5s", exc_info=True)
            await asyncio.sleep(5)
