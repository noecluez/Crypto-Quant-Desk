"""FastAPI app: serves the live dashboard and pushes state over WebSocket.
Feeds run as background asyncio tasks started on app startup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from config import config
from feeds import bybit_feed
from positions import position_book
from state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="Quant Desk Live")
STATIC_DIR = Path(__file__).parent / "static"

_background_tasks: list[asyncio.Task] = []


def _log_if_crashed(task: asyncio.Task) -> None:
    """Background tasks are otherwise fire-and-forget -- if one ever dies
    (a bug we didn't anticipate, not a normal reconnect), asyncio only logs
    that at garbage-collection time, which is easy to miss. Make it loud."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Background task %r crashed and will NOT restart: %s", task.get_name(), exc, exc_info=exc)
        state.feed_status["bybit"] = f"crashed: {exc} (restart the app)"


@app.on_event("startup")
async def startup() -> None:
    bybit_task = asyncio.create_task(bybit_feed.run(), name="bybit_feed")
    bybit_task.add_done_callback(_log_if_crashed)
    _background_tasks.append(bybit_task)
    _background_tasks.append(asyncio.create_task(_heartbeat()))
    # Independent of the Bybit feed's own startup/reconnect cycle -- plain
    # REST calls, not the WebSocket -- so it starts right away and just
    # no-ops on every tick until a Spotlight symbol is actually set.
    _background_tasks.append(asyncio.create_task(bybit_feed.spotlight_loop(), name="spotlight_refresh"))


async def _heartbeat(interval: float = 5.0) -> None:
    """Broadcast periodically even if a feed is quiet (e.g. equities after
    hours), so the UI's "last update" / connection indicators stay honest."""
    while True:
        await asyncio.sleep(interval)
        await state.broadcast()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    state.clients.add(websocket)
    try:
        await websocket.send_text(__import__("json").dumps(state.snapshot()))
        while True:
            # we don't expect messages from the client; just keep the socket open
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(websocket)


# ---------------------------------------------------------------------------
# Paper (fictional) positions -- "Tracker Positions"
# ---------------------------------------------------------------------------

@app.post("/api/positions/open")
async def api_open_position(request: Request) -> dict:
    body = await request.json()
    symbol = str(body.get("symbol", "")).strip().upper()
    side = str(body.get("side", "")).strip().lower()
    if not symbol:
        raise HTTPException(400, "Enter a symbol.")
    if side not in ("long", "short"):
        raise HTTPException(400, "Side must be 'long' or 'short'.")

    st = state.symbols.get(symbol)
    price = st.price if st and st.price > 0 else None
    if price is None:
        # Symbol isn't already streaming (outside the deep watchlist) --
        # look up its current price directly so the position can open
        # immediately rather than waiting on a subscription.
        price = await asyncio.get_event_loop().run_in_executor(None, bybit_feed.fetch_last_price, symbol)
        if price is None:
            raise HTTPException(400, f"Couldn't find a live Bybit spot price for \"{symbol}\" — check the symbol (e.g. BTCUSDT).")
        bybit_feed.track_extra_symbol(symbol, price)

    # Capture what the desk was saying RIGHT NOW, before the outcome exists.
    # This is what makes the tracker a forward test of the app's own signals
    # rather than just a trade diary -- see positions.py's scorecard().
    pos = position_book.open(
        symbol, side, price,
        cost_pct=config.round_trip_cost_pct,
        signal_context=_capture_signal_context(symbol),
    )
    log.info("Opened paper position %s: %s %s @ %s (context: %s)",
             pos.id, side, symbol, price, pos.signal_context.get("bucket_hint", "none"))
    await state.broadcast()
    return pos.to_dict(price)


def _capture_signal_context(symbol: str) -> dict:
    """A snapshot of every call the desk is making about `symbol` at this
    instant. Kept small and flat: this is stored per position and read back
    into the scorecard, so it wants to be stable and cheap, not exhaustive."""
    context: dict = {"captured_at": time.time()}

    st = state.symbols.get(symbol)
    if st is not None:
        context.update({
            "heat": st.heat,
            "likelihood": st.likelihood,
            "direction": st.direction,
            "rsi14": st.rsi14,
            "tags": list(st.tags or []),
        })
        if st.confluence:
            context["confluence_label"] = st.confluence.get("label")
            context["confluence_agree"] = f"{st.confluence.get('agree')}/{st.confluence.get('total')}"
        if st.positioning:
            context["positioning_lean"] = st.positioning.get("lean")
            context["positioning_confidence"] = st.positioning.get("confidence")
        if st.funding:
            context["funding_level"] = st.funding.get("level")
            context["funding_annual_pct"] = st.funding.get("annual_pct")

    # Spotlight only applies if this is the symbol under the microscope.
    spotlight = state.spotlight
    if spotlight and spotlight.get("symbol") == symbol:
        interp = spotlight.get("interpretation") or {}
        context.update({
            "spotlight_bias": interp.get("bias"),
            "spotlight_confidence": interp.get("confidence"),
            "spotlight_pattern": interp.get("pattern"),
        })
        cost_check = interp.get("cost_check")
        if cost_check:
            context["cost_verdict"] = cost_check.get("verdict")

    context["bucket_hint"] = (
        f"spotlight:{context.get('spotlight_bias')}/{context.get('spotlight_confidence')}"
        if context.get("spotlight_bias")
        else f"watchlist:{context.get('direction')}/{context.get('likelihood')}"
    )
    return context


@app.post("/api/positions/{position_id}/close")
async def api_close_position(position_id: str) -> dict:
    pos = position_book.positions.get(position_id)
    if pos is None:
        raise HTTPException(404, "Position not found.")
    if pos.status != "open":
        raise HTTPException(400, "Position is already closed.")
    st = state.symbols.get(pos.symbol)
    price = st.price if st and st.price > 0 else pos.entry_price  # shouldn't normally happen; never block a close
    closed = position_book.close(position_id, price)
    log.info("Closed paper position %s: %s %s @ %s (gross %.2f%% / net %.2f%% after %.2f%% costs)",
             pos.id, pos.side, pos.symbol, price,
             closed.gross_pnl_pct(), closed.net_pnl_pct(), closed.cost_pct)
    await state.broadcast()
    return closed.to_dict()


# ---------------------------------------------------------------------------
# Spotlight -- one symbol, ultra-frequent 1m-12h analysis
# ---------------------------------------------------------------------------

@app.post("/api/spotlight")
async def api_set_spotlight(request: Request) -> dict:
    body = await request.json()
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(400, "Enter a symbol.")
    try:
        # Fetches 9 timeframes and runs the full indicator suite on each --
        # genuinely takes a couple of seconds, so it's offloaded to a
        # worker thread rather than blocking the event loop, same as the
        # rest of this app's REST/pandas work.
        payload = await asyncio.get_event_loop().run_in_executor(None, bybit_feed.set_spotlight, symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Spotlight set to %s", symbol)
    await state.broadcast()
    return payload


@app.post("/api/spotlight/clear")
async def api_clear_spotlight() -> dict:
    bybit_feed.clear_spotlight()
    await state.broadcast()
    return {"status": "cleared"}
