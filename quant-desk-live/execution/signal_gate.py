"""The entry decision: should the desk open a REAL position on `symbol`
right now? Two independent layers, both must pass:

1. **Technical trigger** -- per the user's own framing (2026-08-30):
   "relatively good confluence into the direction that the bias is". The
   watchlist's score_setup() direction must be a real bias ("bullish bias" /
   "bearish bias", never "two-sided" -- that already means the RSI-extreme/
   divergence uncertainty gate in analysis/indicators.py is active, and that
   gate always wins), AND the independently-computed multi-timeframe
   confluence() must agree with that direction, AND agree well enough
   (config.EXECUTION_MIN_CONFLUENCE_RATIO / _MIN_CONFLUENCE_SCORE).

2. **Signal-performance gate** -- consult the desk's OWN trading history
   (paper Tracker Positions + real live_positions.json, combined) for this
   exact kind of setup before acting on it. Requested by the user
   2026-08-30 ("base the strategy on what we have learned so far"). A
   bucket only blocks or is trusted once it has at least
   config.EXECUTION_MIN_BUCKET_N closed trades -- below that it's treated as
   "no information yet", not "bad", since as of 2026-08-30 the paper book
   only has 46 closed trades total and most fine-grained buckets are thinner
   than that. As of that same date this actually matters: the "bullish
   bias/Low" bucket (n=21) has a strong positive edge, several bearish
   buckets are flat-to-negative, and shorts overall have underperformed
   longs -- see the project doc's v8 section for the full numbers. This
   gate is what lets the live desk act on that rather than a static
   confidence-tier heuristic.

Deliberately does NOT touch Spotlight, the historical backtest, or the
Signal Scorecard's own bucketing (positions.py) -- see the project doc's
"three separate scoring paths, deliberately not merged" section. This is a
fourth, independent consumer of the watchlist's score_setup()/confluence()
output, reading the paper book for evidence but never writing to it or
changing its methodology.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    approved: bool
    side: str | None  # "long" | "short" | None
    reason: str
    size_multiplier: float = 1.0
    debug: dict = field(default_factory=dict)


def _bias_side(direction: str) -> str | None:
    if direction == "bullish bias":
        return "long"
    if direction == "bearish bias":
        return "short"
    return None  # "two-sided" (uncertainty gate active) or unset


def _paper_bucket_stats(bucket_key: str) -> dict:
    """Scans the paper Tracker Positions book (positions.py) for closed
    positions matching `bucket_key` -- either a "watchlist:<direction>/
    <likelihood>" key (same shape positions.py's own scorecard uses) or a
    "confluence:<agree>/<total>" key (a dimension the paper scorecard UI
    doesn't track, but the raw signal_context already carries via
    confluence_agree, so it costs nothing to check here).

    Deliberately reimplemented here rather than importing positions.py's
    private `_bucket()` helper: that function returns exactly one key per
    position (direction/likelihood OR spotlight), whereas this gate wants to
    check confluence-agreement as an independent second cut on the same
    trade history."""
    from positions import position_book  # local import: avoids a hard dependency at module load time

    nets = []
    for p in position_book.positions.values():
        if p.status != "closed":
            continue
        ctx = p.signal_context or {}
        keys = []
        direction, likelihood = ctx.get("direction"), ctx.get("likelihood")
        if direction and likelihood:
            keys.append(f"watchlist:{direction}/{likelihood}")
        agree = ctx.get("confluence_agree")
        if agree:
            keys.append(f"confluence:{agree}")
        if bucket_key in keys:
            n = p.net_pnl_pct()
            if n is not None:
                nets.append(n)
    if not nets:
        return {"count": 0, "win_rate": None, "avg_net_pnl_pct": None}
    wins = sum(1 for r in nets if r > 0)
    return {"count": len(nets), "win_rate": round(wins / len(nets) * 100, 1), "avg_net_pnl_pct": round(sum(nets) / len(nets), 3)}


def combined_bucket_stats(bucket_key: str) -> dict:
    """Paper history + real live-trade history, pooled. Both are genuine
    forward tests of the desk's own signal (recorded before the outcome
    exists) -- the only difference is one used fictional capital. Pooling
    them means the live gate's evidence base grows as real trades close,
    rather than starting from zero and ignoring 46+ paper trades of prior
    art on the exact same signals."""
    from execution.live_positions import live_position_book

    paper = _paper_bucket_stats(bucket_key)
    live = live_position_book.stats_for_bucket(bucket_key)
    n = (paper["count"] or 0) + (live["count"] or 0)
    if n == 0:
        return {"count": 0, "win_rate": None, "avg_net_pnl_pct": None, "paper_n": 0, "live_n": 0}
    total_return = (paper["avg_net_pnl_pct"] or 0.0) * paper["count"] + (live["avg_net_pnl_pct"] or 0.0) * live["count"]
    wins = round((paper["win_rate"] or 0.0) / 100 * paper["count"]) + round((live["win_rate"] or 0.0) / 100 * live["count"])
    return {
        "count": n, "win_rate": round(wins / n * 100, 1), "avg_net_pnl_pct": round(total_return / n, 3),
        "paper_n": paper["count"], "live_n": live["count"],
    }


def _track_record_check(direction: str, likelihood: str, agree: int, total: int, min_bucket_n: int) -> tuple[bool, str]:
    """Returns (blocked, reason). Checks both bucket cuts (direction/
    likelihood, and confluence agreement) independently -- either one having
    enough sample and a non-positive edge is enough to skip the trade."""
    for key in (f"watchlist:{direction}/{likelihood}", f"confluence:{agree}/{total}"):
        stats = combined_bucket_stats(key)
        if stats["count"] < min_bucket_n:
            continue  # not enough evidence yet either way -- don't block on it
        if stats["win_rate"] is not None and stats["win_rate"] < 50:
            return True, (
                f"signal-performance gate: '{key}' has historically lost more often than won "
                f"(n={stats['count']}, win rate {stats['win_rate']}%, avg net {stats['avg_net_pnl_pct']}%) -- skipping"
            )
        if stats["avg_net_pnl_pct"] is not None and stats["avg_net_pnl_pct"] <= 0:
            return True, (
                f"signal-performance gate: '{key}' has a non-positive average net return "
                f"(n={stats['count']}, avg {stats['avg_net_pnl_pct']}%) -- skipping"
            )
    return False, ""


def evaluate_entry(symbol: str, st, cfg) -> GateResult:
    """`st` is the SymbolState for `symbol` (state.py). Pure w.r.t. its
    inputs except for reading the two position books for the performance
    gate -- no network calls, no order placement, safe to call on every
    deep-refresh cycle for every watchlist symbol."""
    side = _bias_side(st.direction)
    if side is None:
        return GateResult(False, None, f"no directional bias ('{st.direction}') -- uncertainty gate active or genuinely flat")

    conf = st.confluence
    if not conf:
        return GateResult(False, None, "no confluence data yet (waiting on next deep refresh)")

    expected_conf_dir = "bullish" if side == "long" else "bearish"
    if conf.get("direction") != expected_conf_dir:
        return GateResult(False, None, f"confluence direction ('{conf.get('direction')}') does not confirm the {side} bias")

    agree, total = conf.get("agree", 0) or 0, conf.get("total", 0) or 0
    ratio = (agree / total) if total else 0.0
    if total == 0 or ratio < cfg.EXECUTION_MIN_CONFLUENCE_RATIO:
        return GateResult(False, None, f"confluence agreement too weak ({agree}/{total}, need >= {cfg.EXECUTION_MIN_CONFLUENCE_RATIO:.0%})")

    score = abs(conf.get("score", 0.0) or 0.0)
    if score < cfg.EXECUTION_MIN_CONFLUENCE_SCORE:
        return GateResult(False, None, f"confluence score {score:.2f} below minimum {cfg.EXECUTION_MIN_CONFLUENCE_SCORE:.2f}")

    blocked, reason = _track_record_check(st.direction, st.likelihood, agree, total, cfg.EXECUTION_MIN_BUCKET_N)
    if blocked:
        return GateResult(False, None, reason)

    return GateResult(
        True, side, f"bias={st.direction}, confluence={agree}/{total} (score {score:.2f}) -- entry criteria met",
        debug={"agree": agree, "total": total, "score": score, "likelihood": st.likelihood},
    )
