"""Unit tests for execution/risk.py -- position sizing, leverage selection,
step rounding, and the daily-loss circuit breaker. This is the module a
sizing bug would live in, so it gets the heaviest coverage in execution/.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution import risk


# ---------------------------------------------------------------------------
# Step rounding
# ---------------------------------------------------------------------------

def test_round_step_floor():
    assert risk.round_step(1.2345, 0.01, mode="floor") == 1.23
    assert risk.round_step(0.0009, 0.001, mode="floor") == 0.0
    assert risk.round_step(10.0, 1.0, mode="floor") == 10.0


def test_round_step_nearest():
    assert risk.round_step(1.236, 0.01, mode="nearest") == 1.24
    assert risk.round_step(1.234, 0.01, mode="nearest") == 1.23


def test_format_step_precision():
    assert risk.format_step(1.2, 0.001, mode="floor") == "1.200"
    assert risk.format_step(5, 1.0, mode="nearest") == "5"


# ---------------------------------------------------------------------------
# Position sizing -- the core risk-per-trade rule
# ---------------------------------------------------------------------------

def test_position_sizing_basic_risk_math():
    # $10,000 equity, 2% risk = $200 max loss. Entry 100, stop 98 (2% away).
    # Risk budget / stop distance % = notional: 200 / 0.02 = 10,000 notional
    # -> qty = 100.
    result = risk.position_qty_from_risk(
        equity_usdt=10_000, risk_pct=2, entry_price=100, stop_price=98,
        leverage=10, qty_step=0.001, min_qty=0.001,
    )
    assert result.ok
    assert abs(result.qty - 100.0) < 0.01
    assert abs(result.risk_usdt - 200.0) < 0.01
    # margin = notional / leverage = 10000 / 10 = 1000
    assert abs(result.margin_usdt - 1000.0) < 1.0


def test_position_sizing_is_leverage_independent_for_worst_case_loss():
    """The whole point of risk-based sizing: worst-case $ loss must be the
    same regardless of what leverage is chosen -- leverage only changes
    margin used, not risk. This is the single most important invariant in
    the entire execution engine; if this test ever fails, stop and do not
    ship until it's fixed."""
    for leverage in (3, 5, 10, 20):
        result = risk.position_qty_from_risk(
            equity_usdt=10_000, risk_pct=2, entry_price=100, stop_price=95,
            leverage=leverage, qty_step=0.0001, min_qty=0.0001,
        )
        assert result.ok, f"sizing failed at leverage={leverage}: {result.reason}"
        worst_case_loss = result.qty * abs(100 - 95)
        assert abs(worst_case_loss - 200.0) < 1.0, (
            f"leverage={leverage} produced worst-case loss {worst_case_loss}, expected ~200"
        )
        # But margin used should shrink as leverage rises.
        expected_margin = (result.qty * 100) / leverage
        assert abs(result.margin_usdt - expected_margin) < 1.0


def test_position_sizing_rejects_zero_stop_distance():
    result = risk.position_qty_from_risk(
        equity_usdt=10_000, risk_pct=2, entry_price=100, stop_price=100,
        leverage=10, qty_step=0.001, min_qty=0.001,
    )
    assert not result.ok
    assert "zero" in result.reason


def test_position_sizing_rejects_below_exchange_minimum():
    # Tiny equity + tight risk should size below a realistic min_qty.
    result = risk.position_qty_from_risk(
        equity_usdt=10, risk_pct=0.1, entry_price=50_000, stop_price=49_500,
        leverage=10, qty_step=0.001, min_qty=0.001,
    )
    assert not result.ok


def test_position_sizing_rejects_non_positive_equity():
    result = risk.position_qty_from_risk(
        equity_usdt=0, risk_pct=2, entry_price=100, stop_price=98,
        leverage=10, qty_step=0.001, min_qty=0.001,
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Leverage selection
# ---------------------------------------------------------------------------

def test_choose_leverage_scales_with_confluence():
    low_conf = risk.choose_leverage(confluence_score_abs=0.35, atr_pct_of_price=1.0, min_leverage=3, max_leverage=10)
    high_conf = risk.choose_leverage(confluence_score_abs=1.0, atr_pct_of_price=1.0, min_leverage=3, max_leverage=10)
    assert low_conf < high_conf
    assert 3 <= low_conf <= 10
    assert 3 <= high_conf <= 10


def test_choose_leverage_damps_on_high_volatility():
    calm = risk.choose_leverage(confluence_score_abs=1.0, atr_pct_of_price=0.5, min_leverage=3, max_leverage=10)
    volatile = risk.choose_leverage(confluence_score_abs=1.0, atr_pct_of_price=6.0, min_leverage=3, max_leverage=10)
    assert volatile < calm
    assert volatile >= 3  # never below the floor purely from volatility damping


def test_choose_leverage_never_exceeds_bounds():
    for score in (-5, 0, 0.5, 2.0):
        lev = risk.choose_leverage(confluence_score_abs=score, atr_pct_of_price=10.0, min_leverage=3, max_leverage=10)
        assert 3 <= lev <= 10


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def test_circuit_breaker_trips_at_limit(tmp_path):
    cb = risk.CircuitBreaker(path=tmp_path / "cb.json")
    assert not cb.is_halted()
    cb.record_realized_pnl_pct(-4.0, limit_pct=10)
    assert not cb.is_halted()
    cb.record_realized_pnl_pct(-4.0, limit_pct=10)
    assert not cb.is_halted()
    cb.record_realized_pnl_pct(-3.0, limit_pct=10)  # cumulative -11%, past -10% limit
    assert cb.is_halted()
    assert "Daily loss limit" in cb.state.halted_reason


def test_circuit_breaker_does_not_trip_on_gains():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cb = risk.CircuitBreaker(path=Path(d) / "cb.json")
        cb.record_realized_pnl_pct(5.0, limit_pct=10)
        cb.record_realized_pnl_pct(-2.0, limit_pct=10)
        assert not cb.is_halted()


def test_circuit_breaker_persists_across_instances(tmp_path):
    path = tmp_path / "cb.json"
    cb1 = risk.CircuitBreaker(path=path)
    cb1.record_realized_pnl_pct(-15.0, limit_pct=10)
    assert cb1.is_halted()

    cb2 = risk.CircuitBreaker(path=path)  # simulates an app restart
    assert cb2.is_halted(), "halted state must survive a restart -- it's the whole point of a circuit breaker"


def test_circuit_breaker_rearm_clears_halt(tmp_path):
    cb = risk.CircuitBreaker(path=tmp_path / "cb.json")
    cb.record_realized_pnl_pct(-15.0, limit_pct=10)
    assert cb.is_halted()
    cb.rearm()
    assert not cb.is_halted()
    assert cb.state.halted_reason == ""


def test_circuit_breaker_stays_halted_across_day_rollover(tmp_path, monkeypatch):
    path = tmp_path / "cb.json"
    cb = risk.CircuitBreaker(path=path)
    cb.record_realized_pnl_pct(-15.0, limit_pct=10)
    assert cb.is_halted()

    # Simulate a new UTC day by monkeypatching _utc_today used internally.
    monkeypatch.setattr(risk, "_utc_today", lambda: "2099-01-01")
    assert cb.is_halted(), "a day rollover must NOT silently clear a halt -- only rearm() may"
    # the pnl accumulator itself should have reset for the new day though
    snap = cb.snapshot()
    assert snap["daily_pnl_pct"] == 0.0
    assert snap["halted"] is True


def test_circuit_breaker_manual_halt(tmp_path):
    cb = risk.CircuitBreaker(path=tmp_path / "cb.json")
    cb.halt_manually("reconciliation mismatch")
    assert cb.is_halted()
    assert cb.state.halted_reason == "reconciliation mismatch"
