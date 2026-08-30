"""Tests for execution/signal_gate.py -- the technical trigger (bias +
confluence) and the signal-performance gate that consults the desk's own
paper + live trading history before approving a real entry.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import positions as positions_module
from execution import live_positions as live_positions_module
from execution import signal_gate


class FakeConfig:
    EXECUTION_MIN_CONFLUENCE_RATIO = 0.75
    EXECUTION_MIN_CONFLUENCE_SCORE = 0.35
    EXECUTION_MIN_BUCKET_N = 10


def make_state(direction="bullish bias", likelihood="Elevated", conf_direction="bullish",
                agree=3, total=4, score=0.6):
    return SimpleNamespace(
        direction=direction, likelihood=likelihood,
        confluence={"direction": conf_direction, "agree": agree, "total": total, "score": score, "label": "x"},
    )


def _fresh_books(tmp_path, monkeypatch):
    paper = positions_module.PositionBook(path=tmp_path / "positions.json")
    live = live_positions_module.LivePositionBook(path=tmp_path / "live_positions.json")
    monkeypatch.setattr(positions_module, "position_book", paper)
    monkeypatch.setattr(live_positions_module, "live_position_book", live)
    return paper, live


# ---------------------------------------------------------------------------
# Technical trigger
# ---------------------------------------------------------------------------

def test_rejects_two_sided_direction(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state(direction="two-sided")
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert not result.approved
    assert "no directional bias" in result.reason


def test_rejects_confluence_direction_mismatch(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state(direction="bullish bias", conf_direction="bearish")
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert not result.approved
    assert "does not confirm" in result.reason


def test_rejects_weak_confluence_ratio(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state(agree=2, total=4)  # 0.5 < 0.75 minimum
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert not result.approved
    assert "agreement too weak" in result.reason


def test_rejects_weak_confluence_score(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state(agree=3, total=4, score=0.1)  # ratio ok, score too low
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert not result.approved
    assert "score" in result.reason


def test_approves_clean_bullish_setup_with_no_history(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state()
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert result.approved
    assert result.side == "long"


def test_approves_clean_bearish_setup(tmp_path, monkeypatch):
    _fresh_books(tmp_path, monkeypatch)
    st = make_state(direction="bearish bias", conf_direction="bearish")
    result = signal_gate.evaluate_entry("BTCUSDT", st, FakeConfig())
    assert result.approved
    assert result.side == "short"


# ---------------------------------------------------------------------------
# Signal-performance gate
# ---------------------------------------------------------------------------

def _add_paper_trade(book, symbol, side, entry, exit_, direction, likelihood, agree_str="3/4"):
    pos = book.open(symbol, side, entry, cost_pct=0.15, signal_context={
        "direction": direction, "likelihood": likelihood, "confluence_agree": agree_str,
    })
    book.close(pos.id, exit_)


def test_blocks_entry_when_bucket_has_enough_negative_history(tmp_path, monkeypatch):
    paper, live = _fresh_books(tmp_path, monkeypatch)
    # 10 losing "bearish bias/Elevated" trades -- clears EXECUTION_MIN_BUCKET_N=10.
    for i in range(10):
        _add_paper_trade(paper, f"SYM{i}USDT", "short", 100, 102, "bearish bias", "Elevated")

    st = make_state(direction="bearish bias", likelihood="Elevated", conf_direction="bearish")
    result = signal_gate.evaluate_entry("NEWUSDT", st, FakeConfig())
    assert not result.approved
    assert "signal-performance gate" in result.reason


def test_does_not_block_on_thin_sample(tmp_path, monkeypatch):
    paper, live = _fresh_books(tmp_path, monkeypatch)
    # Only 3 losing trades -- below EXECUTION_MIN_BUCKET_N=10, must not block.
    for i in range(3):
        _add_paper_trade(paper, f"SYM{i}USDT", "short", 100, 102, "bearish bias", "Elevated")

    st = make_state(direction="bearish bias", likelihood="Elevated", conf_direction="bearish")
    result = signal_gate.evaluate_entry("NEWUSDT", st, FakeConfig())
    assert result.approved, f"should not block on a thin sample, got: {result.reason}"


def test_allows_entry_when_bucket_has_enough_positive_history(tmp_path, monkeypatch):
    paper, live = _fresh_books(tmp_path, monkeypatch)
    for i in range(12):
        _add_paper_trade(paper, f"SYM{i}USDT", "long", 100, 103, "bullish bias", "Low")

    st = make_state(direction="bullish bias", likelihood="Low", conf_direction="bullish")
    result = signal_gate.evaluate_entry("NEWUSDT", st, FakeConfig())
    assert result.approved


def test_pools_paper_and_live_history(tmp_path, monkeypatch):
    paper, live = _fresh_books(tmp_path, monkeypatch)
    for i in range(6):
        _add_paper_trade(paper, f"SYM{i}USDT", "short", 100, 102, "bearish bias", "Elevated")
    # 6 live losses too -- neither alone clears n=10, but pooled (12) does.
    for i in range(6):
        pos = live.open(
            symbol=f"LIVE{i}USDT", side="short", order_id="x", entry_price=100, qty=1,
            leverage=5, margin_usdt=20, risk_usdt=2, equity_at_open=1000,
            initial_stop_loss=102, take_profit=94, cost_pct=0.45,
            signal_context={"direction": "bearish bias", "likelihood": "Elevated", "confluence_agree": "3/4"},
        )
        live.close(pos.id, 102, "stop_loss")

    st = make_state(direction="bearish bias", likelihood="Elevated", conf_direction="bearish")
    result = signal_gate.evaluate_entry("NEWUSDT", st, FakeConfig())
    assert not result.approved, "pooled paper+live history should have cleared the min-n threshold and blocked"
