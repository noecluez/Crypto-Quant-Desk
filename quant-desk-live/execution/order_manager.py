"""Owns the full lifecycle of a REAL Bybit position: sizing, placing the
entry order with attached SL/TP, the trailing-stop hand-off once a trade is
in enough profit, detecting exchange-side closes (SL/TP/trailing fired) and
reconciling this app's bookkeeping to match, startup reconciliation against
whatever Bybit actually shows, and the emergency flatten-everything path.

Every write to Bybit goes through execution/bybit_client.py. Every
persisted fact about a real position goes through
execution/live_positions.py. This module is the orchestration layer between
them plus the signal gate (execution/signal_gate.py) and risk math
(execution/risk.py) -- it holds no state of its own beyond a small
instrument-info cache and the lazily-constructed BybitClient.
"""
from __future__ import annotations

import logging
import time

from analysis.indicators import fmt_price
from execution import risk, signal_gate
from execution.bybit_client import BybitClient, BybitAPIError
from execution.costs import execution_round_trip_cost_pct, is_trade_cost_viable, reward_risk_ratio
from execution.live_positions import live_position_book
from execution.risk import circuit_breaker

log = logging.getLogger("execution")

_client: BybitClient | None = None
_instrument_cache: dict[str, dict] = {}


def get_client(cfg) -> BybitClient:
    global _client
    if _client is None:
        if not cfg.execution_configured:
            raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET not set -- cannot use live execution")
        _client = BybitClient(api_key=cfg.BYBIT_API_KEY, api_secret=cfg.BYBIT_API_SECRET, base_url=cfg.bybit_rest_base)
    return _client


def _instrument(cfg, symbol: str) -> dict:
    """qtyStep/minOrderQty/tickSize/maxLeverage for `symbol`, cached for the
    process lifetime -- these don't change often enough to justify a fresh
    call on every trade, and a stale cache just means slightly-conservative
    rounding, never an invalid order."""
    cached = _instrument_cache.get(symbol)
    if cached is not None:
        return cached
    client = get_client(cfg)
    raw = client.instrument_info(symbol, category=cfg.BYBIT_CATEGORY)
    lot = raw.get("lotSizeFilter", {})
    price_filter = raw.get("priceFilter", {})
    leverage_filter = raw.get("leverageFilter", {})
    info = {
        "qty_step": float(lot.get("qtyStep", "0.001")),
        "min_qty": float(lot.get("minOrderQty", "0.001")),
        "tick_size": float(price_filter.get("tickSize", "0.01")),
        "max_leverage": float(leverage_filter.get("maxLeverage", cfg.EXECUTION_MAX_LEVERAGE)),
    }
    _instrument_cache[symbol] = info
    return info


# ---------------------------------------------------------------------------
# Stop loss / take profit placement -- hybrid ATR + support/resistance
# ---------------------------------------------------------------------------

def compute_stop_target(st, side: str, cfg) -> tuple[float | None, float | None, str]:
    """Returns (stop_price, target_price, note). note is empty on success,
    otherwise the reason a stop couldn't be computed (caller must reject the
    trade in that case -- there is no such thing as an unprotected position
    in this app).

    See config.py's EXECUTION_SL_ATR_MULT/_TP_ATR_MULT/_SL_SR_SNAP_MAX_PCT
    docstrings for the parameters. Levels in st.support/st.resistance are
    already sorted nearest-first (analysis/indicators.py's cluster_levels)."""
    price = st.price
    tf = (st.timeframes or {}).get("1h") or (st.timeframes or {}).get("4h")
    atr_val = tf.get("atr") if tf else None
    if not atr_val or atr_val <= 0 or price <= 0:
        return None, None, "no usable ATR (1h/4h) available yet to size a stop"

    sl_distance = cfg.EXECUTION_SL_ATR_MULT * atr_val
    tp_distance = cfg.EXECUTION_TP_ATR_MULT * atr_val

    if side == "long":
        stop = price - sl_distance
        target = price + tp_distance
        levels_for_stop, levels_for_target = (st.support or []), (st.resistance or [])
    else:
        stop = price + sl_distance
        target = price - tp_distance
        levels_for_stop, levels_for_target = (st.resistance or []), (st.support or [])

    # Snap the stop to a nearby well-tested level if one sits close enough to
    # the ATR-implied distance to be meaningful, rather than blindly past it.
    snap_tolerance = sl_distance * (cfg.EXECUTION_SL_SR_SNAP_MAX_PCT / 100.0)
    best_level = None
    for lvl in levels_for_stop:
        lvl_distance = abs(lvl["price"] - price)
        if abs(lvl_distance - sl_distance) <= snap_tolerance:
            if best_level is None or lvl.get("touches", 0) > best_level.get("touches", 0):
                best_level = lvl
    if best_level is not None:
        buffer = atr_val * 0.1  # a little room beyond the level itself, so normal noise at the level doesn't stop us out early
        stop = (best_level["price"] - buffer) if side == "long" else (best_level["price"] + buffer)

    # Extend the target to the nearest qualifying resistance/support beyond
    # the ATR-implied one, if it exists and still clears the minimum R:R.
    risk_after_snap = abs(price - stop)
    for lvl in levels_for_target:
        lvl_price = lvl["price"]
        farther = (lvl_price > target) if side == "long" else (lvl_price < target)
        if farther and risk_after_snap > 0:
            candidate_rr = abs(lvl_price - price) / risk_after_snap
            if candidate_rr >= cfg.EXECUTION_MIN_REWARD_RISK:
                target = lvl_price
            break  # nearest-first ordering -- only the first qualifying level is considered

    risk_dist = abs(price - stop)
    if risk_dist <= 0:
        return None, None, "computed a zero-distance stop"
    rr = abs(target - price) / risk_dist
    if rr < cfg.EXECUTION_MIN_REWARD_RISK:
        # Fall back to the ATR-implied target directly rather than whatever
        # S/R produced, so the minimum reward:risk is always guaranteed.
        target = price + cfg.EXECUTION_MIN_REWARD_RISK * risk_dist if side == "long" else price - cfg.EXECUTION_MIN_REWARD_RISK * risk_dist

    return stop, target, ""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def evaluate_and_maybe_enter(symbol: str, st, cfg) -> None:
    """Called once per symbol per deep-refresh cycle (see
    feeds/bybit_feed.py's _deep_refresh_all). Every early-return here is a
    deliberate skip, not an error -- most symbols, most cycles, will not
    trade, and that is the system working as intended, not a bug."""
    if not cfg.EXECUTION_ENABLED:
        return
    if not cfg.execution_configured:
        log.warning("EXECUTION_ENABLED=true but BYBIT_API_KEY/SECRET are not set -- skipping all execution")
        return
    if circuit_breaker.is_halted():
        return
    if live_position_book.open_position_for_symbol(symbol) is not None:
        return
    if len(live_position_book.open_positions()) >= cfg.EXECUTION_MAX_CONCURRENT_POSITIONS:
        return
    last_closed = live_position_book.last_closed_time_for_symbol(symbol)
    if last_closed is not None and (time.time() - last_closed) < cfg.EXECUTION_SYMBOL_COOLDOWN_MINUTES * 60:
        return

    gate = signal_gate.evaluate_entry(symbol, st, cfg)
    if not gate.approved:
        log.debug("Execution gate declined %s: %s", symbol, gate.reason)
        return

    side = gate.side  # "long" | "short"
    stop, target, note = compute_stop_target(st, side, cfg)
    if stop is None:
        log.info("Execution: %s passed the signal gate but no stop could be computed (%s) -- skipping", symbol, note)
        return

    cost_pct = execution_round_trip_cost_pct(cfg.TAKER_FEE_PCT, cfg.SLIPPAGE_PCT, cfg.EXECUTION_EXTRA_COST_BUFFER_PCT)
    viable, reason = is_trade_cost_viable(st.price, target, cost_pct)
    if not viable:
        log.info("Execution: %s rejected on cost viability -- %s", symbol, reason)
        return

    try:
        _open_position(symbol, st, side, stop, target, cost_pct, gate, cfg)
    except BybitAPIError as exc:
        log.error("Execution: order placement failed for %s: %s", symbol, exc)
        from state import state
        state.log_alert(f"[Execution] FAILED to open {side} {symbol}: {exc}")
    except Exception:
        log.exception("Execution: unexpected error opening %s", symbol)


def _open_position(symbol: str, st, side: str, stop: float, target: float, cost_pct: float, gate, cfg) -> None:
    from state import state  # local import: avoids a state<->execution import cycle, same pattern as state.py's own snapshot()

    client = get_client(cfg)
    instrument = _instrument(cfg, symbol)
    equity = client.equity_usdt()

    tf = (st.timeframes or {}).get("1h") or (st.timeframes or {}).get("4h") or {}
    atr_val = tf.get("atr")
    atr_pct = (atr_val / st.price * 100) if atr_val and st.price > 0 else None
    leverage = risk.choose_leverage(
        confluence_score_abs=gate.debug.get("score", 0.0), atr_pct_of_price=atr_pct,
        min_leverage=cfg.EXECUTION_MIN_LEVERAGE, max_leverage=min(cfg.EXECUTION_MAX_LEVERAGE, instrument["max_leverage"]),
    )

    sizing = risk.position_qty_from_risk(
        equity_usdt=equity, risk_pct=cfg.EXECUTION_RISK_PER_TRADE_PCT,
        entry_price=st.price, stop_price=stop, leverage=leverage,
        qty_step=instrument["qty_step"], min_qty=instrument["min_qty"],
    )
    if not sizing.ok:
        log.info("Execution: %s sizing rejected -- %s", symbol, sizing.reason)
        return

    client.switch_isolated_margin(symbol, leverage, category=cfg.BYBIT_CATEGORY)
    client.set_leverage(symbol, leverage, category=cfg.BYBIT_CATEGORY)

    tick = instrument["tick_size"]
    stop_str = risk.format_step(stop, tick, mode="nearest")
    target_str = risk.format_step(target, tick, mode="nearest")
    order_side = "Buy" if side == "long" else "Sell"

    result = client.place_market_order(
        symbol=symbol, side=order_side, qty=sizing.qty_str, category=cfg.BYBIT_CATEGORY,
        stop_loss=stop_str, take_profit=target_str,
    )
    order_id = result.get("orderId", "")

    signal_context = {
        "direction": st.direction, "likelihood": st.likelihood, "heat": st.heat,
        "confluence_label": (st.confluence or {}).get("label"),
        "confluence_agree": f"{gate.debug.get('agree')}/{gate.debug.get('total')}",
        "confluence_score": gate.debug.get("score"),
        "side": side, "leverage": leverage, "atr_pct": atr_pct,
        "captured_at": time.time(),
    }
    pos = live_position_book.open(
        symbol=symbol, side=side, order_id=order_id, entry_price=st.price,
        qty=sizing.qty, leverage=leverage, margin_usdt=sizing.margin_usdt,
        risk_usdt=sizing.risk_usdt, equity_at_open=equity,
        initial_stop_loss=float(stop_str), take_profit=float(target_str),
        cost_pct=cost_pct, signal_context=signal_context,
    )
    log.info(
        "Execution: OPENED %s %s qty=%s @ %s | SL=%s TP=%s lev=%sx margin=$%.2f risk=$%.2f (order %s)",
        side, symbol, sizing.qty_str, fmt_price(st.price), stop_str, target_str, leverage,
        sizing.margin_usdt, sizing.risk_usdt, order_id,
    )
    state.log_alert(
        f"[Execution] Opened {side.upper()} {symbol} @ {fmt_price(st.price)} | "
        f"SL {fmt_price(float(stop_str))} / TP {fmt_price(float(target_str))} | {leverage}x, ${sizing.margin_usdt:.2f} margin"
    )
    _maybe_whatsapp(cfg, pos.id, (
        f"[Quant Desk LIVE] Opened {side.upper()} {symbol} @ {fmt_price(st.price)} on {'TESTNET' if cfg.BYBIT_TESTNET else 'MAINNET'}. "
        f"SL {fmt_price(float(stop_str))}, TP {fmt_price(float(target_str))}, {leverage}x leverage, "
        f"${sizing.margin_usdt:.2f} margin, ${sizing.risk_usdt:.2f} at risk. "
        f"Signal: {st.direction}/{st.likelihood}, confluence {gate.debug.get('agree')}/{gate.debug.get('total')}."
    ))


def _maybe_whatsapp(cfg, unique_key: str, message: str) -> None:
    from alerts.whatsapp import maybe_alert
    maybe_alert(f"exec:{unique_key}", message)


# ---------------------------------------------------------------------------
# Monitoring: trailing-stop hand-off + detecting exchange-side closes
# ---------------------------------------------------------------------------

def run_monitor_cycle(cfg) -> None:
    """Runs every config.EXECUTION_MONITOR_SECONDS. Two jobs:
    1. For each open live position in decent profit, hand off the fixed
       stop loss to an exchange-native trailing stop (see the docstring on
       BybitClient.set_trading_stop for why this is a hand-off rather than
       setting both at once).
    2. Detect positions Bybit has already closed (SL/TP/trailing fired, or a
       liquidation) that this app still shows as open, and reconcile.
    """
    if not cfg.EXECUTION_ENABLED or not cfg.execution_configured:
        return
    open_positions = live_position_book.open_positions()
    if not open_positions:
        return

    try:
        client = get_client(cfg)
        exchange_positions = {p["symbol"]: p for p in client.positions(category=cfg.BYBIT_CATEGORY) if float(p.get("size") or 0) != 0}
    except Exception:
        log.warning("Execution monitor: couldn't fetch exchange positions this cycle", exc_info=True)
        return

    from state import state

    for pos in open_positions:
        exch = exchange_positions.get(pos.symbol)
        if exch is None:
            _reconcile_closed(pos, cfg)
            continue

        if not pos.trailing_active:
            _maybe_handoff_to_trailing(pos, exch, cfg)


def _maybe_handoff_to_trailing(pos, exch: dict, cfg) -> None:
    from state import state

    mark_price = exch.get("markPrice")
    if not mark_price:
        return
    mark_price = float(mark_price)
    risk_dist = abs(pos.entry_price - pos.initial_stop_loss)
    if risk_dist <= 0:
        return
    favorable_move = (mark_price - pos.entry_price) if pos.side == "long" else (pos.entry_price - mark_price)
    r_multiple = favorable_move / risk_dist
    if r_multiple < cfg.EXECUTION_TRAILING_ACTIVATE_R:
        return

    st = state.symbols.get(pos.symbol)
    tf = ((st.timeframes if st else None) or {}).get("1h") or ((st.timeframes if st else None) or {}).get("4h") or {}
    atr_val = tf.get("atr")
    if not atr_val or atr_val <= 0:
        return  # wait for a cycle where ATR is available rather than guessing a trailing distance

    trailing_distance = atr_val * cfg.EXECUTION_TRAILING_ATR_MULT
    try:
        client = get_client(cfg)
        instrument = _instrument(cfg, pos.symbol)
        tick = instrument["tick_size"]
        # Hand-off, in order: cancel the fixed stop first, THEN set the
        # trailing stop -- so if the process dies between the two calls, the
        # position is still protected by whichever stop is currently set
        # (never neither). See BybitClient.set_trading_stop's docstring.
        client.set_trading_stop(symbol=pos.symbol, category=cfg.BYBIT_CATEGORY, stop_loss="0")
        client.set_trading_stop(
            symbol=pos.symbol, category=cfg.BYBIT_CATEGORY,
            trailing_stop=risk.format_step(trailing_distance, tick, mode="nearest"),
            active_price=risk.format_step(mark_price, tick, mode="nearest"),
        )
    except BybitAPIError as exc:
        log.error("Execution: trailing-stop hand-off failed for %s: %s", pos.symbol, exc)
        return

    live_position_book.mark_trailing_active(pos.id)
    log.info("Execution: %s trailing stop activated at %.2fR (distance %.6g)", pos.symbol, r_multiple, trailing_distance)
    state.log_alert(f"[Execution] {pos.symbol} reached {r_multiple:.1f}R -- switched to trailing stop (protecting gains)")


def _reconcile_closed(pos, cfg) -> None:
    """Bybit no longer shows this position open -- its SL, TP, or trailing
    stop fired (or it was liquidated) while we weren't looking. Close the
    local record using the best price information available so the
    signal-performance gate and circuit breaker still see an accurate
    outcome, rather than leaving a closed real trade marked "open" forever."""
    from state import state

    st = state.symbols.get(pos.symbol)
    exit_price = st.price if st and st.price > 0 else pos.entry_price
    # Best-effort classification -- purely cosmetic (shown in the UI/log);
    # the P&L math doesn't depend on getting this label exactly right.
    if pos.trailing_active:
        reason = "trailing_stop"
    elif pos.side == "long":
        reason = "take_profit" if exit_price >= pos.take_profit else "stop_loss"
    else:
        reason = "take_profit" if exit_price <= pos.take_profit else "stop_loss"

    closed = live_position_book.close(pos.id, exit_price, reason)
    if closed is None:
        return
    account_pct = closed.account_pnl_pct() or 0.0
    circuit_breaker.record_realized_pnl_pct(account_pct, cfg.EXECUTION_DAILY_LOSS_LIMIT_PCT)

    log.info("Execution: reconciled %s as CLOSED (%s) @ %s, account P&L %.3f%%", pos.symbol, reason, fmt_price(exit_price), account_pct)
    state.log_alert(f"[Execution] {pos.symbol} closed ({reason}) @ {fmt_price(exit_price)} — account P&L {account_pct:+.2f}%")
    if circuit_breaker.is_halted():
        state.log_alert(f"[Execution] CIRCUIT BREAKER TRIPPED: {circuit_breaker.state.halted_reason}")
    _maybe_whatsapp(cfg, f"close:{pos.id}", (
        f"[Quant Desk LIVE] {pos.symbol} {pos.side.upper()} closed ({reason}) @ {fmt_price(exit_price)}. "
        f"Account P&L {account_pct:+.2f}%. Today's cumulative: {circuit_breaker.state.daily_pnl_pct:+.2f}%."
        + (f" CIRCUIT BREAKER TRIPPED -- new entries halted until re-armed." if circuit_breaker.is_halted() else "")
    ))


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile_on_startup(cfg) -> None:
    """Runs once, on app startup, before anything else touches execution.
    Never guesses: a live-book "open" record with nothing matching on the
    exchange gets closed using the last known price (same logic as
    _reconcile_closed). An exchange position with NOTHING in the live book
    (opened manually, or by a version of this app that lost its state file)
    is left entirely alone on the exchange -- we do not touch a position we
    have no record of -- but halts the circuit breaker so a human looks at
    it before any new automated entries happen."""
    if not cfg.EXECUTION_ENABLED or not cfg.execution_configured:
        return
    try:
        client = get_client(cfg)
        exchange_positions = {p["symbol"]: p for p in client.positions(category=cfg.BYBIT_CATEGORY) if float(p.get("size") or 0) != 0}
    except Exception:
        log.error("Execution: startup reconciliation could not reach Bybit -- halting new entries until this succeeds", exc_info=True)
        circuit_breaker.halt_manually("Startup reconciliation failed to reach Bybit -- see logs")
        return

    for pos in live_position_book.open_positions():
        if pos.symbol not in exchange_positions:
            _reconcile_closed(pos, cfg)

    known_symbols = {p.symbol for p in live_position_book.open_positions()}
    unexplained = set(exchange_positions) - known_symbols
    if unexplained:
        log.error("Execution: found open exchange position(s) with no local record: %s -- halting new entries", unexplained)
        circuit_breaker.halt_manually(f"Unexplained open position(s) on Bybit not in live_positions.json: {sorted(unexplained)} -- check manually before re-arming")


# ---------------------------------------------------------------------------
# Emergency flatten (kill switch)
# ---------------------------------------------------------------------------

def flatten_all(cfg, reason: str = "manual kill switch") -> list[dict]:
    """Closes every open live position at market, immediately. Does not
    disable EXECUTION_ENABLED or re-arm/halt the circuit breaker itself --
    callers (the API endpoint) decide that separately."""
    client = get_client(cfg)
    results = []
    for pos in live_position_book.open_positions():
        try:
            exch_side = "Buy" if pos.side == "long" else "Sell"
            instrument = _instrument(cfg, pos.symbol)
            qty_str = risk.format_step(pos.qty, instrument["qty_step"], mode="floor")
            client.close_position_market(symbol=pos.symbol, side_to_close=exch_side, qty=qty_str, category=cfg.BYBIT_CATEGORY)
            from state import state
            st = state.symbols.get(pos.symbol)
            exit_price = st.price if st and st.price > 0 else pos.entry_price
            closed = live_position_book.close(pos.id, exit_price, reason)
            if closed:
                circuit_breaker.record_realized_pnl_pct(closed.account_pnl_pct() or 0.0, cfg.EXECUTION_DAILY_LOSS_LIMIT_PCT)
            results.append({"symbol": pos.symbol, "ok": True})
            log.warning("Execution: flattened %s (%s)", pos.symbol, reason)
        except Exception as exc:
            log.error("Execution: FAILED to flatten %s: %s", pos.symbol, exc)
            results.append({"symbol": pos.symbol, "ok": False, "error": str(exc)})
    return results
