"""Trading-cost model.

This app never places an order -- it's an analysis tool. But every
"what would this have returned" number it shows (the signal backtest, a
simulated position's P&L) is meaningless without costs subtracted, because
the edge being measured is frequently smaller than the cost of capturing it.
A signal averaging +0.30% over 5 bars looks fine until you notice a
round-trip on Bybit perps costs ~0.11% in taker fees alone before slippage,
and that a low-timeframe strategy pays that cost far more often than a
swing strategy does.

So: every return figure in this app is reported NET, with the gross figure
kept alongside it so the size of the cost drag is visible rather than
hidden. Pure arithmetic, no I/O, trivially unit-testable.

A note on leverage: percentage returns are leverage-neutral. 10x leverage
multiplies both the gain and the cost by 10, so the *percentage* return on
notional -- which is what this whole app reports -- is unchanged. What
leverage actually changes is how much of your account a given percentage
move represents, which is why the UI frames costs in "% of account at Nx"
terms rather than silently rescaling anything.
"""
from __future__ import annotations


def round_trip_cost_pct(taker_fee_pct: float, slippage_pct: float) -> float:
    """Total cost of getting in and back out again, in percent of notional.
    Both sides pay fee + slippage."""
    return 2.0 * (taker_fee_pct + slippage_pct)


def net_return_pct(gross_return_pct: float | None, cost_pct: float) -> float | None:
    """Gross percentage return minus the round-trip cost. Direction-agnostic:
    `gross_return_pct` should already be signed the way the position was
    (a short that gained is positive), and cost always subtracts."""
    if gross_return_pct is None:
        return None
    return gross_return_pct - cost_pct


def breakeven_move_pct(cost_pct: float) -> float:
    """How far price has to move in your favour just to get back to flat.
    The single most useful number for judging whether a low-timeframe setup
    is worth taking at all: if the nearest resistance is 0.08% away and
    breakeven is 0.15%, the trade cannot work no matter how good the signal
    looks."""
    return cost_pct


def cost_in_account_terms_pct(cost_pct: float, leverage: float) -> float:
    """The round-trip cost expressed as a percentage of *account equity*
    rather than of notional, at a given leverage. This is the number that
    makes the drag feel real: 0.15% of notional at 10x is 1.5% of the
    account, per trade, win or lose."""
    return cost_pct * max(leverage, 0.0)


def summarize_costs(taker_fee_pct: float, slippage_pct: float, leverage: float) -> dict:
    """One dict with everything the UI needs to be honest about costs."""
    cost = round_trip_cost_pct(taker_fee_pct, slippage_pct)
    return {
        "taker_fee_pct": taker_fee_pct,
        "slippage_pct": slippage_pct,
        "round_trip_pct": round(cost, 4),
        "breakeven_move_pct": round(breakeven_move_pct(cost), 4),
        "leverage": leverage,
        "account_cost_pct": round(cost_in_account_terms_pct(cost, leverage), 3),
    }
