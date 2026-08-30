"""Pure risk math (position sizing, leverage selection, rounding to exchange
step sizes) plus the daily-loss circuit breaker. No network calls here --
this module takes numbers in and returns numbers out, which is what makes it
cheap to unit-test exhaustively (this is the code a sizing bug would live in,
so it gets the most test coverage of anything in execution/).
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_CB_PATH = Path(__file__).parent / "circuit_breaker.json"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _decimals_from_step(step: float) -> int:
    """0.001 -> 3, 1.0 -> 0, 10 -> 0. Used to format qty/price strings to
    exactly the precision Bybit's instrument-info reports -- sending more
    decimal places than the step allows gets the order rejected outright."""
    if step <= 0:
        return 8
    s = f"{step:.10f}".rstrip("0")
    if "." not in s:
        return 0
    return len(s.split(".")[1])


def round_step(value: float, step: float, mode: str = "floor") -> float:
    """Round `value` to the nearest multiple of `step`. `mode="floor"` for
    quantities (never send a qty that implies MORE risk than sized -- rounding
    up would). `mode="nearest"` for prices (SL/TP just need to land on a
    valid tick, direction doesn't systematically matter)."""
    if step <= 0:
        return value
    n = value / step
    if mode == "floor":
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(n * step, 10)


def format_step(value: float, step: float, mode: str = "nearest") -> str:
    """Round to `step` and format as the exact-precision string Bybit wants."""
    rounded = round_step(value, step, mode=mode)
    decimals = _decimals_from_step(step)
    return f"{rounded:.{decimals}f}"


@dataclass
class SizingResult:
    ok: bool
    qty: float = 0.0
    qty_str: str = "0"
    notional_usdt: float = 0.0
    margin_usdt: float = 0.0
    risk_usdt: float = 0.0
    reason: str = ""


def position_qty_from_risk(
    *, equity_usdt: float, risk_pct: float, entry_price: float, stop_price: float,
    leverage: float, qty_step: float, min_qty: float, min_notional_usdt: float = 5.0,
) -> SizingResult:
    """The core sizing rule: position size is chosen so that a full stop-out
    costs exactly `risk_pct`% of account equity, REGARDLESS of leverage.
    Leverage only changes how much margin that position size locks up (see
    config.EXECUTION_MAX_LEVERAGE's docstring) -- it is never used to inflate
    size beyond what the risk budget allows.
    """
    if equity_usdt <= 0:
        return SizingResult(ok=False, reason=f"non-positive equity ({equity_usdt})")
    if entry_price <= 0 or stop_price <= 0:
        return SizingResult(ok=False, reason="non-positive entry/stop price")
    if leverage <= 0:
        return SizingResult(ok=False, reason=f"non-positive leverage ({leverage})")

    stop_distance_pct = abs(entry_price - stop_price) / entry_price
    if stop_distance_pct <= 0:
        return SizingResult(ok=False, reason="stop distance is zero -- refusing to size an unbounded-risk position")

    risk_usdt = equity_usdt * (risk_pct / 100.0)
    notional = risk_usdt / stop_distance_pct
    qty = notional / entry_price
    qty = round_step(qty, qty_step, mode="floor")

    if qty < min_qty:
        return SizingResult(
            ok=False, reason=f"sized qty {qty} is below the exchange minimum {min_qty} for this symbol "
                              f"(risk budget too small for this stop distance at current equity)",
        )

    notional = qty * entry_price
    if notional < min_notional_usdt:
        return SizingResult(ok=False, reason=f"sized notional {notional:.2f} USDT is below the exchange minimum {min_notional_usdt}")

    margin = notional / leverage
    decimals = _decimals_from_step(qty_step)
    return SizingResult(
        ok=True, qty=qty, qty_str=f"{qty:.{decimals}f}",
        notional_usdt=round(notional, 4), margin_usdt=round(margin, 4),
        risk_usdt=round(risk_usdt, 4),
    )


def choose_leverage(
    *, confluence_score_abs: float, atr_pct_of_price: float | None,
    min_leverage: float, max_leverage: float,
) -> float:
    """Dynamic leverage, capped by config.EXECUTION_MAX_LEVERAGE / floored by
    EXECUTION_MIN_LEVERAGE. Scales UP with confluence strength (a cleaner,
    more-agreed setup gets closer to the max), and DOWN with volatility (a
    choppier pair at the same confluence score gets less, since the same %
    stop distance already eats more margin efficiency there).

    This only changes margin efficiency, never worst-case loss: position
    size is computed from the risk budget independently of leverage (see
    position_qty_from_risk above), so a lower leverage choice here just means
    more margin gets tied up to hold the same risk-sized position, not a
    smaller or larger loss if the stop is hit.
    """
    if max_leverage <= min_leverage:
        return max(min_leverage, 1.0)
    confluence_component = clamp(confluence_score_abs, 0.0, 1.0)
    base = min_leverage + (max_leverage - min_leverage) * confluence_component

    # Volatility damp: ATR as a % of price, referenced against 3% (a fairly
    # wide 1h ATR for a major; alts run hotter). Damp factor never drops
    # leverage below 60% of `base` purely from volatility -- confluence
    # strength should still matter even on a hot pair.
    if atr_pct_of_price is not None and atr_pct_of_price > 0:
        vol_ref = 3.0
        damp = clamp(1.0 - (atr_pct_of_price / vol_ref) * 0.4, 0.6, 1.0)
    else:
        damp = 1.0

    lev = base * damp
    return round(clamp(lev, min_leverage, max_leverage), 1)


# ---------------------------------------------------------------------------
# Daily loss circuit breaker
# ---------------------------------------------------------------------------

def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@dataclass
class CircuitBreakerState:
    date: str
    daily_pnl_pct: float = 0.0
    halted: bool = False
    halted_reason: str = ""
    halted_at: float | None = None


class CircuitBreaker:
    """Tracks realized P&L (as % of account equity) accumulated over the
    current UTC day. Once cumulative loss breaches the configured limit, all
    NEW entries are blocked (existing open positions are untouched -- their
    own exchange-native SL/TP keep protecting them) until a human re-arms it.

    Deliberately does NOT auto-clear on a day rollover: a halt means
    "something needs a human to look at this", and a day boundary alone
    doesn't resolve that. Only `rearm()` (called from the local UI / API)
    clears `halted`. The `daily_pnl_pct` accumulator itself does reset each
    UTC day, since it's specifically a *daily* limit.
    """

    def __init__(self, path: Path = DEFAULT_CB_PATH):
        self.path = path
        self.state = CircuitBreakerState(date=_utc_today())
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            known = set(CircuitBreakerState.__dataclass_fields__)
            self.state = CircuitBreakerState(**{k: v for k, v in raw.items() if k in known})
        except Exception:
            pass  # corrupt file -- start fresh rather than crash the app

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        os.replace(tmp, self.path)

    def _roll_if_new_day(self) -> None:
        today = _utc_today()
        if self.state.date != today:
            self.state.date = today
            self.state.daily_pnl_pct = 0.0
            self._save()

    def record_realized_pnl_pct(self, pnl_pct_of_equity: float, limit_pct: float) -> None:
        self._roll_if_new_day()
        self.state.daily_pnl_pct += pnl_pct_of_equity
        if not self.state.halted and self.state.daily_pnl_pct <= -abs(limit_pct):
            self.state.halted = True
            self.state.halted_reason = (
                f"Daily loss limit hit: {self.state.daily_pnl_pct:.2f}% "
                f"(limit -{abs(limit_pct):.2f}%). New entries blocked until manually re-armed."
            )
            self.state.halted_at = time.time()
        self._save()

    def halt_manually(self, reason: str) -> None:
        """For non-P&L triggers (e.g. a reconciliation mismatch) that should
        also stop new entries pending human review."""
        self._roll_if_new_day()
        self.state.halted = True
        self.state.halted_reason = reason
        self.state.halted_at = time.time()
        self._save()

    def is_halted(self) -> bool:
        self._roll_if_new_day()
        return self.state.halted

    def rearm(self) -> None:
        self.state.halted = False
        self.state.halted_reason = ""
        self.state.halted_at = None
        self._save()

    def snapshot(self) -> dict:
        self._roll_if_new_day()
        return asdict(self.state)


circuit_breaker = CircuitBreaker()
