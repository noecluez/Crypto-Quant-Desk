"""Thin orchestration layer so feeds/bybit_feed.py and server.py don't need
to import execution/order_manager.py (or know anything about signal_gate,
risk, or live_positions) directly. Keeps the dependency direction one-way:
feeds/server -> execution, never the reverse -- execution/order_manager.py
already reads from state.py and positions.py via local imports precisely to
avoid a cycle, and this module is where that boundary is drawn cleanly for
callers outside execution/.
"""
from __future__ import annotations

import asyncio
import logging

from execution import order_manager

log = logging.getLogger("execution.engine")


def evaluate_watchlist(symbols: list[str], state_symbols: dict, cfg) -> None:
    """Called once per deep-refresh cycle (config.DEEP_REFRESH_SECONDS,
    default 5 min) for the current deep watchlist -- NOT on every fast
    price tick. Deliberate: entry evaluation involves real network calls
    (equity, instrument info, order placement) that must not run at
    tick frequency (~50ms) across ~20 symbols, and the technical trigger
    (confluence, direction) only changes meaningfully on the same cadence
    the deep refresh itself recomputes it."""
    if not cfg.EXECUTION_ENABLED:
        return
    for sym in symbols:
        st = state_symbols.get(sym)
        if st is None or st.price <= 0:
            continue
        try:
            order_manager.evaluate_and_maybe_enter(sym, st, cfg)
        except Exception:
            log.exception("Execution: unhandled error evaluating %s (skipping, will retry next cycle)", sym)


def startup_reconcile(cfg) -> None:
    try:
        order_manager.reconcile_on_startup(cfg)
    except Exception:
        log.exception("Execution: startup reconciliation raised unexpectedly")


async def monitor_loop(cfg) -> None:
    """Independent cadence from the deep-refresh loop (config.
    EXECUTION_MONITOR_SECONDS, default 20s) -- trailing-stop hand-off and
    exchange-side close detection both want to be much more responsive than
    the 5-minute technical-signal refresh."""
    while True:
        await asyncio.sleep(cfg.EXECUTION_MONITOR_SECONDS)
        if not cfg.EXECUTION_ENABLED:
            continue
        try:
            await asyncio.get_event_loop().run_in_executor(None, order_manager.run_monitor_cycle, cfg)
        except Exception:
            log.warning("Execution monitor cycle failed (will retry next cycle)", exc_info=True)
