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
    # NOTE: this app never places an order on either market -- see the README.
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

    @property
    def round_trip_cost_pct(self) -> float:
        """Entry + exit, fee + slippage on each side."""
        return 2 * (self.TAKER_FEE_PCT + self.SLIPPAGE_PCT)

    @property
    def is_derivatives(self) -> bool:
        return self.BYBIT_CATEGORY in ("linear", "inverse")

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.CALLMEBOT_API_KEY and self.WHATSAPP_PHONE)


config = Config()
