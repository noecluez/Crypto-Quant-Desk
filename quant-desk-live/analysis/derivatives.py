"""Positioning analysis: funding rates, open interest, long/short account
ratio and liquidations.

This is the module that adds information the price chart doesn't already
contain. Everything in analysis/indicators.py -- RSI, MACD, Bollinger,
Stochastic -- is a transformation of the same price series, so those
indicators agreeing with each other is much weaker evidence than it looks.
Funding, open interest and liquidations are different: they describe how
*positioned* the market is, which is why they can tell you a rally is
running on new conviction rather than on shorts being forced out, and why
they can flag a crowded trade before price says anything at all.

All of it is free and keyless from Bybit's public v5 endpoints, and all of
it only exists on the derivatives (linear/inverse) categories -- spot has
no funding, no open interest and no liquidations. When the app is pointed
at spot these functions are simply never called and the UI says so plainly
rather than inventing numbers.

Pure functions: data in, plain dicts + plain English out. No I/O.
"""
from __future__ import annotations

import time

# Bybit funds perpetuals every 8 hours -> 3 funding events a day.
FUNDING_PERIODS_PER_YEAR = 3 * 365


# ---------------------------------------------------------------------------
# Funding rate
# ---------------------------------------------------------------------------

def annualize_funding(funding_rate: float) -> float:
    """Bybit reports funding as a per-period fraction (e.g. 0.0001 = 0.01%
    per 8h). Annualizing makes the number legible: 0.01%/8h is ~11%/yr,
    which is roughly the long-run neutral level, while 0.1%/8h is ~110%/yr,
    which is a market paying dearly to stay long."""
    return funding_rate * FUNDING_PERIODS_PER_YEAR * 100


def interpret_funding(
    funding_rate: float | None,
    *,
    extreme_annual_pct: float = 30.0,
    elevated_annual_pct: float = 15.0,
    history: list[float] | None = None,
) -> dict | None:
    """Who is paying whom, how crowded that is, and what it implies.

    Positive funding = longs pay shorts = more leveraged long demand than
    short. Sustained high positive funding means a crowded long book, which
    is *fuel for a downside squeeze*: those positions are paying rent to
    stay open and get liquidated first on a flush. Negative funding is the
    mirror image and often precedes short squeezes.

    Note this is a positioning read, not a timing signal. Crowded can stay
    crowded for a long while, which is why the summary below deliberately
    talks about elevated *risk* of a squeeze rather than predicting one.
    """
    if funding_rate is None:
        return None

    annual = annualize_funding(funding_rate)
    per_period_pct = funding_rate * 100
    side = "longs" if funding_rate > 0 else ("shorts" if funding_rate < 0 else "neither side")
    crowded_side = "long" if funding_rate > 0 else ("short" if funding_rate < 0 else "balanced")

    abs_annual = abs(annual)
    if abs_annual >= extreme_annual_pct:
        level, squeeze_risk = "extreme", "high"
    elif abs_annual >= elevated_annual_pct:
        level, squeeze_risk = "elevated", "moderate"
    else:
        level, squeeze_risk = "normal", "low"

    trend = None
    if history and len(history) >= 3:
        recent = sum(history[-3:]) / 3
        earlier = sum(history[:-3]) / max(len(history) - 3, 1)
        if abs(earlier) > 1e-12:
            if recent > earlier * 1.25:
                trend = "rising"
            elif recent < earlier * 0.75:
                trend = "falling"
            else:
                trend = "steady"

    if level == "normal":
        summary = (
            f"Funding is unremarkable at {per_period_pct:+.4f}% per 8h ({annual:+.1f}% annualized) — "
            f"leveraged positioning looks balanced, so there's no crowded-trade risk showing here."
        )
    else:
        squeeze_dir = "downside" if funding_rate > 0 else "upside"
        summary = (
            f"Funding is {level} at {per_period_pct:+.4f}% per 8h ({annual:+.1f}% annualized) — "
            f"{side} are paying to hold, meaning the {crowded_side} side is crowded. Positions paying "
            f"that much rent are the first to be forced out, so this raises the risk of a {squeeze_dir} "
            f"squeeze. It doesn't say when: crowded can stay crowded."
        )
    if trend == "rising" and level != "normal":
        summary += " It's also been climbing recently, so the crowding is building rather than unwinding."
    elif trend == "falling" and level != "normal":
        summary += " It has been easing off recently, so some of that crowding is already unwinding."

    return {
        "rate": funding_rate,
        "per_period_pct": round(per_period_pct, 6),
        "annual_pct": round(annual, 2),
        "level": level,
        "crowded_side": crowded_side,
        "paying_side": side,
        "squeeze_risk": squeeze_risk,
        "trend": trend,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Open interest vs. price -- the four-quadrant read
# ---------------------------------------------------------------------------

_OI_QUADRANTS = {
    ("up", "up"): (
        "new_money_long",
        "Price up on rising open interest — new long positions are being opened into the move. "
        "That's real conviction rather than a bounce on closing shorts, and it's the healthier "
        "kind of rally, though it also builds the position pile that a later flush would hit.",
    ),
    ("up", "down"): (
        "short_covering",
        "Price up on FALLING open interest — this move is shorts closing out, not new buyers "
        "arriving. Short-covering rallies tend to run out of fuel once the trapped shorts are "
        "done, so treat strength here with more suspicion than the chart alone suggests.",
    ),
    ("down", "up"): (
        "new_money_short",
        "Price down on rising open interest — new short positions are being opened into the "
        "decline. That's genuine bearish conviction rather than longs giving up, and it tends "
        "to have more follow-through than a simple long flush.",
    ),
    ("down", "down"): (
        "long_liquidation",
        "Price down on FALLING open interest — this is longs closing or being liquidated rather "
        "than new sellers arriving. These flushes are often sharp but self-limiting: once the "
        "leveraged longs are cleared out the selling pressure tends to dry up.",
    ),
}


def interpret_open_interest(
    oi_now: float | None,
    oi_prev: float | None,
    price_change_pct: float | None,
    *,
    flat_threshold_pct: float = 0.5,
) -> dict | None:
    """The single most useful derivatives read there is: the same price move
    means opposite things depending on whether open interest is rising or
    falling with it. Rising OI = new positions opening; falling OI =
    positions closing.

    `flat_threshold_pct` keeps small wiggles from being read as signal in
    either dimension.
    """
    if oi_now is None or oi_prev is None or price_change_pct is None or oi_prev <= 0:
        return None

    oi_change_pct = (oi_now - oi_prev) / oi_prev * 100

    price_dir = "up" if price_change_pct > flat_threshold_pct else ("down" if price_change_pct < -flat_threshold_pct else "flat")
    oi_dir = "up" if oi_change_pct > flat_threshold_pct else ("down" if oi_change_pct < -flat_threshold_pct else "flat")

    if price_dir == "flat" or oi_dir == "flat":
        pattern = "inconclusive"
        summary = (
            f"Open interest {oi_change_pct:+.2f}% against a {price_change_pct:+.2f}% price move — "
            f"neither is decisive enough to read positioning from. No signal here either way."
        )
    else:
        pattern, summary = _OI_QUADRANTS[(price_dir, oi_dir)]

    # Which direction the flow *implies*, distinct from which way price went.
    implication = {
        "new_money_long": "bullish",
        "new_money_short": "bearish",
        "short_covering": "fading-bullish",
        "long_liquidation": "fading-bearish",
        "inconclusive": "neutral",
    }[pattern]

    return {
        "open_interest": oi_now,
        "previous": oi_prev,
        "change_pct": round(oi_change_pct, 3),
        "price_change_pct": round(price_change_pct, 3),
        "pattern": pattern,
        "implication": implication,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Long / short account ratio
# ---------------------------------------------------------------------------

def interpret_long_short_ratio(buy_ratio: float | None, sell_ratio: float | None,
                                *, skew_threshold: float = 0.60) -> dict | None:
    """Bybit's account-ratio endpoint reports what fraction of accounts are
    positioned long vs. short. Read as a contrarian gauge: when a very large
    majority of retail accounts sit on one side, that side is the one with
    stops to hunt. Weak on its own -- meaningful when it agrees with funding.
    """
    if buy_ratio is None or sell_ratio is None:
        return None
    total = buy_ratio + sell_ratio
    if total <= 0:
        return None
    long_pct = buy_ratio / total * 100
    short_pct = sell_ratio / total * 100

    if long_pct >= skew_threshold * 100:
        skew, contrarian = "long-heavy", "bearish"
        summary = (
            f"{long_pct:.0f}% of accounts are positioned long. A crowd this one-sided is where "
            f"the stops are, so as a contrarian gauge this leans bearish — though crowd positioning "
            f"is a weak signal alone and only really matters when funding agrees with it."
        )
    elif short_pct >= skew_threshold * 100:
        skew, contrarian = "short-heavy", "bullish"
        summary = (
            f"{short_pct:.0f}% of accounts are positioned short. That's a crowded short book, which "
            f"as a contrarian gauge leans bullish — a squeeze needs trapped shorts, and here they "
            f"exist. Weak alone; stronger if funding is negative too."
        )
    else:
        skew, contrarian = "balanced", "neutral"
        summary = f"Accounts are split {long_pct:.0f}% long / {short_pct:.0f}% short — no meaningful crowd skew."

    return {
        "long_pct": round(long_pct, 1),
        "short_pct": round(short_pct, 1),
        "skew": skew,
        "contrarian_lean": contrarian,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Liquidations
# ---------------------------------------------------------------------------

def summarize_liquidations(events: list[dict], *, window_minutes: int = 15,
                            cascade_usd: float = 1_000_000, now: float | None = None) -> dict:
    """`events` are raw liquidation records: {ts, side, price, qty, usd}.

    `side` follows Bybit's convention -- it's the side of the *order that
    closed the position*, so a "Buy" liquidation order means a SHORT position
    was liquidated, and "Sell" means a LONG was. That inversion is easy to
    get backwards and completely flips the interpretation, so it's resolved
    here once, in one place.

    A cascade is the defining mechanic of leveraged crypto: forced closes
    push price, which forces more closes. Seeing one live is the difference
    between "price dropped 2%" and "price dropped 2% because 8 million of
    longs got force-closed in four minutes."
    """
    now = now if now is not None else time.time()
    cutoff = now - window_minutes * 60
    recent = [e for e in events if e.get("ts", 0) >= cutoff]

    long_usd = sum(e.get("usd", 0.0) for e in recent if e.get("liquidated_side") == "long")
    short_usd = sum(e.get("usd", 0.0) for e in recent if e.get("liquidated_side") == "short")
    total_usd = long_usd + short_usd

    dominant = "long" if long_usd > short_usd else ("short" if short_usd > long_usd else None)
    cascade = total_usd >= cascade_usd

    if not recent:
        summary = f"No liquidations in the last {window_minutes} minutes — no forced-exit pressure right now."
    elif cascade and dominant == "long":
        summary = (
            f"Liquidation cascade underway: ~${total_usd:,.0f} force-closed in {window_minutes} min, "
            f"mostly LONGS (${long_usd:,.0f}). Leveraged longs are being flushed, which is what makes "
            f"drops accelerate — but these are self-limiting, and the bottom of a cascade is often "
            f"where the move exhausts."
        )
    elif cascade and dominant == "short":
        summary = (
            f"Liquidation cascade underway: ~${total_usd:,.0f} force-closed in {window_minutes} min, "
            f"mostly SHORTS (${short_usd:,.0f}). Trapped shorts are being forced to buy back, which "
            f"is what makes squeezes accelerate — and what makes chasing them late so expensive."
        )
    else:
        side_txt = f"mostly {dominant}s" if dominant else "evenly split"
        summary = (
            f"~${total_usd:,.0f} liquidated in the last {window_minutes} min ({side_txt}) — elevated "
            f"but below cascade levels. Normal churn for a leveraged market."
        )

    return {
        "window_minutes": window_minutes,
        "count": len(recent),
        "total_usd": round(total_usd, 2),
        "long_usd": round(long_usd, 2),
        "short_usd": round(short_usd, 2),
        "dominant_side": dominant,
        "cascade": cascade,
        "summary": summary,
        "recent": sorted(recent, key=lambda e: e.get("ts", 0), reverse=True)[:20],
    }


# ---------------------------------------------------------------------------
# Combined positioning read
# ---------------------------------------------------------------------------

def positioning_read(funding: dict | None, oi: dict | None,
                      ls_ratio: dict | None, liquidations: dict | None) -> dict:
    """Folds the four positioning inputs into one directional lean plus a
    plain-English paragraph.

    Deliberately conservative about confidence: these signals are genuinely
    orthogonal to price-derived indicators, but they're also slow and noisy,
    so a single one firing is explicitly not treated as a call. Real weight
    only accrues when funding and the crowd agree -- that's the classic
    crowded-trade setup.
    """
    votes = []       # (direction, weight, reason)

    if funding and funding["level"] != "normal":
        # Crowding is contrarian: a crowded long book is a bearish risk.
        direction = "bearish" if funding["crowded_side"] == "long" else "bullish"
        weight = 2.0 if funding["level"] == "extreme" else 1.0
        votes.append((direction, weight, f"{funding['level']} funding ({funding['crowded_side']} side crowded)"))

    if oi and oi["implication"] not in ("neutral",):
        mapping = {
            "bullish": ("bullish", 1.5),
            "bearish": ("bearish", 1.5),
            "fading-bullish": ("bearish", 0.75),   # rally on closing shorts -> weak
            "fading-bearish": ("bullish", 0.75),   # flush on closing longs -> self-limiting
        }
        direction, weight = mapping[oi["implication"]]
        votes.append((direction, weight, oi["pattern"].replace("_", " ")))

    if ls_ratio and ls_ratio["contrarian_lean"] != "neutral":
        votes.append((ls_ratio["contrarian_lean"], 0.5, f"crowd {ls_ratio['skew']}"))

    if liquidations and liquidations.get("cascade") and liquidations.get("dominant_side"):
        # A cascade is exhaustion-flavoured: mass long liquidation often
        # marks capitulation rather than the start of more downside.
        direction = "bullish" if liquidations["dominant_side"] == "long" else "bearish"
        votes.append((direction, 1.0, f"{liquidations['dominant_side']} liquidation cascade"))

    if not votes:
        return {
            "lean": "neutral", "score": 0.0, "confidence": "low", "drivers": [],
            "summary": "Positioning data is unremarkable right now — funding, open interest and the "
                       "crowd are all near neutral, so there's nothing here that price isn't already telling you.",
        }

    score = sum(w if d == "bullish" else -w for d, w, _ in votes)
    total_weight = sum(w for _, w, _ in votes)
    normalized = score / total_weight if total_weight else 0.0

    if normalized > 0.34:
        lean = "bullish"
    elif normalized < -0.34:
        lean = "bearish"
    else:
        lean = "mixed"

    # Confidence rises only when several independent inputs agree.
    agreeing = sum(1 for d, _, _ in votes if (d == "bullish") == (score > 0))
    if lean != "mixed" and agreeing >= 3:
        confidence = "high"
    elif lean != "mixed" and agreeing >= 2:
        confidence = "moderate"
    else:
        confidence = "low"

    drivers = [reason for _, _, reason in votes]
    if lean == "mixed":
        summary = ("Positioning signals disagree with each other (" + "; ".join(drivers) + ") — "
                   "no clean positioning edge, so weight the price action instead.")
    else:
        summary = (f"Positioning leans {lean} ({confidence} confidence), driven by: " + "; ".join(drivers) +
                   ". This is a slow, contrarian-flavoured read about how the market is positioned — "
                   "it says nothing about timing on its own.")

    return {
        "lean": lean,
        "score": round(normalized, 2),
        "confidence": confidence,
        "drivers": drivers,
        "summary": summary,
    }
