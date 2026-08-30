import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.costs import execution_round_trip_cost_pct, is_trade_cost_viable, reward_risk_ratio


def test_execution_cost_adds_buffer_on_top_of_measured_cost():
    measured = execution_round_trip_cost_pct(0.055, 0.02, 0.0)
    with_buffer = execution_round_trip_cost_pct(0.055, 0.02, 0.3)
    assert abs(with_buffer - (measured + 0.3)) < 1e-9
    assert abs(measured - 2 * (0.055 + 0.02)) < 1e-9


def test_reward_risk_ratio_basic():
    assert reward_risk_ratio(100, 98, 106) == 3.0  # risk 2, reward 6
    assert reward_risk_ratio(100, 100, 106) is None  # zero risk


def test_cost_viability_rejects_tiny_target():
    ok, reason = is_trade_cost_viable(100, 100.1, cost_pct=0.45, min_multiple=1.5)
    assert not ok
    assert "round-trip cost" in reason


def test_cost_viability_accepts_ample_target():
    ok, reason = is_trade_cost_viable(100, 106, cost_pct=0.45, min_multiple=1.5)
    assert ok
