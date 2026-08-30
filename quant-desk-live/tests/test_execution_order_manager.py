"""Tests for execution/order_manager.py's compute_stop_target -- the hybrid
ATR + support/resistance stop/target placement. No network involved.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.order_manager import compute_stop_target


class FakeConfig:
    EXECUTION_SL_ATR_MULT = 1.5
    EXECUTION_TP_ATR_MULT = 3.0
    EXECUTION_MIN_REWARD_RISK = 2.0
    EXECUTION_SL_SR_SNAP_MAX_PCT = 50


def make_state(price=100.0, atr=2.0, support=None, resistance=None):
    return SimpleNamespace(
        price=price,
        timeframes={"1h": {"atr": atr}},
        support=support or [],
        resistance=resistance or [],
    )


def test_pure_atr_long_stop_and_target():
    st = make_state(price=100.0, atr=2.0)
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert note == ""
    assert stop == 100.0 - 1.5 * 2.0  # 97.0
    assert target == 100.0 + 3.0 * 2.0  # 106.0


def test_pure_atr_short_stop_and_target():
    st = make_state(price=100.0, atr=2.0)
    stop, target, note = compute_stop_target(st, "short", FakeConfig())
    assert stop == 100.0 + 1.5 * 2.0  # 103.0
    assert target == 100.0 - 3.0 * 2.0  # 94.0


def test_rejects_when_no_atr_available():
    st = make_state(price=100.0, atr=None)
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert stop is None and target is None
    assert "ATR" in note


def test_falls_back_to_4h_atr_if_1h_missing():
    st = SimpleNamespace(price=100.0, timeframes={"4h": {"atr": 4.0}}, support=[], resistance=[])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert note == ""
    assert stop == 100.0 - 1.5 * 4.0


def test_snaps_stop_to_nearby_support_on_long():
    # ATR stop would be at 97.0 (3.0 away). A support at 97.5 (2.5 away) is
    # within the 50% snap tolerance (1.5 * 0.5 = 0.75 tolerance around 3.0),
    # so it should snap there (minus a small buffer).
    st = make_state(price=100.0, atr=2.0, support=[{"price": 97.5, "touches": 3, "distance_pct": -2.5}])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    buffer = 2.0 * 0.1
    assert abs(stop - (97.5 - buffer)) < 1e-9


def test_does_not_snap_to_a_support_far_outside_tolerance():
    # Support at 80 is way outside the ATR-implied stop distance (20 vs 3,
    # tolerance is 0.75) -- must NOT snap to it.
    st = make_state(price=100.0, atr=2.0, support=[{"price": 80.0, "touches": 5, "distance_pct": -20.0}])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert stop == 97.0  # unchanged, pure ATR


def test_extends_target_to_qualifying_resistance():
    # ATR target = 106. A resistance at 110 gives R:R = 10/3 = 3.33 >= 2.0
    # minimum, so target should extend to it.
    st = make_state(price=100.0, atr=2.0, resistance=[{"price": 110.0, "touches": 2, "distance_pct": 10.0}])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert target == 110.0


def test_does_not_extend_target_to_a_level_closer_than_the_atr_target():
    # The extension logic only ever considers levels FARTHER than the
    # ATR-implied target (106 here) -- a resistance well inside that
    # distance must be left alone entirely, not used as a target.
    st = make_state(price=100.0, atr=2.0, resistance=[{"price": 100.5, "touches": 1, "distance_pct": 0.5}])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    assert target == 106.0


def test_guarantees_minimum_reward_risk_even_after_a_widening_snap():
    # A support at distance 4.4 (vs. ATR-implied 3.0) is within the 50%
    # snap tolerance (tolerance = 1.5) and WIDENS the stop, which would
    # otherwise leave R:R at 6/4.6 = 1.3 -- below the 2.0 minimum. The
    # fallback must widen the target off the post-snap risk distance to
    # restore the minimum reward:risk instead of shipping an under-sized target.
    st = make_state(price=100.0, atr=2.0, support=[{"price": 95.6, "touches": 4, "distance_pct": -4.4}])
    stop, target, note = compute_stop_target(st, "long", FakeConfig())
    risk_dist = abs(100.0 - stop)
    reward_dist = abs(target - 100.0)
    assert stop < 97.0, "expected the wider support-based stop to have been snapped to"
    assert reward_dist / risk_dist >= FakeConfig.EXECUTION_MIN_REWARD_RISK - 1e-9
