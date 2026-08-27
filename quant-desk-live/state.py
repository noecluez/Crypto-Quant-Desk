"""In-memory shared state: what every feed writes to and what the web UI reads from.
One process, one dict, guarded by an asyncio lock. No database needed for a
single-user local monitor.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class SymbolState:
    symbol: str
    asset_class: str  # "crypto" | "equity"
    price: float = 0.0
    prev_close: float = 0.0
    change_24h_pct: float | None = None
    change_7d_pct: float | None = None
    rsi14: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    vol_ratio: float | None = None
    heat: float = 0.0
    likelihood: str = "Low"
    direction: str = "two-sided"
    tags: list = field(default_factory=list)
    history: list = field(default_factory=list)  # last N (ts, price) for sparklines
    last_update: float = 0.0
    connected: bool = False

    # --- deep technical analysis (refreshed periodically, see feeds/bybit_feed.py) ---
    tier: str = "pinned"  # "pinned" (from CRYPTO_SYMBOLS) | "discovered" (found by the universe scan)
    timeframes: dict = field(default_factory=dict)   # {"15m": {...}, "1h": {...}, "4h": {...}, "1D": {...}}
    confluence: dict | None = None                    # output of analysis.indicators.confluence()
    divergence: dict | None = None                     # output of analysis.indicators.detect_divergence()
    support: list = field(default_factory=list)        # [{price, touches, distance_pct}, ...]
    resistance: list = field(default_factory=list)
    fibonacci: dict | None = None                       # output of analysis.indicators.fibonacci_levels()
    deep_updated_at: float = 0.0

    # --- derivatives positioning (linear/inverse only; None on spot) ---
    # These are the only fields in this app that carry information the price
    # chart doesn't already contain -- see analysis/derivatives.py.
    funding: dict | None = None            # interpret_funding()
    funding_history: list = field(default_factory=list)
    open_interest: dict | None = None      # interpret_open_interest()
    long_short_ratio: dict | None = None   # interpret_long_short_ratio()
    liquidations: dict | None = None       # summarize_liquidations()
    positioning: dict | None = None        # positioning_read() -- the combined view
    derivatives_updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "tier": self.tier,
            "price": self.price,
            "change_24h_pct": self.change_24h_pct,
            "change_7d_pct": self.change_7d_pct,
            "rsi14": self.rsi14,
            "sma50": self.sma50,
            "sma200": self.sma200,
            "high_52w": self.high_52w,
            "low_52w": self.low_52w,
            "vol_ratio": self.vol_ratio,
            "heat": self.heat,
            "likelihood": self.likelihood,
            "direction": self.direction,
            "tags": self.tags,
            "sparkline": [p for _, p in self.history[-60:]],
            "last_update": self.last_update,
            "connected": self.connected,
            "timeframes": self.timeframes,
            "confluence": self.confluence,
            "divergence": self.divergence,
            "support": self.support,
            "resistance": self.resistance,
            "fibonacci": self.fibonacci,
            "deep_updated_at": self.deep_updated_at,
            "funding": self.funding,
            "open_interest": self.open_interest,
            "long_short_ratio": self.long_short_ratio,
            "liquidations": self.liquidations,
            "positioning": self.positioning,
            "derivatives_updated_at": self.derivatives_updated_at,
        }

    def push_price(self, price: float, max_points: int = 300) -> None:
        self.price = price
        self.last_update = time.time()
        self.history.append((self.last_update, price))
        if len(self.history) > max_points:
            self.history = self.history[-max_points:]


class AppState:
    def __init__(self) -> None:
        self.symbols: dict[str, SymbolState] = {}
        self.lock = asyncio.Lock()
        self.clients: set = set()
        self.feed_status: dict[str, str] = {"bybit": "starting"}
        self.started_at = time.time()
        self.alert_log: list[dict] = []

        # Which symbols are currently under full deep analysis (pinned +
        # discovered), set by feeds/bybit_feed.py. Anything else lingering
        # in `self.symbols` (e.g. a discovered pair that later rotated out)
        # is filtered out of the snapshot rather than deleted outright, so
        # a brief flicker in/out of the watchlist doesn't lose its history.
        self.deep_symbols: set[str] = set()
        self.universe_scanned_count: int = 0
        self.signal_stats: dict = {}  # aggregated backtest library, see analysis/backtest.py

        # Spotlight: one symbol under ultra-frequent 1m-12h analysis, set by
        # feeds/bybit_feed.py's set_spotlight()/clear_spotlight(). None means
        # no Spotlight is currently active.
        self.spotlight_symbol: str | None = None
        self.spotlight: dict | None = None

    def log_alert(self, message: str) -> None:
        self.alert_log.append({"ts": time.time(), "message": message})
        self.alert_log = self.alert_log[-25:]

    def ensure(self, symbol: str, asset_class: str) -> SymbolState:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol=symbol, asset_class=asset_class)
        return self.symbols[symbol]

    def snapshot(self) -> dict:
        from positions import position_book  # local import: avoids a state<->positions import cycle
        from config import config
        from analysis.costs import summarize_costs

        active = [s for s in self.symbols.values() if s.symbol in self.deep_symbols]
        ranked = sorted([s for s in active if s.price > 0], key=lambda s: s.heat, reverse=True)
        top, also_watching = ranked[:10], ranked[10:20]
        # Every symbol we have a live price for, not just the deep-analyzed
        # ones -- a paper position can be open on a ticker outside the
        # watchlist too (see feeds/bybit_feed.py: track_extra_symbol).
        live_prices = {sym: s.price for sym, s in self.symbols.items() if s.price > 0}
        return {
            "type": "snapshot",
            "server_time": time.time(),
            "uptime_seconds": time.time() - self.started_at,
            "feed_status": self.feed_status,
            "alerts": list(reversed(self.alert_log)),
            "top": [s.to_dict() for s in top],
            "also_watching": [s.to_dict() for s in also_watching],
            "crypto": sorted([s.to_dict() for s in active], key=lambda s: s["symbol"]),
            "universe_scanned_count": self.universe_scanned_count,
            "signal_stats": self.signal_stats,
            "positions": position_book.snapshot(live_prices),
            "spotlight": {
                "symbol": self.spotlight_symbol,
                "data": self.spotlight,
                "refresh_seconds": config.SPOTLIGHT_REFRESH_SECONDS,
            },
            "market": {
                "category": config.BYBIT_CATEGORY,
                "derivatives": config.is_derivatives,
            },
            "costs": summarize_costs(config.TAKER_FEE_PCT, config.SLIPPAGE_PCT, config.ASSUMED_LEVERAGE),
        }

    async def broadcast(self) -> None:
        if not self.clients:
            return
        import json

        payload = json.dumps(self.snapshot())
        dead = set()
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self.clients -= dead


state = AppState()
