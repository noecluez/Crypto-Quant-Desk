# Quant Desk Live

A local, real-time crypto **analysis** desk for leveraged trading. It reads
Bybit's free public perpetual-futures data and turns it into a picture you
can act on: full multi-timeframe technical analysis (RSI, MACD, Bollinger
Bands, Stochastic, ATR, VWAP, RSI/price divergence, fractal support and
resistance, Fibonacci retracement, multi-timeframe confluence), the
**leverage and positioning data that the price chart cannot show you**
(funding rates, open interest, live liquidations, long/short account ratio),
a cost-adjusted historical backtest, a live-updating web UI, and WhatsApp
alerts when something crosses a threshold.

> **This app is an analysis tool. It cannot place a trade.**
> There is no order-placing code anywhere in it, it holds no exchange
> credentials, and it never asks for an API key. "Tracker Positions" are
> simulated arithmetic against live prices — a way to test your own calls,
> not a way to execute them. Everything it reads is public market data.

Three things make it more than an indicator dashboard:

1. **Spotlight** — pick one symbol and get 1m/3m/5m/10m/15m/30m/1h/4h/12h
   analysis on it, with the fastest frames updating live off the WebSocket,
   plus a plain-English read on what it all currently implies short-term.
2. **Positioning data** — funding, open interest, liquidations and crowd
   ratio are genuinely orthogonal to price-derived indicators. RSI and MACD
   are two views of the same price series; funding tells you something the
   chart simply doesn't contain.
3. **It grades its own homework** — every simulated position records what
   the desk was *saying* at the moment it opened, so the Signal Scorecard
   becomes an unbiased forward test of the app's own calls. And every
   return figure anywhere in the app is reported net of trading costs,
   because a signal whose edge is smaller than its costs is not a signal.

The app doesn't just watch the pairs you list — it also scans the wider
Bybit market for the biggest liquid movers of the day and adds the most
interesting ones to a second "Also Watching Today" tier, so you see setups
you didn't think to look for, not just the ones you already had pinned.

**No exchange account or API key of any kind is needed for the market data
itself** — Bybit's public REST and WebSocket endpoints are open to anyone.
The only signup in this whole app is the free, 2-minute WhatsApp opt-in
below.

It runs entirely on your own machine. It holds no trading credentials, can't
place orders, and no data leaves your computer except the read-only
connection to Bybit and the one-line alert text sent through CallMeBot.

## 1. Install

Requires Python 3.10+.

```bash
cd quant-desk-live
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Set up WhatsApp alerts (free, ~2 minutes)

This uses [CallMeBot](https://www.callmebot.com/), a free personal-use
WhatsApp relay — no account, no app to install, no cost:

1. Save **+34 623 75 84 18** to your phone's contacts (any name works).
2. Send it a WhatsApp message: `I allow callmebot to send me messages`
3. Within a couple of minutes you'll get a reply with your personal API key.

CallMeBot is a community-run service intended for exactly this kind of
personal alert — it's not officially affiliated with WhatsApp, so treat it as
"free and convenient" rather than "enterprise-grade." If it ever goes down,
swap `alerts/whatsapp.py`'s `_send_via_callmebot` for a Twilio WhatsApp call —
nothing else in the app needs to change.

You can skip this step and run the app without alerts — it'll just log
"WhatsApp not configured" and keep working as a live dashboard.

## 3. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in:
- `CALLMEBOT_API_KEY` — from step 2 (optional — leave blank to skip alerts)
- `WHATSAPP_PHONE` — your own number, international format, digits only (e.g. `41791234567` for a Swiss number, no `+`)
- Adjust `CRYPTO_SYMBOLS` if you want a different pinned watchlist — any Bybit spot pair works (`BTCUSDT`, `ETHUSDT`, etc.). These always get full deep analysis regardless of what the market scan finds.
- The deep-analysis settings (`UNIVERSE_SCAN_ENABLED`, `UNIVERSE_MIN_TURNOVER_USDT`, `DEEP_WATCHLIST_SIZE`, `UNIVERSE_RESCAN_SECONDS`, `DEEP_REFRESH_SECONDS`) are documented inline in `.env.example` — the defaults (scan on, top 20 pairs total, rescan every 30 min, refresh indicators every 5 min) are sensible for most people, but turn `UNIVERSE_SCAN_ENABLED` off if you only want your pinned pairs analyzed.

**Never commit `.env` or paste its contents anywhere.** `.gitignore` already excludes it.

## 4. Run

```bash
python main.py
```

Then open **http://localhost:8000** in your browser. The page connects over
a local WebSocket and updates itself — no refreshing.

Leave the terminal window running for as long as you want it monitoring.
Stop it with Ctrl+C.

## The technical analysis

For every pair in the deep watchlist (your pinned symbols + the discovered
"also watching" ones), the app tracks four timeframes — 15m, 1h, 4h, 1D —
and on each one computes RSI(14), MACD(12,26,9) with cross detection,
Bollinger Bands(20,2) with squeeze detection, Stochastic(14,3,3), ATR(14),
and (on intraday timeframes) a daily-anchored VWAP. On top of that:

- **Multi-timeframe confluence** — each timeframe gets a bullish/bearish/
  neutral read, weighted by timeframe (1D counts more than 15m) and combined
  into one overall call plus an "X/Y timeframes agree" count. Agreement
  across timeframes is a real signal; a strong read on one 15-minute chart
  alone usually isn't.
- **RSI/price divergence** — flags when price makes a new high/low but RSI
  doesn't confirm it, an early warning that momentum doesn't support the
  move.
- **Support & resistance** — fractal swing-high/low detection on the daily
  chart, clustered into zones and ranked by how many times price has
  respected them.
- **Fibonacci retracement** — automatically anchored to the largest recent
  swing.
- **Signal Track Record** — a real backtest, not a guess: the app scans this
  watchlist's own historical daily bars for every past occurrence of each
  signal type (RSI cross, MACD cross, Bollinger squeeze breakout) and shows
  the actual historical win-rate and average forward return, 5 and 10 days
  out, with the sample size (`n=`) shown honestly so you can judge how much
  to trust it. This refreshes every `DEEP_REFRESH_SECONDS`.

All of this feeds into the same 0–100 "heat" score and Likelihood/Direction
call the dashboard already used — it's just built from a lot more now.
Click any row in either watchlist table to expand the full breakdown.

## What triggers a WhatsApp alert

Checked on every price tick, each rate-limited to one alert per symbol per
reason per `ALERT_COOLDOWN_MINUTES` (default 30):
- RSI(14) crosses into overbought (≥70) or oversold (≤30) territory
- Price pushes within 0.1% of its trailing 52-week high
- RSI/price divergence is detected (bullish or bearish)
- Multi-timeframe confluence reaches "Strong" in either direction
- The heuristic "heat" score reaches **Very High**

Every alert message spells out what the signal implies in plain English —
not just the raw number — and prices are always written as e.g. `USD
78,531.41` (CallMeBot's relay silently strips a leading `$` followed by a
digit, which is why earlier versions of this app sent garbled prices).

Tune the thresholds in `.env`.

## Leverage & positioning (funding, open interest, liquidations, crowd)

By default the app analyzes **USDT perpetual futures** (`BYBIT_CATEGORY=linear`)
rather than spot, because that's where leverage lives — and with it, four
streams of information that don't exist on spot and that no amount of
charting can derive:

- **Funding rate.** Positive funding means longs are paying shorts, i.e.
  more leveraged long demand than short. Sustained high funding means a
  crowded long book — positions paying rent to stay open, and the first to
  be force-closed on a flush. It's shown per-8h and annualized, with a
  "normal / elevated / extreme" call and an explicit note on which side is
  crowded and which direction the squeeze risk points. It is a *positioning*
  read, not a timing signal: crowded can stay crowded for a long time.
- **Open interest, read against price.** This is the single most useful
  derivatives read there is, because the same price move means opposite
  things depending on it. Price up on **rising** OI = new longs opening =
  real conviction. Price up on **falling** OI = shorts covering = a rally
  that runs out of fuel once the trapped shorts are done. Price down on
  rising OI = new shorts = genuine bearish conviction. Price down on falling
  OI = longs being flushed = sharp but self-limiting. The app names which
  of the four you're looking at and explains it in plain English.
- **Live liquidations.** Streamed over the WebSocket as they happen, bucketed
  into a rolling window, split long vs. short, with a cascade flag when the
  notional crosses your threshold. Note the app resolves Bybit's convention
  for you: a "Buy" liquidation order means a *short* was force-closed, which
  is easy to read backwards and flips the whole interpretation.
- **Long/short account ratio.** A contrarian gauge: when a large majority of
  accounts sit on one side, that's where the stops are. Weak on its own,
  meaningful when funding agrees with it.

These are folded into one **positioning read** with its own lean and
confidence. Confidence only rises when several independent inputs agree —
a single signal firing is explicitly not treated as a call.

**How this interacts with the technical read:** positioning can *downgrade*
Spotlight's confidence when it contradicts the chart, but never upgrade it.
Agreement between a fast technical signal and a slow positioning signal is
reassuring, not additive. When they disagree you get an explicit warning
banner rather than a silently averaged number.

Set `BYBIT_CATEGORY=spot` if you'd rather analyze spot; the positioning
panels then say "not available on spot" instead of showing invented numbers.

## Trading costs (why some green numbers turned red)

Every "what would this have returned" figure in the app is reported **net of
a configurable round-trip cost** — `TAKER_FEE_PCT` (0.055% default, Bybit's
standard perp taker fee) plus `SLIPPAGE_PCT`, on both entry and exit. This
changes some numbers from positive to negative, which is exactly the point:
a low-timeframe strategy pays that cost constantly, and an edge smaller than
its cost is not an edge.

Concretely, three things now exist that didn't before:

- **The Signal Track Record shows net win rate and net average return**, and
  flags any signal that doesn't survive costs at any horizon.
- **Tracker Positions show gross and net side by side**, so a +0.10% "win"
  correctly reads as a −0.05% loss after a 0.15% round trip.
- **Spotlight runs a cost check on the setup itself**: if the nearest level
  in your favour is 0.08% away and a round trip costs 0.15%, it says so
  outright — that trade cannot pay for itself no matter how clean the
  technicals look. This is the check most often skipped and most reliably
  paid for.

Percentage returns are leverage-neutral (10x multiplies gain and cost
alike), so `ASSUMED_LEVERAGE` never changes a percentage — it's used only to
express the cost in account terms, which is where it stops feeling abstract:
0.15% of notional at 10x is **1.5% of your account per trade**, win or lose.

## Signal Scorecard — the app grading its own homework

The historical backtest has an honesty problem that can't be fixed from
inside it: the watchlist is populated with pairs chosen *because they
already moved today*, so backtesting signals on their history is textbook
selection bias, and it flatters every number it produces. The backtest now
at least reports its results against the **unconditional base rate** (what
you'd have got entering at random over the same data) so a 60% win rate in
a market that rose 60% of the time is visibly no edge at all.

The Signal Scorecard fixes the problem properly. Every Tracker Position
records what the desk was saying at the instant it opened — the Spotlight
bias and confidence, the heat/likelihood call, the positioning read, the
funding level. When you close it, the outcome is filed under that call. Over
time you get a straight answer to the only question that matters: *when this
app says "aligned bullish, high confidence", what actually happens next?*

That measurement is unbiased by construction, because the signal is recorded
before the outcome exists. It just needs closed positions to become
meaningful, so it starts empty and fills up as you use the app. Thin buckets
are shown with their sample size rather than hidden, and any call that wins
before costs but loses after them is flagged explicitly — that's the single
most valuable thing this app can tell you about your own trading.

## Spotlight (one symbol, ultra-frequent 1m-12h analysis)

The rest of the desk analyzes ~20 pairs on 15m/1h/4h/1D every 5 minutes —
good for scanning a watchlist, too coarse for actively trading one pair on
low timeframes with leverage. **Spotlight** is the other mode: pick one
symbol (type it into the box at the top of the page, or click the 🔦 button
on any watchlist row) and the app switches to tracking just that symbol
across **nine timeframes — 1m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 12h —
refreshed every 3 minutes** (Bybit has no native 10-minute candle; the app
builds it itself by combining pairs of 5-minute candles).

**The fast frames are genuinely live.** 1m, 3m and 5m are fed straight off
Bybit's WebSocket kline stream rather than the 3-minute REST cycle, so a
"1m" reading is actually current instead of up to three candles stale —
which would have defeated the entire point of a low-timeframe panel.
Indicators recompute on candle *close*, not on every in-flight tick: a
half-formed candle's RSI flickers with every trade and isn't a number worth
acting on. Look for the ⚡ badge next to the symbol.

Every one of those 9 timeframes gets the full indicator suite — RSI, MACD,
Bollinger %B and squeeze, Stochastic, ATR, RSI/price divergence, fractal
support/resistance, and Fibonacci retracement — not just the coarser ones
the regular watchlist covers. (VWAP is shown only where it means something:
it anchors to the current UTC day, so on a 12h frame it would cover at most
two candles, and on 4h at most six. Rather than print a VWAP-shaped number
that isn't one, those two rows show a dash.)

**The short-term read.** On top of the raw numbers, Spotlight groups the 9
timeframes into three horizons and reads them against each other:

- **Entry (1m-10m)** — the immediate momentum you'd actually be entering or
  exiting on.
- **Session (15m-1h)** — the intraday trend a scalp should generally
  respect.
- **Macro (4h-12h)** — the bigger picture; fighting this without a real
  reason is usually how a "quick trade" turns into a bad one.

The headline callout at the top of the panel is plain English, built from
comparing those three: full alignment across all three reads as the
cleanest kind of setup; entry+session agreeing while macro disagrees reads
as a counter-trend bounce, not a reversal, and is called out as such; entry
disagreeing with session reads as chop with no clean edge. **An RSI extreme
or RSI/price divergence on any entry or session timeframe always overrides
whatever the trend says** and forces a "two-sided / caution" read instead —
the same "uncertainty wins" rule the rest of the desk's heat score already
follows (see the project's build notes on this if you're curious why).
Below the headline: entry/session/macro/overall confluence as its own card
each, the leverage-and-positioning panel, a cost check on the setup, key
levels, the full 9-row indicator table, and a divergence note for any
timeframe that has one.

**Nearest and strongest levels are shown separately**, because they answer
different questions — the nearest level is what price has to get through
next, the strongest (most-tested) is where it's most likely to actually
stop. Earlier versions sorted levels by touch count and then labelled the
first one "nearest", which could put a level 6% away under a "nearest
support" heading — actively misleading if you're using it to place a stop.
Both are now shown, with the timeframe and touch count on each. There's
also an ATR reading to gauge how tight a stop is realistic right now.

**This is technical analysis stated in plain English, not a prediction.**
Every sentence in the headline is traceable back to a specific number
elsewhere on the panel — there's no hidden model or black-box score behind
it. Markets move against clean-looking setups constantly, and low-timeframe
leveraged trading can lose money fast; treat this as a fast, organized way
to read the board, not as something to blindly execute on.

Spotlight is one symbol at a time (switching clears the previous one's
data, though the previous symbol keeps getting ordinary live price ticks if
it's still in your pinned watchlist) and isn't persisted across restarts —
pick it again after restarting the app. Refresh cadence is
`SPOTLIGHT_REFRESH_SECONDS` in `.env` (default 180s / 3 min).

## Tracker Positions (fictional, paper-traded)

Below the watchlist there's a **Tracker Positions** panel for testing your
own calls without risking anything. Click **Long** or **Short** on any row
in the watchlist tables, or type a symbol into the box at the top of the
panel and pick a side — the app opens a fictional position at that symbol's
current live price and starts tracking it in real time until you hit
**Close**.

- **Nothing here ever touches a real exchange.** No order is placed, no API
  key is used, no account is involved — it's arithmetic against the same
  live price feed the rest of the app already reads.
- The entry and exit price are always the app's own live price at the
  moment you click, never something typed in — so the P&L you see is honest
  relative to what the market actually did.
- You can open a position on a symbol that isn't in your watchlist (e.g. a
  pair you're just curious about); the app fetches its current price and
  quietly starts tracking it in the background for as long as the position
  stays open.
- **Both gross and net P&L are shown.** Net subtracts the round-trip cost
  the position was opened under, so a +0.10% "win" correctly reads as a
  −0.05% loss after a 0.15% round trip. The cost assumption is frozen at
  open time, so retuning `TAKER_FEE_PCT` later never silently rewrites your
  existing track record.
- **Each position records what the desk was saying when you opened it**,
  shown in the "Signal at entry" column and aggregated into the Signal
  Scorecard above. This is what makes the tracker a forward test rather
  than a diary.
- P&L is percentage-based (`(exit − entry) / entry`, flipped for shorts) —
  it's not tied to a dollar notional size or position size, since there's no
  real capital behind it.
- Open positions and their live P&L, and a history of closed ones with a
  running win rate / average / best / worst, persist to `positions.json`
  next to `main.py` — so your track record survives closing the app and
  reopening it later. That file is gitignored; delete it any time to reset
  your history.

## If the Bybit status chip won't go green

The chip in the top right now always shows a real, specific status instead
of sitting blank: `seeding history` → `connecting` → `live`, or if something
goes wrong, `reconnecting (last error: ...)` with the actual reason (a
firewall/antivirus block, a network drop, etc.) — it keeps retrying every 5
seconds on its own. If you ever see `crashed: ...`, that's the one case that
needs a restart (`Ctrl+C` then `python main.py` again); everything else
self-heals without you doing anything. If it's stuck on `reconnecting` for a
long time, the message after "last error" tells you why — most commonly a
firewall or antivirus blocking outbound WebSocket connections, which you'd
need to allow for `python.exe`/`main.py`.

## Notes and honest limitations

- **No live order-book depth or trade tape.** The feed is last-price/ticker
  level (pushed roughly every 50ms) plus live klines and liquidations, but
  not full Level 2 depth or individual trades. Bybit publishes both for free
  (`orderbook.{depth}.{symbol}` and `publicTrade.{symbol}`), and book
  imbalance / cumulative volume delta would be the natural next orthogonal
  signal to add to `feeds/bybit_feed.py`.
- **Volume isn't part of the confluence vote.** The timeframe bias is built
  from RSI, MACD, SMA position and %B — all price-derived. A breakout on
  expanding volume and one on fading volume score identically here, which
  they shouldn't.
- **Stocks, commodities, futures, and options are intentionally not
  included** — by request, this build is crypto-only via Bybit. The original
  Quant Desk artifact (the Cowork-hosted dashboard) still covers all four
  asset classes on its own hourly/4-hour refresh schedule using free scraped
  sources; this local app is the "genuinely live" companion for crypto
  specifically.
- **RSI/50-day/200-day are seeded from daily closes** (via Bybit's kline
  history endpoint) and then updated live by treating the current price as
  an evolving "today" bar — the same methodology the static dashboard used,
  just computed from real numbers instead of scraped ones now.
- This app **cannot place trades** — it only ever reads market data. It is
  an analysis tool by design and there is no code path in it that could
  submit an order.
- **The Signal Track Record carries selection bias that no amount of maths
  fixes.** It's pooled across your ~20-pair deep watchlist's own daily-bar
  history (up to ~260 days per pair), and that watchlist is populated with
  pairs chosen *because they already moved today* — so it is backtesting
  signals on pre-selected winners, which flatters the results. The base-rate
  comparison and cost adjustment make this visible rather than hidden, but
  they don't remove it. **The Signal Scorecard is the trustworthy one**: it
  records the call before the outcome exists, so it has no such bias. Prefer
  it once it has enough closed positions to mean anything.
- **The indicators are less independent than the "X/9 timeframes agree"
  count makes them look.** RSI, MACD, %B and price-vs-SMA are four
  transformations of the same price series, and 1m/3m of the same asset are
  near-identical. High agreement is partly autocorrelation, not four
  independent confirmations. This is precisely why the positioning data
  matters: funding and open interest are the only inputs here that aren't
  derived from the price line.
- **Positioning is a slow, contrarian-flavoured read, not a timing signal.**
  Extreme funding says a trade is crowded, not that it's about to unwind.
  Crowded can stay crowded for weeks.
- **The cost model is an estimate.** `TAKER_FEE_PCT` defaults to Bybit's
  standard perp taker fee, but your actual fee depends on your VIP tier and
  whether you're maker or taker, and `SLIPPAGE_PCT` is a guess you should
  tune per-pair — thin alts fill far worse than BTC. Funding paid while a
  position is held is *not* modelled at all, which matters for anything
  held more than a few hours.
- **Discovered pairs rotate.** "Also Watching Today" is re-picked every
  `UNIVERSE_RESCAN_SECONDS` from whatever's moving most in the wider market
  right now — it's meant to surface things you didn't think to look for,
  not to be a stable list you track over weeks. Pin anything you want to
  keep watching long-term into `CRYPTO_SYMBOLS` instead.
