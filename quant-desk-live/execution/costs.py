"""Execution-specific cost helpers, layered on top of analysis/costs.py.
See config.EXECUTION_EXTRA_COST_BUFFER_PCT's docstring for why real order
decisions use a more conservative cost figure than the paper Tracker
Positions' cost_pct."""
from __future__ import annotations

from analysis.costs import round_trip_cost_pct


def execution_round_trip_cost_pct(taker_fee_pct: float, slippage_pct: float, extra_buffer_pct: float) -> float:
    """The measured round-trip cost (fee + slippage, both sides) plus the
    user-requested flat safety buffer on top -- see config.py."""
    return round_trip_cost_pct(taker_fee_pct, slippage_pct) + extra_buffer_pct


def reward_risk_ratio(entry_price: float, stop_price: float, target_price: float) -> float | None:
    risk = abs(entry_price - stop_price)
    reward = abs(target_price - entry_price)
    if risk <= 0:
        return None
    return reward / risk


def is_trade_cost_viable(entry_price: float, target_price: float, cost_pct: float, min_multiple: float = 1.5) -> tuple[bool, str]:
    """The setup must be able to pay for its own round trip (fees + slippage
    + the extra safety buffer) several times over before it's worth risking
    real capital on -- the same principle as the Spotlight cost-viability
    check in analysis/spotlight.py, applied here to a real order using the
    more conservative execution cost figure."""
    if entry_price <= 0:
        return False, "invalid entry price"
    move_pct = abs(target_price - entry_price) / entry_price * 100
    if move_pct < cost_pct * min_multiple:
        return False, (
            f"target move ({move_pct:.3f}%) doesn't clear {min_multiple}x the estimated round-trip cost "
            f"({cost_pct:.3f}%) -- this trade can't pay for itself with margin to spare"
        )
    return True, ""
