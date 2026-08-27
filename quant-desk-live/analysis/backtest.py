"""Statistical edge validation: scans a symbol's own historical bars for
occurrences of each named signal, measures the forward return some bars
later, and aggregates those occurrences across the whole deep watchlist so
each signal type gets a real "this has worked X% of the time, n=Y" stat
instead of a one-off heuristic guess.

Aggregating across ~20 symbols instead of reporting per-symbol stats is
deliberate: a single symbol's ~260-bar daily history only throws off a
handful of occurrences of any one signal, which isn't enough to say
anything statistically meaningful. Pooling the whole watchlist gives a
usable sample size while staying honest about it — every stat carries its
`n` so a thin sample is visible, not hidden.

No network, no I/O — pure functions over pandas data, consistent with
analysis/indicators.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analysis.indicators import rsi_series, macd_series, bollinger_series

FORWARD_BARS = (5, 10)

# name -> the direction a "win" means for that signal
SIGNAL_TYPES = {
    "rsi_oversold_cross": "bullish",
    "rsi_overbought_cross": "bearish",
    "macd_bull_cross": "bullish",
    "macd_bear_cross": "bearish",
    "bb_squeeze_breakout_up": "bullish",
    "bb_squeeze_breakout_down": "bearish",
}

SIGNAL_LABELS = {
    "rsi_oversold_cross": "RSI oversold cross",
    "rsi_overbought_cross": "RSI overbought cross",
    "macd_bull_cross": "MACD bullish cross",
    "macd_bear_cross": "MACD bearish cross",
    "bb_squeeze_breakout_up": "BB squeeze breakout (up)",
    "bb_squeeze_breakout_down": "BB squeeze breakout (down)",
}


def _forward_returns(closes: pd.Series, idx: int, horizons=FORWARD_BARS) -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    base = closes.iloc[idx]
    for h in horizons:
        j = idx + h
        if j < len(closes) and base > 0:
            out[h] = float((closes.iloc[j] - base) / base * 100)
        else:
            out[h] = None
    return out


def find_signal_occurrences(df: pd.DataFrame, rsi_overbought: float = 70, rsi_oversold: float = 30) -> dict[str, list[dict]]:
    """`df` needs an oldest-first `close` column (a daily-bar DataFrame is
    the natural input — enough history for MACD/Bollinger to warm up).
    Returns {signal_name: [ {index, forward_returns: {5: pct, 10: pct}} ]}
    for every historical occurrence of each signal, so downstream code can
    both count them and compute forward-return statistics."""
    closes = df["close"].astype(float).reset_index(drop=True)
    n = len(closes)
    occurrences: dict[str, list[dict]] = {name: [] for name in SIGNAL_TYPES}
    if n < 40:
        return occurrences

    rsis = rsi_series(closes)
    macd_df = macd_series(closes)
    boll_df = bollinger_series(closes)
    bw_median = boll_df["bandwidth"].rolling(120, min_periods=20).median()

    for i in range(1, n):
        r_prev, r_cur = rsis.iloc[i - 1], rsis.iloc[i]
        if not pd.isna(r_prev) and not pd.isna(r_cur):
            if r_prev <= rsi_oversold < r_cur:
                occurrences["rsi_oversold_cross"].append({"index": i, "forward_returns": _forward_returns(closes, i)})
            if r_prev >= rsi_overbought > r_cur:
                occurrences["rsi_overbought_cross"].append({"index": i, "forward_returns": _forward_returns(closes, i)})

        h_prev, h_cur = macd_df["hist"].iloc[i - 1], macd_df["hist"].iloc[i]
        if not pd.isna(h_prev) and not pd.isna(h_cur):
            if h_prev <= 0 < h_cur:
                occurrences["macd_bull_cross"].append({"index": i, "forward_returns": _forward_returns(closes, i)})
            if h_prev >= 0 > h_cur:
                occurrences["macd_bear_cross"].append({"index": i, "forward_returns": _forward_returns(closes, i)})

        bw_prev, med_prev = boll_df["bandwidth"].iloc[i - 1], bw_median.iloc[i - 1]
        if not pd.isna(bw_prev) and not pd.isna(med_prev) and bw_prev < 0.5 * med_prev:
            upper_prev, lower_prev = boll_df["upper"].iloc[i - 1], boll_df["lower"].iloc[i - 1]
            if not pd.isna(upper_prev) and closes.iloc[i] > upper_prev:
                occurrences["bb_squeeze_breakout_up"].append({"index": i, "forward_returns": _forward_returns(closes, i)})
            elif not pd.isna(lower_prev) and closes.iloc[i] < lower_prev:
                occurrences["bb_squeeze_breakout_down"].append({"index": i, "forward_returns": _forward_returns(closes, i)})

    return occurrences


def _base_rates(per_symbol_closes: dict[str, "pd.Series"], horizons=FORWARD_BARS) -> dict[int, dict]:
    """The unconditional forward return over the same data -- i.e. what you
    would have got by entering at a random bar with no signal at all.

    This is the comparison that turns a win rate into an edge estimate. A
    signal with a 60% win rate sounds good until you notice the asset rose
    in 60% of ALL windows in the sample, at which point the signal has told
    you nothing. Without this row, every stat in the panel is flattering by
    construction -- especially here, where the watchlist is populated with
    today's biggest movers.
    """
    out: dict[int, dict] = {}
    for h in horizons:
        rets: list[float] = []
        for closes in per_symbol_closes.values():
            c = closes.reset_index(drop=True)
            for i in range(len(c) - h):
                base = c.iloc[i]
                if base > 0:
                    rets.append(float((c.iloc[i + h] - base) / base * 100))
        if not rets:
            out[h] = {"n": 0, "up_rate": None, "avg_return": None}
            continue
        arr = np.array(rets)
        out[h] = {
            "n": int(len(arr)),
            "up_rate": round(float((arr > 0).sum() / len(arr) * 100), 1),
            "avg_return": round(float(arr.mean()), 3),
        }
    return out


def aggregate_signal_stats(
    per_symbol_occurrences: dict[str, dict[str, list[dict]]],
    horizons=FORWARD_BARS,
    cost_pct: float = 0.0,
    per_symbol_closes: dict[str, "pd.Series"] | None = None,
) -> dict[str, dict]:
    """`per_symbol_occurrences` is {symbol: find_signal_occurrences(...)}.
    Pools every symbol's occurrences of each signal type and computes
    win-rate + average forward return at each horizon. A "win" is a
    positive forward return for a bullish-expectation signal, negative for
    a bearish-expectation one.

    Two honesty adjustments layered on top of the raw numbers:

    * **Costs.** `cost_pct` (a round trip's fees + slippage) is subtracted
      from every trade's realized return before the net stats are computed.
      A signal is only real if it survives this. Gross figures are kept
      alongside so the size of the drag is visible rather than hidden.
    * **Base rate.** When `per_symbol_closes` is supplied, the unconditional
      forward return over the same data is computed too, and each signal's
      edge is reported *relative to* it. A win rate that merely matches the
      base rate is not an edge, however good the absolute number looks.
    """
    base = _base_rates(per_symbol_closes, horizons) if per_symbol_closes else {}
    stats: dict[str, dict] = {}
    for name, expected_dir in SIGNAL_TYPES.items():
        by_horizon: dict[int, dict] = {}
        for h in horizons:
            returns = [
                occ["forward_returns"].get(h)
                for sym_occ in per_symbol_occurrences.values()
                for occ in sym_occ.get(name, [])
                if occ["forward_returns"].get(h) is not None
            ]
            if not returns:
                by_horizon[h] = {"n": 0, "win_rate": None, "avg_return": None,
                                 "net_win_rate": None, "net_avg_return": None,
                                 "edge_vs_base": None, "survives_costs": None}
                continue
            arr = np.array(returns)
            # Realized return in the direction the signal expects: a bearish
            # signal "wins" when price falls, so its realized return is the
            # negative of the raw move. Costs then subtract from that either
            # way -- you pay the spread whichever direction you took.
            realized = arr if expected_dir == "bullish" else -arr
            net = realized - cost_pct

            base_h = base.get(h) or {}
            base_rate = base_h.get("up_rate")
            if base_rate is not None and expected_dir == "bearish":
                base_rate = 100 - base_rate  # the unconditional DOWN rate

            win_rate = float((realized > 0).sum() / len(realized) * 100)
            net_win_rate = float((net > 0).sum() / len(net) * 100)

            by_horizon[h] = {
                "n": int(len(arr)),
                "win_rate": round(win_rate, 1),
                "avg_return": round(float(arr.mean()), 2),
                "net_win_rate": round(net_win_rate, 1),
                "net_avg_return": round(float(net.mean()), 3),
                "base_rate": round(base_rate, 1) if base_rate is not None else None,
                "edge_vs_base": round(win_rate - base_rate, 1) if base_rate is not None else None,
                "survives_costs": bool(net.mean() > 0),
            }
        stats[name] = {
            "label": SIGNAL_LABELS.get(name, name),
            "expected_direction": expected_dir,
            "by_horizon": by_horizon,
        }
    return {"signals": stats, "base_rates": base, "cost_pct": cost_pct}
