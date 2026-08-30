import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.live_positions import LivePositionBook, bucket_keys


def make_book(tmp_path):
    return LivePositionBook(path=tmp_path / "live_positions.json")


def test_open_and_close_computes_pnl(tmp_path):
    book = make_book(tmp_path)
    pos = book.open(
        symbol="BTCUSDT", side="long", order_id="o1", entry_price=100, qty=1,
        leverage=10, margin_usdt=10, risk_usdt=2, equity_at_open=1000,
        initial_stop_loss=98, take_profit=106, cost_pct=0.45, signal_context={},
    )
    closed = book.close(pos.id, 106, "take_profit")
    assert closed.status == "closed"
    assert abs(closed.gross_pnl_pct() - 6.0) < 1e-9
    assert abs(closed.net_pnl_pct() - (6.0 - 0.45)) < 1e-9
    # realized_pnl_usdt = notional(100) * net_pct/100 = 100 * 5.55/100 = 5.55
    assert abs(closed.realized_pnl_usdt() - 5.55) < 1e-6
    # account_pnl_pct = 5.55 / 1000 * 100 = 0.555%
    assert abs(closed.account_pnl_pct() - 0.555) < 1e-6


def test_short_position_pnl_direction():
    from execution.live_positions import LivePosition
    pos = LivePosition(
        id="x", symbol="ETHUSDT", side="short", order_id="o", entry_price=100, entry_time=0,
        qty=1, leverage=5, margin_usdt=20, risk_usdt=2, equity_at_open=1000,
        initial_stop_loss=105, take_profit=90, cost_pct=0.0,
    )
    # price fell -- a short should be profitable
    assert pos.gross_pnl_pct(95) == 5.0
    # price rose -- a short should be losing
    assert pos.gross_pnl_pct(105) == -5.0


def test_persists_across_instances(tmp_path):
    path = tmp_path / "live_positions.json"
    book1 = LivePositionBook(path=path)
    book1.open(
        symbol="BTCUSDT", side="long", order_id="o1", entry_price=100, qty=1,
        leverage=10, margin_usdt=10, risk_usdt=2, equity_at_open=1000,
        initial_stop_loss=98, take_profit=106, cost_pct=0.45, signal_context={},
    )
    book2 = LivePositionBook(path=path)
    assert len(book2.positions) == 1
    assert book2.open_positions()[0].symbol == "BTCUSDT"


def test_max_concurrent_and_cooldown_helpers(tmp_path):
    import time
    book = make_book(tmp_path)
    assert book.open_position_for_symbol("BTCUSDT") is None
    pos = book.open(
        symbol="BTCUSDT", side="long", order_id="o1", entry_price=100, qty=1,
        leverage=10, margin_usdt=10, risk_usdt=2, equity_at_open=1000,
        initial_stop_loss=98, take_profit=106, cost_pct=0.45, signal_context={},
    )
    assert book.open_position_for_symbol("BTCUSDT") is not None
    assert len(book.open_positions()) == 1
    book.close(pos.id, 106, "take_profit")
    assert book.open_position_for_symbol("BTCUSDT") is None
    last_closed = book.last_closed_time_for_symbol("BTCUSDT")
    assert last_closed is not None
    assert abs(last_closed - time.time()) < 5


def test_bucket_keys_extracts_multiple_dimensions():
    keys = bucket_keys({"direction": "bullish bias", "likelihood": "Low", "confluence_agree": "3/4", "side": "long"})
    assert "watchlist:bullish bias/Low" in keys
    assert "confluence:3/4" in keys
    assert "side:long" in keys


def test_stats_for_bucket(tmp_path):
    book = make_book(tmp_path)
    for i in range(3):
        pos = book.open(
            symbol=f"S{i}USDT", side="long", order_id="o", entry_price=100, qty=1,
            leverage=5, margin_usdt=20, risk_usdt=2, equity_at_open=1000,
            initial_stop_loss=98, take_profit=104, cost_pct=0.0,
            signal_context={"direction": "bullish bias", "likelihood": "Low"},
        )
        book.close(pos.id, 104, "take_profit")
    stats = book.stats_for_bucket("watchlist:bullish bias/Low")
    assert stats["count"] == 3
    assert stats["win_rate"] == 100.0
    assert abs(stats["avg_net_pnl_pct"] - 4.0) < 1e-9
