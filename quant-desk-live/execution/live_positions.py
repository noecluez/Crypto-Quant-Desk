"""Real-money position bookkeeping -- the live-execution counterpart of
positions.py's paper PositionBook, kept in a SEPARATE file
(live_positions.json, next to the existing positions.json) so real trades
and fictional Tracker Positions can never be confused with each other or
accidentally merged in the UI.

Same signal-attribution principle as the paper book (see positions.py's
docstring): every live position records what the desk was saying at entry,
so execution/signal_gate.py can eventually judge its own live track record
the same unbiased way the Signal Scorecard judges the paper one. Early on
there won't be enough live trades for that to mean anything -- the gate
falls back to the paper book's much larger history until the live book
catches up (see signal_gate.py).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "live_positions.json"


@dataclass
class LivePosition:
    id: str
    symbol: str
    side: str  # "long" | "short"
    order_id: str
    entry_price: float
    entry_time: float
    qty: float
    leverage: float
    margin_usdt: float
    risk_usdt: float
    equity_at_open: float
    initial_stop_loss: float
    take_profit: float
    cost_pct: float = 0.0  # execution_cost_pct (round-trip + buffer) captured at open
    status: str = "open"  # "open" | "closed"
    trailing_active: bool = False
    trailing_activated_at: float | None = None
    exit_price: float | None = None
    exit_time: float | None = None
    exit_reason: str | None = None  # "take_profit" | "stop_loss" | "trailing_stop" | "manual" | "circuit_breaker" | "reconciliation"
    signal_context: dict = field(default_factory=dict)

    def gross_pnl_pct(self, live_price: float | None = None) -> float | None:
        price = self.exit_price if self.status == "closed" else live_price
        if price is None or self.entry_price <= 0:
            return None
        if self.side == "long":
            return (price - self.entry_price) / self.entry_price * 100
        return (self.entry_price - price) / self.entry_price * 100

    def net_pnl_pct(self, live_price: float | None = None) -> float | None:
        gross = self.gross_pnl_pct(live_price)
        if gross is None:
            return None
        return gross - self.cost_pct

    def realized_pnl_usdt(self, live_price: float | None = None) -> float | None:
        net_pct = self.net_pnl_pct(live_price)
        if net_pct is None:
            return None
        notional = self.qty * self.entry_price
        return notional * net_pct / 100.0

    def account_pnl_pct(self, live_price: float | None = None) -> float | None:
        """Realized/unrealized net P&L as a % of the equity this position was
        actually opened against -- the real (not assumed) account-impact
        figure, since this is real money."""
        pnl_usdt = self.realized_pnl_usdt(live_price)
        if pnl_usdt is None or self.equity_at_open <= 0:
            return None
        return pnl_usdt / self.equity_at_open * 100.0

    def to_dict(self, live_price: float | None = None) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "side": self.side, "order_id": self.order_id,
            "status": self.status, "entry_price": self.entry_price, "entry_time": self.entry_time,
            "qty": self.qty, "leverage": self.leverage, "margin_usdt": self.margin_usdt,
            "risk_usdt": self.risk_usdt, "initial_stop_loss": self.initial_stop_loss,
            "take_profit": self.take_profit, "trailing_active": self.trailing_active,
            "exit_price": self.exit_price, "exit_time": self.exit_time, "exit_reason": self.exit_reason,
            "current_price": self.exit_price if self.status == "closed" else live_price,
            "gross_pnl_pct": self.gross_pnl_pct(live_price),
            "net_pnl_pct": self.net_pnl_pct(live_price),
            "realized_pnl_usdt": self.realized_pnl_usdt(live_price),
            "account_pnl_pct": self.account_pnl_pct(live_price),
            "signal_context": self.signal_context,
        }


def bucket_keys(context: dict) -> list[str]:
    """A position can be filed under more than one bucket key -- e.g. its
    watchlist direction/likelihood bucket AND its confluence-agreement
    bucket -- since execution/signal_gate.py checks both independently. See
    positions.py's `_bucket()` for the original single-key version this
    extends; kept as a separate function here rather than importing it
    because the live book intentionally tracks additional dimensions the
    paper scorecard UI doesn't need to show."""
    keys: list[str] = []
    direction, likelihood = context.get("direction"), context.get("likelihood")
    if direction and likelihood:
        keys.append(f"watchlist:{direction}/{likelihood}")
    agree = context.get("confluence_agree")
    if agree:
        keys.append(f"confluence:{agree}")
    side = context.get("side")
    if side:
        keys.append(f"side:{side}")
    return keys


def _stats_for(returns_net: list[float]) -> dict:
    if not returns_net:
        return {"count": 0, "win_rate": None, "avg_net_pnl_pct": None}
    wins = sum(1 for r in returns_net if r > 0)
    return {
        "count": len(returns_net),
        "win_rate": round(wins / len(returns_net) * 100, 1),
        "avg_net_pnl_pct": round(sum(returns_net) / len(returns_net), 3),
    }


class LivePositionBook:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.positions: dict[str, LivePosition] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            known = set(LivePosition.__dataclass_fields__)
            for row in raw.get("positions", []):
                pos = LivePosition(**{k: v for k, v in row.items() if k in known})
                self.positions[pos.id] = pos
        except Exception:
            pass  # never let a corrupt file crash startup -- see positions.py's identical rationale

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"positions": [asdict(p) for p in self.positions.values()]}
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    def open(self, **kwargs) -> LivePosition:
        pos = LivePosition(id=uuid.uuid4().hex[:12], entry_time=time.time(), **kwargs)
        self.positions[pos.id] = pos
        self._save()
        return pos

    def close(self, position_id: str, price: float, reason: str) -> LivePosition | None:
        pos = self.positions.get(position_id)
        if pos is None or pos.status != "open":
            return None
        pos.status = "closed"
        pos.exit_price = price
        pos.exit_time = time.time()
        pos.exit_reason = reason
        self._save()
        return pos

    def mark_trailing_active(self, position_id: str) -> None:
        pos = self.positions.get(position_id)
        if pos is not None:
            pos.trailing_active = True
            pos.trailing_activated_at = time.time()
            self._save()

    def open_positions(self) -> list[LivePosition]:
        return [p for p in self.positions.values() if p.status == "open"]

    def open_position_for_symbol(self, symbol: str) -> LivePosition | None:
        for p in self.positions.values():
            if p.symbol == symbol and p.status == "open":
                return p
        return None

    def last_closed_time_for_symbol(self, symbol: str) -> float | None:
        times = [p.exit_time for p in self.positions.values() if p.symbol == symbol and p.status == "closed" and p.exit_time]
        return max(times) if times else None

    def stats_for_bucket(self, key: str) -> dict:
        nets = [
            r for p in self.positions.values() if p.status == "closed" and key in bucket_keys(p.signal_context)
            for r in [p.net_pnl_pct()] if r is not None
        ]
        return _stats_for(nets)

    def snapshot(self, live_prices: dict[str, float]) -> dict:
        open_positions = sorted(
            (p.to_dict(live_prices.get(p.symbol)) for p in self.positions.values() if p.status == "open"),
            key=lambda d: d["entry_time"], reverse=True,
        )
        closed_positions = sorted(
            (p.to_dict() for p in self.positions.values() if p.status == "closed"),
            key=lambda d: d["exit_time"] or 0, reverse=True,
        )
        closed_net = [p.net_pnl_pct() for p in self.positions.values() if p.status == "closed" and p.net_pnl_pct() is not None]
        closed_usdt = [p.realized_pnl_usdt() for p in self.positions.values() if p.status == "closed" and p.realized_pnl_usdt() is not None]
        return {
            "open": open_positions,
            "closed": closed_positions[:50],
            "summary": {
                **_stats_for(closed_net),
                "total_realized_usdt": round(sum(closed_usdt), 2) if closed_usdt else 0.0,
            },
        }


live_position_book = LivePositionBook()
