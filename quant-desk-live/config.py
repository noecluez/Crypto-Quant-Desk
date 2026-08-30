"""Loads configuration from environment / .env. Nothing here talks to the network."""
import os
from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


class Config:
    CALLMEBOT_API_KEY = os.getenv("CALLMEBOT_API_KEY", "")
    WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")

    CRYPTO_SYMBOLS = _list("CRYPTO_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")

    # --- Which Bybit market to analyze ---
    # "linear" = USDT perpetual futures (the default, and where leverage
    # actually lives): unlocks funding rates, open interest, liquidations and
    # the long/short account ratio, none of which exist on spot. "spot" still
    # works if you'd rather analyze the spot market, but the positioning
    # panels will simply report "not available on spot" rather than guess.
    # NOTE: as of v8 (2026-08-30) this app CAN place real orders, on linear
    # only, when EXECUTION_ENABLED=true -- see "LIVE EXECUTION" below and the
    # README. Analysis and Tracker Positions work the same regardless.
    BYBIT_CATEGORY = os.getenv("BYBIT_CATEGORY", "linear").strip().lower()

    PORT = int(os.getenv("PORT", "8000"))

    ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "30"))
    RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70"))
    RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))

    # --- Deep technical-analysis scope ---
    # Your pinned CRYPTO_SYMBOLS above always get the full multi-timeframe
    # treatment. On top of those, the app scans the wider Bybit spot market
    # and pulls in the most-interesting *discovered* pairs (by 24h % move,
    # above a liquidity floor) to fill out the watchlist — this is what
    # decides which pairs make the "Also Watching Today" panel.
    UNIVERSE_SCAN_ENABLED = os.getenv("UNIVERSE_SCAN_ENABLED", "true").lower() in ("1", "true", "yes")
    UNIVERSE_MIN_TURNOVER_USDT = float(os.getenv("UNIVERSE_MIN_TURNOVER_USDT", "5000000"))
    DEEP_WATCHLIST_SIZE = int(os.getenv("DEEP_WATCHLIST_SIZE", "20"))  # pinned + discovered, combined
    UNIVERSE_RESCAN_SECONDS = int(os.getenv("UNIVERSE_RESCAN_SECONDS", "1800"))  # how often to re-scan the wider market
    DEEP_REFRESH_SECONDS = int(os.getenv("DEEP_REFRESH_SECONDS", "300"))  # how often to refresh multi-timeframe TA

    # --- Spotlight: one symbol at a time, ultra-frequent 1m-12h analysis ---
    # For actively trading a single pair on low timeframes with leverage --
    # a much tighter refresh cadence and a much finer timeframe ladder than
    # the rest of the watchlist gets, plus a plain-English interpretation of
    # what the numbers imply short-term (see analysis/spotlight.py).
    SPOTLIGHT_REFRESH_SECONDS = int(os.getenv("SPOTLIGHT_REFRESH_SECONDS", "180"))
    # Feed the Spotlight symbol's fastest timeframes straight off the
    # WebSocket (Bybit's kline.{1,3,5} topics) instead of waiting for the
    # REST cycle above. Without this a "1m" reading can be three candles
    # stale, which defeats the point of a low-timeframe panel.
    SPOTLIGHT_LIVE_KLINES = os.getenv("SPOTLIGHT_LIVE_KLINES", "true").lower() in ("1", "true", "yes")

    # --- Derivatives positioning (linear/perp only) ---
    # Funding, open interest and the long/short account ratio move on the
    # order of hours, so they get their own slower refresh loop. Liquidations
    # arrive live over the WebSocket and are kept in a rolling window.
    DERIVATIVES_REFRESH_SECONDS = int(os.getenv("DERIVATIVES_REFRESH_SECONDS", "300"))
    # Annualized funding (%) beyond which positioning counts as "crowded".
    # Bybit funds every 8h, so 0.01% per period ~= 10.95% annualized is the
    # long-run neutral-ish baseline; 30%+ annualized is genuinely stretched.
    FUNDING_EXTREME_ANNUAL_PCT = float(os.getenv("FUNDING_EXTREME_ANNUAL_PCT", "30"))
    FUNDING_ELEVATED_ANNUAL_PCT = float(os.getenv("FUNDING_ELEVATED_ANNUAL_PCT", "15"))
    # How far back to look when judging "is a liquidation cascade happening".
    LIQUIDATION_WINDOW_MINUTES = int(os.getenv("LIQUIDATION_WINDOW_MINUTES", "15"))
    # USD notional of liquidations inside that window that counts as a cascade.
    LIQUIDATION_CASCADE_USD = float(os.getenv("LIQUIDATION_CASCADE_USD", "1000000"))

    # --- Trading-cost model (analysis only -- this app never places orders) ---
    # Every "what would this have returned" number in the app is reported net
    # of these, because a signal that only wins before costs is not a signal.
    # Bybit's standard perp taker fee is 0.055%; SLIPPAGE_PCT is your own
    # estimate of how far from the quoted price a market order actually fills.
    TAKER_FEE_PCT = float(os.getenv("TAKER_FEE_PCT", "0.055"))
    SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "0.02"))
    # Purely for framing the cost in leverage terms in the UI -- it does not
    # change any percentage, since a % return on notional is leverage-neutral.
    ASSUMED_LEVERAGE = float(os.getenv("ASSUMED_LEVERAGE", "10"))
    # What fraction of account equity a single Tracker Position is assumed to
    # use as margin. This app never allocates or holds real capital -- it's
    # purely a sizing assumption applied uniformly across the whole closed
    # track record so "cumulative P&L" can be shown as a % of account rather
    # than a leverage-neutral price-move %. Combined with ASSUMED_LEVERAGE: a
    # 2.5% net move on a position sized at 5% of account at 10x = 1.25% of
    # account. Unlike cost_pct, this is not locked in per-trade -- it's a
    # "what if I sized this way" framing choice, not a cost actually paid, so
    # changing it recomputes the whole cumulative figure rather than only
    # applying to new positions.
    POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "5"))

    # -----------------------------------------------------------------
    # LIVE EXECUTION (Bybit futures) -- v8, 2026-08-30.
    # Everything below this line is new: real API keys, real orders, real
    # leverage. EXECUTION_ENABLED defaults to false so upgrading this app
    # never silently starts trading -- it must be turned on explicitly.
    # See README.md "Live execution" for the full safety model.
    # -----------------------------------------------------------------
    EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes")
    # true = https://api-testnet.bybit.com (fake funds). false = real mainnet
    # money. Get this working end-to-end on testnet first -- there is no
    # supported path that skips it.
    BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")
    BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
    BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

    # --- Risk limits (hard caps, enforced in execution/risk.py) ---
    EXECUTION_MAX_LEVERAGE = float(os.getenv("EXECUTION_MAX_LEVERAGE", "10"))
    EXECUTION_MIN_LEVERAGE = float(os.getenv("EXECUTION_MIN_LEVERAGE", "3"))
    EXECUTION_RISK_PER_TRADE_PCT = float(os.getenv("EXECUTION_RISK_PER_TRADE_PCT", "2"))
    EXECUTION_MAX_CONCURRENT_POSITIONS = int(os.getenv("EXECUTION_MAX_CONCURRENT_POSITIONS", "5"))
    # % of account equity lost in a UTC day that halts all new entries. Never
    # closes existing positions (their own SL/TP still protect them) -- only
    # blocks opening new ones. Stays tripped until re-armed (UI button or
    # POST /api/execution/rearm), even across a day boundary, so a bad night
    # can't be "fixed" by simply waiting for the clock to roll over.
    EXECUTION_DAILY_LOSS_LIMIT_PCT = float(os.getenv("EXECUTION_DAILY_LOSS_LIMIT_PCT", "10"))
    # Minutes to wait before re-entering the same symbol after closing a
    # position on it, win or lose -- stops immediate re-entry into the same
    # whipsaw.
    EXECUTION_SYMBOL_COOLDOWN_MINUTES = int(os.getenv("EXECUTION_SYMBOL_COOLDOWN_MINUTES", "30"))
    # Always isolated margin per position -- one bad trade must never be able
    # to draw down margin shared with any other position.
    EXECUTION_MARGIN_MODE = "isolated"

    # --- Entry gate (see execution/signal_gate.py) ---
    # "Relatively good confluence into the direction of the bias" (the user's
    # own framing, 2026-08-30): the watchlist's score_setup() direction must
    # be a real bias (not "two-sided" -- that already means the RSI-extreme/
    # divergence uncertainty gate is active, see analysis/indicators.py), AND
    # the independent multi-timeframe confluence() read must agree with that
    # direction, AND agree "well enough" per the two knobs below.
    # Minimum agree/total timeframe ratio (e.g. 0.75 = at least 3 of 4).
    EXECUTION_MIN_CONFLUENCE_RATIO = float(os.getenv("EXECUTION_MIN_CONFLUENCE_RATIO", "0.75"))
    # Minimum |confluence score| (-1..1 scale from analysis.indicators.confluence()).
    EXECUTION_MIN_CONFLUENCE_SCORE = float(os.getenv("EXECUTION_MIN_CONFLUENCE_SCORE", "0.35"))
    # A signal-performance bucket (see execution/signal_gate.py) needs at
    # least this many closed trades (paper + live combined) before its
    # historical win-rate/return is trusted enough to block or downsize a
    # trade. Below this, the bucket is treated as "no information" rather
    # than "bad" -- 46 total paper trades exist as of 2026-08-30, so most
    # fine-grained buckets don't clear this yet.
    EXECUTION_MIN_BUCKET_N = int(os.getenv("EXECUTION_MIN_BUCKET_N", "10"))

    # --- Stop loss / take profit placement (execution/order_manager.py) ---
    # Hybrid: ATR sets the volatility-sane base distance; a nearby strong S/R
    # level tightens or widens it. Stop = entry -+ SL_ATR_MULT * ATR(1h),
    # unless a qualifying S/R level sits closer, per SL_SR_SNAP_MAX_PCT.
    EXECUTION_SL_ATR_MULT = float(os.getenv("EXECUTION_SL_ATR_MULT", "1.5"))
    EXECUTION_TP_ATR_MULT = float(os.getenv("EXECUTION_TP_ATR_MULT", "3.0"))
    EXECUTION_MIN_REWARD_RISK = float(os.getenv("EXECUTION_MIN_REWARD_RISK", "2.0"))
    # Only snap the stop to a support/resistance level if doing so keeps the
    # stop within this much of the ATR-based distance either way (percent of
    # entry price) -- prevents an S/R level that happens to be very close or
    # very far away from producing a nonsensical stop.
    EXECUTION_SL_SR_SNAP_MAX_PCT = float(os.getenv("EXECUTION_SL_SR_SNAP_MAX_PCT", "50"))
    # Once a position reaches this many multiples of its own initial risk (R)
    # in profit, the fixed stop loss is replaced with an exchange-native
    # trailing stop (see order_manager.py's "handoff" -- deliberately never
    # both at once, to avoid any ambiguity about which one the exchange
    # honors first). The position stays protected by the fixed SL the whole
    # time up to that point.
    EXECUTION_TRAILING_ACTIVATE_R = float(os.getenv("EXECUTION_TRAILING_ACTIVATE_R", "1.0"))
    EXECUTION_TRAILING_ATR_MULT = float(os.getenv("EXECUTION_TRAILING_ATR_MULT", "1.0"))

    # --- Cost buffer (execution/costs.py) ---
    # Added ON TOP of the existing round_trip_cost_pct (measured taker fee +
    # slippage) for every execution decision -- the entry cost-viability
    # check, and take-profit sizing. Pure safety margin so a real trade has
    # to clear its true cost estimate *plus* this before it's judged
    # profitable, absorbing fee-tier surprises, funding paid while holding,
    # and slippage worse than SLIPPAGE_PCT assumes. Requested by the user
    # 2026-08-30. Never applied to the paper Tracker Positions' own cost_pct
    # (that stays exactly the analysis-honesty model it always was).
    EXECUTION_EXTRA_COST_BUFFER_PCT = float(os.getenv("EXECUTION_EXTRA_COST_BUFFER_PCT", "0.3"))

    # --- Reconciliation / monitoring loop cadence ---
    EXECUTION_MONITOR_SECONDS = int(os.getenv("EXECUTION_MONITOR_SECONDS", "20"))

    @property
    def round_trip_cost_pct(self) -> float:
        """Entry + exit, fee + slippage on each side."""
        return 2 * (self.TAKER_FEE_PCT + self.SLIPPAGE_PCT)

    @property
    def execution_cost_pct(self) -> float:
        """round_trip_cost_pct plus the extra safety buffer -- what
        execution/signal_gate.py and order_manager.py actually gate and size
        against. Kept separate from round_trip_cost_pct (used everywhere
        else, including the paper Tracker Positions) rather than changing
        that shared property, so the buffer only affects real orders."""
        return self.round_trip_cost_pct + self.EXECUTION_EXTRA_COST_BUFFER_PCT

    @property
    def bybit_rest_base(self) -> str:
        return "https://api-testnet.bybit.com" if self.BYBIT_TESTNET else "https://api.bybit.com"

    @property
    def execution_configured(self) -> bool:
        return bool(self.BYBIT_API_KEY and self.BYBIT_API_SECRET)

    @property
    def is_derivatives(self) -> bool:
        return self.BYBIT_CATEGORY in ("linear", "inverse")

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.CALLMEBOT_API_KEY and self.WHATSAPP_PHONE)


config = Config()
