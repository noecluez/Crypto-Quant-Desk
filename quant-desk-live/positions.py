"""Fictional ("paper") long/short positions the user opens by hand from the
UI, simulated against real live Bybit prices, entirely local -- no exchange
account touched, no order ever placed anywhere. This app is an analysis
tool; nothing in this module can or does reach a broker.

Deliberately has zero dependency on state.py or the network: callers
(server.py) pass in whatever live price and signal context they already
have, so this module is pure bookkeeping + JSON persistence and is easy to
unit-test in isolation.

Two things make this more than a trade diary:

1. **Signal attribution.** Every position records what the desk was
   *saying* at the moment it opened -- the Spotlight bias/confidence/
   pattern, the heat score and technical direction, and the derivatives
   positioning read. That turns this file into a running, unbiased forward
   test of the desk's own opinions. The historical backtest in
   analysis/backtest.py cannot do that job: it only sees daily bars, and it
   runs on symbols selected *because* they already moved, which inflates
   every number it produces. This doesn't have that problem -- the signal
   is recorded before the outcome exists.

2. **Costs.** Every P&L figure is reported both gross and net of a
   configurable fee + slippage round trip. A low-timeframe strategy pays
   that cost constantly, and a signal whose edge is smaller than its cost
   is not an edge. See analysis/costs.py.

Positions persist to a JSON file next to main.py so the track record
survives an app restart -- losing it on every Ctrl+C would defeat the point
of "results should be tracked."
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "positions.json"


@dataclass
class Position:
    id: str
    symbol: str
    side: str  # "long" | "short"
    entry_price: float
    entry_time: float
    status: str = "open"  # "open" | "closed"
    exit_price: float | None = None
    exit_time: float | None = None
    # Round-trip cost (%) captured at open, so a position keeps the cost
    # assumption it was opened under even if the user later retunes
    # TAKER_FEE_PCT/SLIPPAGE_PCT. Rewriting history on a config change would
    # quietly invalidate the whole track record.
    cost_pct: float = 0.0
    # What the desk was saying when this opened -- see module docstring.
    signal_context: dict = field(default_factory=dict)

    def gross_pnl_pct(self, live_price: float | None = None) -> float | None:
        price = self.exit_price if self.status == "closed" else live_price
        if price is None or self.entry_price <= 0:
            return None
        if self.side == "long":
            return (price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - price) / self.entry_price * 100

    # Historical name, kept so nothing that already calls it breaks.
    def pnl_pct(self, live_price: float | None = None) -> float | None:
        return self.gross_pnl_pct(live_price)

    def net_pnl_pct(self, live_price: float | None = None) -> float | None:
        """Gross minus the round-trip cost this position was opened under.
        This is the number that decides whether the trade actually worked."""
        gross = self.gross_pnl_pct(live_price)
        if gross is None:
            return None
        return gross - self.cost_pct

    def to_dict(self, live_price: float | None = None) -> dict:
        current_price = self.exit_price if self.status == "closed" else live_price
        end_time = self.exit_time if self.status == "closed" else time.time()
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time,
            "current_price": current_price,
            "pnl_pct": self.gross_pnl_pct(live_price),
            "net_pnl_pct": self.net_pnl_pct(live_price),
            "cost_pct": self.cost_pct,
            "signal_context": self.signal_context,
            "duration_seconds": (end_time - self.entry_time) if end_time else None,
        }


def _bucket(context: dict) -> str | None:
    """The key a position's outcome is filed under in the scorecard.

    Deliberately coarse: "Spotlight bias + confidence" and "direction +
    likelihood" are the two calls a user actually acts on, and splitting any
    finer would scatter a small number of trades across too many buckets to
    ever reach a readable sample size."""
    if not context:
        return None
    bias = context.get("spotlight_bias")
    confidence = context.get("spotlight_confidence")
    if bias and confidence:
        return f"spotlight:{bias}/{confidence}"
    likelihood = context.get("likelihood")
    direction = context.get("direction")
    if likelihood and direction:
        return f"watchlist:{direction}/{likelihood}"
    return None


_BUCKET_LABELS = {"spotlight": "Spotlight said", "watchlist": "Watchlist said"}


def _bucket_label(bucket: str) -> str:
    kind, _, rest = bucket.partition(":")
    return f"{_BUCKET_LABELS.get(kind, kind)} {rest}"


def _stats_for(returns_gross: list[float], returns_net: list[float]) -> dict:
    if not returns_net:
        return {"count": 0, "win_rate": None, "avg_pnl_pct": None,
                "gross_win_rate": None, "gross_avg_pnl_pct": None,
                "best_pnl_pct": None, "worst_pnl_pct": None}
    wins = sum(1 for r in returns_net if r > 0)
    gross_wins = sum(1 for r in returns_gross if r > 0)
    return {
        "count": len(returns_net),
        "win_rate": round(wins / len(returns_net) * 100, 1),
        "avg_pnl_pct": round(sum(returns_net) / len(returns_net), 3),
        "gross_win_rate": round(gross_wins / len(returns_gross) * 100, 1),
        "gross_avg_pnl_pct": round(sum(returns_gross) / len(returns_gross), 3),
        "best_pnl_pct": round(max(returns_net), 3),
        "worst_pnl_pct": round(min(returns_net), 3),
    }


class PositionBook:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.positions: dict[str, Position] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            known = set(Position.__dataclass_fields__)
            for row in raw.get("positions", []):
                # Tolerate rows written by an older version that predates
                # cost_pct/signal_context -- discarding a user's whole
                # history over a schema addition would be unforgivable.
                row.setdefault("cost_pct", 0.0)
                row.setdefault("signal_context", {})
                pos = Position(**{k: v for k, v in row.items() if k in known})
                self.positions[pos.id] = pos
        except Exception:
            # A corrupt/partial file must never crash the app on startup --
            # worst case, paper-trading history is lost, which is far
            # better than the whole app failing to start over it.
            pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"positions": [asdict(p) for p in self.positions.values()]}
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)  # atomic on both POSIX and Windows

    def open(self, symbol: str, side: str, price: float,
             cost_pct: float = 0.0, signal_context: dict | None = None) -> Position:
        pos = Position(
            id=uuid.uuid4().hex[:12], symbol=symbol, side=side,
            entry_price=price, entry_time=time.time(),
            cost_pct=cost_pct, signal_context=signal_context or {},
        )
        self.positions[pos.id] = pos
        self._save()
        return pos

    def close(self, position_id: str, price: float) -> Position | None:
        pos = self.positions.get(position_id)
        if pos is None or pos.status != "open":
            return None
        pos.status = "closed"
        pos.exit_price = price
        pos.exit_time = time.time()
        self._save()
        return pos

    def summary(self) -> dict:
        closed = [p for p in self.positions.values() if p.status == "closed"]
        gross = [r for p in closed if (r := p.gross_pnl_pct()) is not None]
        net = [r for p in closed if (r := p.net_pnl_pct()) is not None]
        stats = _stats_for(gross, net)
        # Total cost drag paid across the whole track record -- the clearest
        # way to see how much of a gross edge is handed to the exchange.
        stats["total_cost_drag_pct"] = round(sum(p.cost_pct for p in closed), 3)
        return stats

    def scorecard(self) -> dict:
        """Forward-test results grouped by what the desk was saying at entry.

        This is the loop-closing feature: it answers "when this app says
        aligned-bullish with high confidence, what has actually happened
        next?" -- measured on real forward outcomes rather than on a
        backtest of already-selected winners.

        Thin buckets are still shown, with their count, because hiding them
        would be worse than showing an honestly-small number. The UI is
        responsible for making `n` impossible to miss.
        """
        buckets: dict[str, dict[str, list]] = {}
        unattributed = 0
        for p in self.positions.values():
            if p.status != "closed":
                continue
            key = _bucket(p.signal_context)
            if key is None:
                unattributed += 1
                continue
            g, n = p.gross_pnl_pct(), p.net_pnl_pct()
            if g is None or n is None:
                continue
            entry = buckets.setdefault(key, {"gross": [], "net": []})
            entry["gross"].append(g)
            entry["net"].append(n)

        rows = []
        for key, vals in buckets.items():
            stats = _stats_for(vals["gross"], vals["net"])
            stats["bucket"] = key
            stats["label"] = _bucket_label(key)
            # A signal that wins gross but loses net is the single most
            # important thing this app can tell you, so flag it explicitly
            # rather than leaving it to be spotted in the numbers.
            stats["eaten_by_costs"] = bool(
                stats["gross_avg_pnl_pct"] is not None
                and stats["avg_pnl_pct"] is not None
                and stats["gross_avg_pnl_pct"] > 0 >= stats["avg_pnl_pct"]
            )
            rows.append(stats)
        rows.sort(key=lambda r: (-r["count"], r["bucket"]))
        return {"rows": rows, "unattributed": unattributed}

    def snapshot(self, live_prices: dict[str, float]) -> dict:
        open_positions = sorted(
            (p.to_dict(live_prices.get(p.symbol)) for p in self.positions.values() if p.status == "open"),
            key=lambda d: d["entry_time"], reverse=True,
        )
        closed_positions = sorted(
            (p.to_dict() for p in self.positions.values() if p.status == "closed"),
            key=lambda d: d["exit_time"] or 0, reverse=True,
        )
        return {
            "open": list(open_positions),
            "closed": closed_positions[:50],
            "summary": self.summary(),
            "scorecard": self.scorecard(),
        }


position_book = PositionBook()
