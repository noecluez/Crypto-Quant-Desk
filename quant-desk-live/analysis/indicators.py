"""Pure functions: turn OHLCV price history into a full technical picture —
RSI(14), 50/200 SMA, MACD, Bollinger Bands, Stochastic, ATR, anchored VWAP,
RSI/price divergence, fractal support/resistance, Fibonacci retracements,
multi-timeframe confluence, and a per-signal historical backtest (see
backtest.py) — plus the qualitative breakout "heat" score that ties it all
together. No network, no I/O: every function here takes plain pandas/numpy
data in and returns plain dicts/floats out, which is what makes them cheap
to unit-test with synthetic data (see /tmp and the project doc for the
methodology notes).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Core single-value indicators (existing, unchanged behavior)
# ---------------------------------------------------------------------------

def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI as a full series (index-aligned with `closes`, NaN until
    warmed up). Used both for the live "current RSI" number and for scanning
    history for oversold/overbought *crossings* in the backtester."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    out[avg_loss == 0] = 100.0
    return out


def rsi(closes: pd.Series, period: int = 14) -> float | None:
    """Classic Wilder RSI on a series of closes (oldest first). Needs >period points."""
    if len(closes) < period + 1:
        return None
    val = rsi_series(closes, period).iloc[-1]
    return None if pd.isna(val) else float(val)


def sma(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return float(closes.iloc[-period:].mean())


def pct(a: float, b: float) -> float:
    """% change of a relative to b."""
    if b == 0:
        return 0.0
    return (a - b) / b * 100


def fmt_price(p: float) -> str:
    """Human-readable price with no scientific notation, ever — used in both
    the UI and WhatsApp alerts. Python's general format spec (e.g. ``.4g``)
    renders large prices as "7.853e+04", which is unreadable in a text
    message, so we always spell it out with fixed decimals instead."""
    if p is None:
        return "n/a"
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def macd_series(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """Latest MACD reading plus whether the histogram just crossed zero
    (the tradeable "cross" event, not just the sign)."""
    if len(closes) < slow + signal:
        return None
    df = macd_series(closes, fast, slow, signal)
    if len(df) < 2 or df["hist"].iloc[-2:].isna().any():
        return None
    prev_hist, cur_hist = float(df["hist"].iloc[-2]), float(df["hist"].iloc[-1])
    cross = None
    if prev_hist <= 0 < cur_hist:
        cross = "bullish"
    elif prev_hist >= 0 > cur_hist:
        cross = "bearish"
    return {
        "macd": float(df["macd"].iloc[-1]),
        "signal": float(df["signal"].iloc[-1]),
        "hist": cur_hist,
        "cross": cross,
    }


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bollinger_series(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": bandwidth})


def bollinger(closes: pd.Series, period: int = 20, num_std: float = 2.0, squeeze_lookback: int = 120) -> dict | None:
    """Latest Bollinger reading. `squeeze` is relative to the band's *own*
    recent history (bandwidth below half its rolling median) rather than a
    fixed number, since "tight bands" means something different for BTC than
    for a low-cap altcoin."""
    if len(closes) < period:
        return None
    df = bollinger_series(closes, period, num_std)
    last = df.iloc[-1]
    if pd.isna(last["mid"]) or last["mid"] == 0:
        return None
    price = float(closes.iloc[-1])
    percent_b = (price - last["lower"]) / (last["upper"] - last["lower"]) if last["upper"] != last["lower"] else 0.5
    recent_bw = df["bandwidth"].tail(squeeze_lookback).dropna()
    squeeze = bool(len(recent_bw) >= period and last["bandwidth"] < 0.5 * recent_bw.median())
    return {
        "mid": float(last["mid"]),
        "upper": float(last["upper"]),
        "lower": float(last["lower"]),
        "bandwidth": float(last["bandwidth"]),
        "percent_b": float(percent_b),
        "squeeze": squeeze,
    }


# ---------------------------------------------------------------------------
# Stochastic Oscillator
# ---------------------------------------------------------------------------

def stochastic_series(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                       k_period: int = 14, k_smooth: int = 3, d_period: int = 3) -> pd.DataFrame:
    lowest = lows.rolling(k_period).min()
    highest = highs.rolling(k_period).max()
    raw_k = 100 * (closes - lowest) / (highest - lowest).replace(0, np.nan)
    k = raw_k.rolling(k_smooth).mean()
    d = k.rolling(d_period).mean()
    return pd.DataFrame({"k": k, "d": d})


def stochastic(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                k_period: int = 14, k_smooth: int = 3, d_period: int = 3) -> dict | None:
    if len(closes) < k_period + k_smooth + d_period:
        return None
    df = stochastic_series(highs, lows, closes, k_period, k_smooth, d_period)
    if len(df) < 2 or df.iloc[-2:].isna().any().any():
        return None
    prev_k, prev_d = float(df["k"].iloc[-2]), float(df["d"].iloc[-2])
    cur_k, cur_d = float(df["k"].iloc[-1]), float(df["d"].iloc[-1])
    cross = None
    if prev_k <= prev_d and cur_k > cur_d:
        cross = "bullish"
    elif prev_k >= prev_d and cur_k < cur_d:
        cross = "bearish"
    return {"k": cur_k, "d": cur_d, "cross": cross, "overbought": cur_k >= 80, "oversold": cur_k <= 20}


# ---------------------------------------------------------------------------
# ATR (volatility)
# ---------------------------------------------------------------------------

def atr_series(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    val = atr_series(highs, lows, closes, period).iloc[-1]
    return None if pd.isna(val) else float(val)


# ---------------------------------------------------------------------------
# Anchored VWAP (resets each UTC day — meaningful on intraday timeframes only)
# ---------------------------------------------------------------------------

def vwap_anchored_daily(df: pd.DataFrame) -> float | None:
    """`df` needs `start` (ms epoch), `high`, `low`, `close`, `volume`,
    oldest-first. Anchors to the most recent UTC-day boundary present in the
    data and VWAPs from there — the standard intraday VWAP definition."""
    if df.empty or "start" not in df.columns:
        return None
    ts = pd.to_datetime(df["start"], unit="ms", utc=True)
    day = ts.dt.floor("D")
    today = day.iloc[-1]
    session = df.loc[(day == today).values]
    if session.empty or float(session["volume"].sum()) == 0:
        return None
    typical = (session["high"].astype(float) + session["low"].astype(float) + session["close"].astype(float)) / 3
    vol = session["volume"].astype(float)
    return float((typical * vol).sum() / vol.sum())


# ---------------------------------------------------------------------------
# Divergence: price vs. an oscillator (RSI) disagreeing on swing direction
# ---------------------------------------------------------------------------

def _local_extrema(series: pd.Series, order: int = 3, kind: str = "max") -> list[int]:
    """Confirmed fractal extrema: index i qualifies if it's the (not
    necessarily strict) max/min of the window [i-order, i+order]. The most
    recent `order` bars can never be confirmed yet (no future bars to
    compare against) — that's intentional, it avoids flagging a swing that
    might still be forming. Real price data often has flat tops/bottoms
    (a repeated high, a rounded price) — a strict-uniqueness check would
    silently find nothing at all near a tie, so instead we accept ties and
    then collapse any run of adjacent qualifying indices (the same plateau)
    down to its single middle point."""
    vals = series.values
    n = len(vals)
    raw = []
    for i in range(order, n - order):
        window = vals[i - order:i + order + 1]
        if np.isnan(window).any():
            continue
        center = vals[i]
        if kind == "max" and center >= window.max():
            raw.append(i)
        elif kind == "min" and center <= window.min():
            raw.append(i)

    if not raw:
        return []
    out = []
    run = [raw[0]]
    for i in raw[1:]:
        if i - run[-1] <= order:
            run.append(i)
        else:
            out.append(run[len(run) // 2])
            run = [i]
    out.append(run[len(run) // 2])
    return out


def detect_divergence(closes: pd.Series, indicator: pd.Series, lookback: int = 60, order: int = 3) -> dict | None:
    """Compares the last two confirmed swing highs (or lows) in `closes`
    against the indicator's value at those same two points. Classic regular
    divergence: price makes a higher high while the indicator makes a lower
    high (bearish), or price makes a lower low while the indicator makes a
    higher low (bullish) — an early warning that the move's momentum doesn't
    support its price action."""
    if len(closes) < lookback:
        lookback = len(closes)
    c = closes.tail(lookback).reset_index(drop=True)
    ind = indicator.tail(lookback).reset_index(drop=True)

    highs_idx = _local_extrema(c, order=order, kind="max")
    lows_idx = _local_extrema(c, order=order, kind="min")

    if len(highs_idx) >= 2:
        i1, i2 = highs_idx[-2], highs_idx[-1]
        if not (pd.isna(ind.iloc[i1]) or pd.isna(ind.iloc[i2])):
            price_higher_high = c.iloc[i2] > c.iloc[i1]
            ind_lower_high = ind.iloc[i2] < ind.iloc[i1]
            if price_higher_high and ind_lower_high:
                return {"kind": "bearish", "detail": "price higher high, momentum lower high"}

    if len(lows_idx) >= 2:
        i1, i2 = lows_idx[-2], lows_idx[-1]
        if not (pd.isna(ind.iloc[i1]) or pd.isna(ind.iloc[i2])):
            price_lower_low = c.iloc[i2] < c.iloc[i1]
            ind_higher_low = ind.iloc[i2] > ind.iloc[i1]
            if price_lower_low and ind_higher_low:
                return {"kind": "bullish", "detail": "price lower low, momentum higher low"}

    return None


# ---------------------------------------------------------------------------
# Support / resistance via fractal swing clustering
# ---------------------------------------------------------------------------

def find_swing_levels(highs: pd.Series, lows: pd.Series, order: int = 3, lookback: int = 150) -> tuple[list[float], list[float]]:
    """Returns (swing_high_prices, swing_low_prices) — confirmed fractal
    extrema over the trailing `lookback` bars, raw (not yet clustered)."""
    h = highs.tail(lookback).reset_index(drop=True)
    l = lows.tail(lookback).reset_index(drop=True)
    high_idx = _local_extrema(h, order=order, kind="max")
    low_idx = _local_extrema(l, order=order, kind="min")
    return [float(h.iloc[i]) for i in high_idx], [float(l.iloc[i]) for i in low_idx]


def cluster_levels(levels: list[float], current_price: float, tolerance_pct: float = 1.0, top_n: int = 3) -> tuple[list[dict], list[dict]]:
    """Merges nearby swing levels into zones (a price level that got
    touched 3 times is a much stronger level than one touched once), then
    splits into supports (below price) and resistances (above price).
    Returns (supports, resistances), each a list of
    {price, touches, distance_pct}.

    Ordering is by PROXIMITY, closest first. This used to sort by touch
    count first, which meant a 3-touch level 6% away outranked a 1-touch
    level 0.2% away -- and since callers take `[0]` and label it "nearest",
    the panel could show a distant level under a "nearest support" heading.
    For stop placement on a fast trade that's actively misleading, so
    proximity now decides the order and strength is carried in `touches`
    for anyone who wants to weigh it (see strongest_level() below).
    """
    if not levels:
        return [], []
    levels_sorted = sorted(levels)
    clusters: list[list[float]] = []
    for lvl in levels_sorted:
        if clusters and abs(lvl - clusters[-1][-1]) / clusters[-1][-1] * 100 <= tolerance_pct:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])

    zones = [{"price": float(np.mean(c)), "touches": len(c)} for c in clusters]
    for z in zones:
        z["distance_pct"] = pct(z["price"], current_price)

    # abs(distance_pct) ascending == closest to current price first, for both
    # sides. Ties (rare) break toward the better-tested level.
    supports = sorted(
        [z for z in zones if z["price"] < current_price],
        key=lambda z: (abs(z["distance_pct"]), -z["touches"]),
    )[:top_n]
    resistances = sorted(
        [z for z in zones if z["price"] > current_price],
        key=lambda z: (abs(z["distance_pct"]), -z["touches"]),
    )[:top_n]
    return supports, resistances


def strongest_level(levels: list[dict]) -> dict | None:
    """The best-tested level in a list, regardless of distance. Distinct
    from the nearest one, and worth showing alongside it: the nearest level
    is what price has to get through next, the strongest is where it is
    most likely to actually stop."""
    if not levels:
        return None
    return max(levels, key=lambda z: (z.get("touches", 0), -abs(z.get("distance_pct", 0.0))))


# ---------------------------------------------------------------------------
# Fibonacci retracement, anchored to the most significant recent swing
# ---------------------------------------------------------------------------

FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fibonacci_levels(highs: pd.Series, lows: pd.Series, lookback: int = 90, order: int = 3) -> dict | None:
    """Finds the single largest swing (high-to-low or low-to-high) within
    the lookback window and returns standard retracement levels for it,
    tagged with which direction the underlying move was in."""
    swing_highs_idx = _local_extrema(highs.tail(lookback).reset_index(drop=True), order=order, kind="max")
    swing_lows_idx = _local_extrema(lows.tail(lookback).reset_index(drop=True), order=order, kind="min")
    if not swing_highs_idx or not swing_lows_idx:
        return None

    h = highs.tail(lookback).reset_index(drop=True)
    l = lows.tail(lookback).reset_index(drop=True)

    best = None
    for hi in swing_highs_idx:
        for li in swing_lows_idx:
            span = abs(h.iloc[hi] - l.iloc[li])
            if best is None or span > best[0]:
                best = (span, hi, li)
    if best is None:
        return None
    _, hi, li = best
    high_price, low_price = float(h.iloc[hi]), float(l.iloc[li])
    uptrend = li < hi  # low came first, then the high -> retracement measures a pullback within an uptrend
    levels = {}
    for r in FIB_RATIOS:
        levels[r] = high_price - r * (high_price - low_price) if uptrend else low_price + r * (high_price - low_price)
    return {"trend": "uptrend" if uptrend else "downtrend", "swing_high": high_price, "swing_low": low_price, "levels": levels}


# ---------------------------------------------------------------------------
# Multi-timeframe confluence
# ---------------------------------------------------------------------------

def timeframe_bias(*, price: float, rsi14: float | None, macd_hist: float | None,
                    sma50: float | None, percent_b: float | None) -> tuple[str, float]:
    """Boils one timeframe's indicator set down to a direction ("bullish" /
    "bearish" / "neutral") and a 0-1 strength, so timeframes can be compared
    and combined apples-to-apples. Each signal is a soft vote, not a veto."""
    votes = 0.0
    n = 0
    if rsi14 is not None:
        votes += 1 if rsi14 > 55 else (-1 if rsi14 < 45 else 0)
        n += 1
    if macd_hist is not None:
        votes += 1 if macd_hist > 0 else -1
        n += 1
    if sma50 is not None and price > 0:
        votes += 1 if price > sma50 else -1
        n += 1
    if percent_b is not None:
        votes += 1 if percent_b > 0.6 else (-1 if percent_b < 0.4 else 0)
        n += 1
    if n == 0:
        return "neutral", 0.0
    score = votes / n  # -1..1
    if score > 0.25:
        return "bullish", min(abs(score), 1.0)
    if score < -0.25:
        return "bearish", min(abs(score), 1.0)
    return "neutral", min(abs(score), 1.0)


# Longer timeframes carry more weight — a 1D bullish read matters more than a 15m blip.
DEFAULT_TIMEFRAME_WEIGHTS = {"15m": 0.5, "1h": 1.0, "4h": 2.0, "1D": 3.0}


def confluence(biases: dict[str, tuple[str, float]], weights: dict[str, float] | None = None) -> dict:
    """Combines per-timeframe (direction, strength) readings into one
    overall call. `agree`/`total` tells you how many of the available
    timeframes point the same way — the real "is this an edge or noise"
    signal, since a single-timeframe read is easy to fake with normal chop."""
    weights = weights or DEFAULT_TIMEFRAME_WEIGHTS
    if not biases:
        return {"label": "No data", "score": 0.0, "direction": "neutral", "agree": 0, "total": 0}

    weighted_sum = 0.0
    weight_total = 0.0
    bullish_count = sum(1 for d, _ in biases.values() if d == "bullish")
    bearish_count = sum(1 for d, _ in biases.values() if d == "bearish")
    for tf, (direction, strength) in biases.items():
        w = weights.get(tf, 1.0)
        signed = strength if direction == "bullish" else (-strength if direction == "bearish" else 0.0)
        weighted_sum += w * signed
        weight_total += w
    score = weighted_sum / weight_total if weight_total else 0.0

    agree = max(bullish_count, bearish_count)
    total = len(biases)
    leaning = "bullish" if bullish_count >= bearish_count else "bearish"

    if score >= 0.5 and agree >= max(3, total - 1):
        label, direction = "Strong Bullish Confluence", "bullish"
    elif score <= -0.5 and agree >= max(3, total - 1):
        label, direction = "Strong Bearish Confluence", "bearish"
    elif score > 0.15:
        label, direction = "Bullish Lean", "bullish"
    elif score < -0.15:
        label, direction = "Bearish Lean", "bearish"
    else:
        label, direction = "Mixed / Neutral", "neutral"

    return {"label": label, "score": round(float(score), 2), "direction": direction, "agree": agree, "total": total, "leaning": leaning}


# ---------------------------------------------------------------------------
# Composite breakout "heat" score
# ---------------------------------------------------------------------------

def score_setup(
    *,
    symbol: str,
    price: float,
    change_24h_pct: float | None,
    change_7d_pct: float | None,
    rsi14: float | None,
    sma50: float | None,
    sma200: float | None,
    high_52w: float | None,
    low_52w: float | None,
    vol_ratio: float | None,  # today's volume / avg volume, if known
    rsi_overbought: float = 70,
    rsi_oversold: float = 30,
    macd_cross: str | None = None,
    bb_squeeze: bool = False,
    divergence: str | None = None,       # "bullish" | "bearish" | None
    near_support: dict | None = None,    # {"price":.., "distance_pct":.., "touches":..}
    near_resistance: dict | None = None,
    confluence_info: dict | None = None,  # output of confluence()
) -> dict:
    """Reproduce the desk's qualitative methodology on live numbers:
    momentum + RSI extremes + MA position + proximity to 52w range + volume,
    now extended with MACD crosses, Bollinger squeezes, RSI divergence,
    fractal support/resistance proximity, and multi-timeframe confluence.
    Returns a dict with a 0-100 'heat' score, a direction, and short tags —
    the same vocabulary the dashboard already uses, just computed live now.
    """
    heat = 0.0
    tags: list[str] = []
    direction = "two-sided"
    # Signals that represent genuine uncertainty/reversal risk must win over
    # the plain trend-following MA check below (this exact bug — the MA
    # check silently overwriting an RSI-extreme "two-sided" call back to a
    # trend bias — was caught and fixed 2026-08-26; divergence carries the
    # same "uncertainty wins" property and must gate the same way).
    trend_override = False

    if change_7d_pct is not None:
        heat += min(abs(change_7d_pct), 40) * 0.6
        if change_7d_pct > 8:
            tags.append(f"+{change_7d_pct:.1f}% 7d")
        elif change_7d_pct < -8:
            tags.append(f"{change_7d_pct:.1f}% 7d")

    if rsi14 is not None:
        if rsi14 >= rsi_overbought:
            heat += (rsi14 - rsi_overbought) * 1.2
            tags.append(f"RSI {rsi14:.0f} overbought")
            trend_override = True
        elif rsi14 <= rsi_oversold:
            heat += (rsi_oversold - rsi14) * 1.2
            tags.append(f"RSI {rsi14:.0f} oversold")
            trend_override = True

    if divergence is not None:
        heat += 15
        tags.append(f"{divergence} divergence")
        trend_override = True

    if sma50 is not None and sma200 is not None and not trend_override:
        above50 = price > sma50
        above200 = price > sma200
        if above50 and above200:
            direction = "bullish bias" if direction == "two-sided" else direction
        elif not above50 and not above200:
            direction = "bearish bias" if direction == "two-sided" else direction
        # a fresh cross is the interesting event
        gap50 = pct(price, sma50)
        if abs(gap50) < 1.5:
            heat += 10
            tags.append("at 50D MA")

    if macd_cross is not None:
        heat += 12
        tags.append(f"MACD {macd_cross} cross")
        if not trend_override:
            direction = (f"{macd_cross} bias") if direction == "two-sided" else direction

    if bb_squeeze:
        heat += 8
        tags.append("BB squeeze (volatility contracting)")

    if high_52w is not None and price > 0:
        from_high = pct(price, high_52w)
        if from_high > -3:
            heat += 20
            tags.append("near 52w high")
    if low_52w is not None and price > 0:
        from_low = pct(price, low_52w)
        if from_low < 3:
            heat += 20
            tags.append("near 52w low")

    if near_support is not None:
        heat += 12
        tags.append(f"near support {fmt_price(near_support['price'])} ({near_support.get('touches', 1)}x tested)")
    if near_resistance is not None:
        heat += 12
        tags.append(f"near resistance {fmt_price(near_resistance['price'])} ({near_resistance.get('touches', 1)}x tested)")

    if vol_ratio is not None and vol_ratio > 1.5:
        heat += min((vol_ratio - 1) * 15, 25)
        tags.append(f"{vol_ratio:.1f}x avg volume")

    if confluence_info is not None and confluence_info.get("label", "").startswith("Strong"):
        heat += 15
        tags.append(f"{confluence_info['agree']}/{confluence_info['total']} timeframes aligned {confluence_info['direction']}")
        # Same "uncertainty wins" rule as everywhere else in this function:
        # a strong multi-timeframe read is a real signal, but it must not
        # paper over an RSI-extreme or divergence call, which exist
        # specifically to flag elevated reversal risk.
        if direction == "two-sided" and not trend_override:
            direction = f"{confluence_info['direction']} bias"

    heat = max(0.0, min(100.0, heat))
    if heat >= 70:
        likelihood = "Very High"
    elif heat >= 50:
        likelihood = "High"
    elif heat >= 30:
        likelihood = "Elevated"
    else:
        likelihood = "Low"

    return {
        "symbol": symbol,
        "heat": round(heat, 1),
        "likelihood": likelihood,
        "direction": direction,
        "tags": tags,
    }
