"""Turns a Spotlight symbol's full 1m-12h multi-timeframe readout into a
plain-English short-term read: which way the evidence leans, how much to
trust it right now, and the nearest levels that would confirm or invalidate
that read.

Pure function, no network/I/O -- same testing philosophy as
analysis/indicators.py. Deliberately NOT a black-box prediction: every
branch here is a readable rule over the same indicators already shown in
the timeframe table, so the headline text is always traceable back to a
specific number on the page rather than a hidden score. This is technical
analysis interpreted in plain English, not a guarantee of what happens
next -- see the disclaimer in the UI.
"""
from __future__ import annotations

# "Act on this in the next few minutes" -- the timeframes an actively
# managed, leveraged low-timeframe trade is actually entered/exited on.
ENTRY_TFS = ["1m", "3m", "5m", "10m"]
# The intraday/session trend a scalp should generally line up with.
MICRO_TFS = ["15m", "30m", "1h"]
# The bigger picture -- don't fight this without a real reason.
MACRO_TFS = ["4h", "12h"]
ALL_TFS = ENTRY_TFS + MICRO_TFS + MACRO_TFS

TF_GROUP_LABELS = {
    "entry": "entry timeframes (1m-10m)",
    "micro": "the 15m-1h session trend",
    "macro": "the 4h-12h bigger-picture trend",
}

# Nearest S/R and ATR are read off a timeframe fine enough to matter for a
# fast entry but not so fine it's mostly noise -- checked in this order,
# first one with data wins.
LEVEL_TF_PREFERENCE = ["15m", "5m", "30m", "1h"]
ATR_TF_PREFERENCE = ["5m", "15m", "1m"]


def _group_direction(conf: dict | None) -> str:
    if not conf or not conf.get("total"):
        return "neutral"
    return conf.get("direction", "neutral")


def _caution_reasons(tf_summaries: dict[str, dict], rsi_overbought: float, rsi_oversold: float) -> list[str]:
    """Any RSI extreme or RSI/price divergence on a timeframe the user would
    actually be acting on (entry + micro horizons) is genuine uncertainty
    that must win over whatever the trend/confluence read says -- the same
    "uncertainty wins" invariant analysis/indicators.py's score_setup()
    enforces for the rest of the desk, just checked across more timeframes
    here since Spotlight looks at so many of them at once. A single flagged
    timeframe is enough to trigger this (no threshold-counting), matching
    how score_setup treats its own single 1D reading."""
    reasons = []
    for tf in ENTRY_TFS + MICRO_TFS:
        tf_data = tf_summaries.get(tf)
        if not tf_data:
            continue
        rsi14 = tf_data.get("rsi14")
        if rsi14 is not None and rsi14 >= rsi_overbought:
            reasons.append(f"{tf} RSI {rsi14:.0f} overbought")
        elif rsi14 is not None and rsi14 <= rsi_oversold:
            reasons.append(f"{tf} RSI {rsi14:.0f} oversold")
        div = tf_data.get("divergence")
        if div is not None:
            reasons.append(f"{tf} {div['kind']} divergence")
    return reasons


def _nearest_level(tf_summaries: dict[str, dict], key: str) -> dict | None:
    """The closest level to current price. `cluster_levels()` now sorts by
    proximity, so [0] genuinely is the nearest -- it used to be sorted by
    touch count, which meant this returned a distant-but-well-tested level
    under a "nearest" label. That mattered because this is the number
    someone eyeballs for stop placement."""
    for tf in LEVEL_TF_PREFERENCE:
        levels = (tf_summaries.get(tf) or {}).get(key) or []
        if levels:
            return {**levels[0], "timeframe": tf}
    return None


def _strongest_level(tf_summaries: dict[str, dict], key: str) -> dict | None:
    """The best-tested level across the preferred timeframes -- where price
    is most likely to actually stop, as opposed to the next one it meets."""
    best = None
    for tf in LEVEL_TF_PREFERENCE:
        for lvl in (tf_summaries.get(tf) or {}).get(key) or []:
            candidate = {**lvl, "timeframe": tf}
            if best is None or (candidate.get("touches", 0), -abs(candidate.get("distance_pct", 0.0))) > \
                                (best.get("touches", 0), -abs(best.get("distance_pct", 0.0))):
                best = candidate
    return best


def _cost_viability(nearest_resistance: dict | None, nearest_support: dict | None,
                     bias: str, cost_pct: float) -> dict | None:
    """Is there even enough room between here and the next level to cover
    the round trip?

    This is the check that low-timeframe traders skip most often and pay for
    most reliably: if the nearest resistance is 0.08% away and a round trip
    costs 0.15%, the setup cannot be profitable no matter how clean the
    technicals look. Cheap to compute, and it disqualifies trades before
    they're taken rather than after.
    """
    if cost_pct <= 0:
        return None
    target = nearest_resistance if bias == "bullish" else (nearest_support if bias == "bearish" else None)
    if not target:
        return None
    room_pct = abs(target.get("distance_pct") or 0.0)
    ratio = room_pct / cost_pct if cost_pct else 0.0
    if ratio < 1.0:
        verdict, note = "not-viable", (
            f"The nearest level in your favour is only {room_pct:.2f}% away but a round trip costs "
            f"{cost_pct:.2f}% — this move cannot pay for itself even if it works perfectly."
        )
    elif ratio < 3.0:
        verdict, note = "thin", (
            f"Only {room_pct:.2f}% of room to the nearest level against {cost_pct:.2f}% of round-trip cost "
            f"({ratio:.1f}x) — thin margin, so the setup has to be near-perfect to be worth it."
        )
    else:
        verdict, note = "adequate", (
            f"{room_pct:.2f}% of room to the nearest level versus {cost_pct:.2f}% round-trip cost "
            f"({ratio:.1f}x) — enough room for the move to cover its own costs."
        )
    return {"room_pct": round(room_pct, 3), "cost_pct": round(cost_pct, 3),
            "ratio": round(ratio, 2), "verdict": verdict, "note": note}


def _atr_reference(tf_summaries: dict[str, dict]) -> dict | None:
    for tf in ATR_TF_PREFERENCE:
        atr_val = (tf_summaries.get(tf) or {}).get("atr")
        if atr_val is not None:
            return {"timeframe": tf, "value": atr_val}
    return None


_ACTION_WORD = {"bullish": "long", "bearish": "short"}  # matches Tracker Positions' long/short terminology


def _build_headline(symbol: str, pattern: str, entry_dir: str, micro_dir: str, macro_dir: str,
                     trend_override: bool, caution_reasons: list[str]) -> str:
    action = _ACTION_WORD.get(entry_dir, entry_dir)
    if pattern == "insufficient_data":
        return f"{symbol}: still gathering enough {TF_GROUP_LABELS['entry']} history to read momentum — check back in a moment."

    if trend_override:
        reason_text = "; ".join(caution_reasons)
        agree_note = " Momentum still reads aligned across timeframes, but" if pattern == "aligned" else " And"
        return (
            f"{symbol}: {reason_text}.{agree_note} this is a stretched/uncertain moment, not a clean "
            f"continuation signal — the higher-probability next move is a pullback, cool-off, or chop "
            f"before either direction resolves. Not the moment to add fresh risk; let it settle first."
        )

    if pattern == "no_signal":
        return (
            f"{symbol}: no clean short-term edge right now — {TF_GROUP_LABELS['entry']} are mixed or "
            f"neutral. Better to wait for a cleaner alignment than force a trade into chop."
        )

    if pattern == "choppy":
        return (
            f"{symbol}: {TF_GROUP_LABELS['entry']} lean {entry_dir}, but that's fighting {TF_GROUP_LABELS['micro']} "
            f"which leans {micro_dir} — the immediate move and the session trend disagree, a classic setup "
            f"for a fakeout. Low-conviction chop until these two line up."
        )

    if pattern == "counter_trend":
        return (
            f"{symbol}: {TF_GROUP_LABELS['entry']} and {TF_GROUP_LABELS['micro']} both lean {entry_dir}, but "
            f"{TF_GROUP_LABELS['macro']} is still {macro_dir} — this reads as a counter-trend move, not a "
            f"reversal. Treat it as a quick, tightly-managed {action} against the bigger trend, "
            f"not something to hold through a pullback."
        )

    # pattern == "aligned"
    if macro_dir == entry_dir:
        tail = (
            f"This is the cleanest kind of setup for a fast, leveraged {action} — momentum, the session "
            f"trend, and the bigger picture are all pointing the same way right now."
        )
        macro_note = f", with {TF_GROUP_LABELS['macro']} agreeing too"
    elif macro_dir == "neutral":
        tail = (
            f"Good short-term alignment for a quick {action} — just without a strong macro tailwind "
            f"behind it, so keep it tactical rather than treating it as a trend change."
        )
        macro_note = f" — {TF_GROUP_LABELS['macro']} isn't offering strong confirmation either way"
    else:
        # shouldn't normally reach here (macro disagreeing routes to counter_trend), kept as a safe fallback
        tail = f"Short-term alignment for a quick {action}, but keep size modest given the mixed bigger picture."
        macro_note = ""
    return f"{symbol}: {TF_GROUP_LABELS['entry']} and {TF_GROUP_LABELS['micro']} both lean {entry_dir}{macro_note}. {tail}"


def interpret_spotlight(
    *,
    symbol: str,
    tf_summaries: dict[str, dict],  # tf -> {rsi14, macd, bollinger, stochastic, atr, vwap, divergence, support, resistance, ...}
    entry_confluence: dict,
    micro_confluence: dict,
    macro_confluence: dict,
    overall_confluence: dict,
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    positioning: dict | None = None,
    cost_pct: float = 0.0,
) -> dict:
    """The single entry point: fold everything Spotlight computed for one
    symbol into a `{headline, bias, confidence, pattern, caution_reasons,
    key_levels, positioning_note, cost_check}` dict the UI renders directly."""
    if not entry_confluence or not entry_confluence.get("total"):
        return {
            "headline": _build_headline(symbol, "insufficient_data", "neutral", "neutral", "neutral", False, []),
            "bias": "two-sided",
            "confidence": "low",
            "pattern": "insufficient_data",
            "caution_reasons": [],
            "key_levels": {"nearest_support": None, "nearest_resistance": None,
                            "strongest_support": None, "strongest_resistance": None, "atr_reference": None},
            "positioning_note": None,
            "cost_check": None,
        }

    entry_dir = _group_direction(entry_confluence)
    micro_dir = _group_direction(micro_confluence)
    macro_dir = _group_direction(macro_confluence)

    caution_reasons = _caution_reasons(tf_summaries, rsi_overbought, rsi_oversold)
    trend_override = bool(caution_reasons)

    if entry_dir == "neutral":
        pattern = "no_signal"
    elif micro_dir != "neutral" and entry_dir != micro_dir:
        pattern = "choppy"
    elif macro_dir != "neutral" and entry_dir != macro_dir:
        pattern = "counter_trend"
    else:
        pattern = "aligned"

    if trend_override:
        bias, confidence = "two-sided", "low"
    elif pattern in ("no_signal", "choppy"):
        bias, confidence = "two-sided", "low"
    elif pattern == "counter_trend":
        bias = entry_dir
        confidence = "low" if macro_confluence.get("label", "").startswith("Strong") else "moderate"
    else:  # aligned
        bias = entry_dir
        confidence = "high" if overall_confluence.get("label", "").startswith("Strong") else "moderate"

    headline = _build_headline(symbol, pattern, entry_dir, micro_dir, macro_dir, trend_override, caution_reasons)

    # Positioning (funding / OI / crowd / liquidations) is genuinely
    # orthogonal to everything above -- price-derived indicators can't see it.
    # It gets to *downgrade* confidence when it contradicts the technical
    # read, but never to upgrade it: agreement between a fast technical
    # signal and a slow positioning signal is reassuring, not additive, and
    # inflating confidence on that basis is exactly the kind of overreach
    # this desk avoids everywhere else.
    positioning_note = None
    if positioning and positioning.get("lean") not in (None, "neutral"):
        p_lean, p_conf = positioning["lean"], positioning.get("confidence", "low")
        if bias in ("bullish", "bearish") and p_lean in ("bullish", "bearish"):
            if p_lean == bias:
                positioning_note = (
                    f"Positioning agrees ({p_lean}, {p_conf} confidence): {positioning.get('summary', '')}"
                )
            else:
                positioning_note = (
                    f"⚠ Positioning DISAGREES with the technical read — technicals lean {bias} but positioning "
                    f"leans {p_lean} ({p_conf} confidence). {positioning.get('summary', '')} When the chart and "
                    f"the leverage data point opposite ways, the chart is usually the one that's early."
                )
                if p_conf in ("high", "moderate") and confidence == "high":
                    confidence = "moderate"
                elif p_conf == "high" and confidence == "moderate":
                    confidence = "low"
        else:
            positioning_note = positioning.get("summary")

    nearest_support = _nearest_level(tf_summaries, "support")
    nearest_resistance = _nearest_level(tf_summaries, "resistance")

    return {
        "headline": headline,
        "bias": bias,
        "confidence": confidence,
        "pattern": pattern,
        "caution_reasons": caution_reasons,
        "key_levels": {
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "strongest_support": _strongest_level(tf_summaries, "support"),
            "strongest_resistance": _strongest_level(tf_summaries, "resistance"),
            "atr_reference": _atr_reference(tf_summaries),
        },
        "positioning_note": positioning_note,
        "cost_check": _cost_viability(nearest_resistance, nearest_support, bias, cost_pct),
    }
