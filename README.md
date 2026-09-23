# XAU/USDT Indicator Backtesting Engine

Local-first backtesting for gold, driven by the **exact indicator logic already in this
repo's Android app** (`../app`). Nothing here is invented — every formula is a cited port,
and `docs/INDICATORS.md` carries the `file:line` for each one.

The app source is **read-only**. Nothing under `../app` has been modified.

**Status:** PHASE 0–3 complete, PHASE 4 partly. **`watch.bat` now runs in LIVE mode —
no backtest in the loop; the rule is evaluated on each closed bar and gated by a live risk
read (`docs/LIVE_MODE.md`).** Indicator parity **confirmed against the
live app**. Live watch mode updates the chart every 5 s without reloading, and the same
strategy can be run against gold, BTC and ETH side by side. Still to build: feature mining
(`by_feature` / `by_time`), in-sample/out-of-sample split, and the FastAPI server.

---

## Setup

Python 3.12 is already installed and the venv already exists. To rebuild from scratch:

```bash
python -m venv C:\inetpub\Claude\ITSupport\backtest\.venv
```

```bash
C:\inetpub\Claude\ITSupport\backtest\.venv\Scripts\python.exe -m pip install -r C:\inetpub\Claude\ITSupport\backtest\requirements.txt
```

All commands below assume you run from `C:\inetpub\Claude\ITSupport`.

---

## Confirm the instrument before trusting anything

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli resolve
```

Resolves the gold contract at runtime against `/v5/market/instruments-info` and **refuses
to proceed** unless `baseCoin == "XAU"`. Expect:

```
symbol XAUUSDT · baseCoin XAU · quoteCoin USDT · LinearPerpetual
tickSize 0.01 · qtyStep 0.001 · minOrderQty 0.001
```

`XAUUSDT` is the gold commodity perpetual the app prices. It is **not** `XAUTUSDT`
(Tether Gold), which the app's own source rejects by name — see `docs/OPEN_QUESTIONS.md` Q1.

---

## Sync market data

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli sync --symbol XAUUSDT --tf 1m,5m,15m,30m,1h,4h --from 2025-01-01 --to now --funding
```

Idempotent, resumable and gap-aware. If the requested start predates the contract's
listing it says so rather than returning a silently empty table.

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli coverage --symbol XAUUSDT --show-gaps
```

⚠️ **`XAUUSDT` was listed 2026-03-09 — only ~166 days exist.** For feature mining use
`PAXGUSDT`, which is synced in full (1m–4h from 2022-03-15, 4.4 years) and tracks XAUUSDT
to 0.237%. See `docs/OPEN_QUESTIONS.md` **Q0**.

`BTCUSDT` and `ETHUSDT` are synced over the same window so the strategy can be compared
across instruments. Any symbol Bybit lists under `linear` works; `XAUUSDT`, `BTCUSDT` and
`ETHUSDT` additionally have their `baseCoin` asserted at resolution time
(`data/bybit_client.py`, `EXPECTED_BASE_COIN`) so a re-listing cannot silently swap the
instrument under a saved config.

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli sync --symbol BTCUSDT --tf 1m,5m,15m,30m,1h,4h --from 2026-03-09 --to now --funding
```

---

## Compute indicators

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli indicators --symbol XAUUSDT --tf 30m --tail 10
```

---

## See the chart (cross-check against the app)

**Double-click `backtest\chart.bat`.** It syncs the latest candles, renders a 30m chart in
MYT, prints the legend values, and opens the image.

From a terminal you can pass timeframe, bar count and timezone:

```bash
C:\inetpub\Claude\ITSupport\backtest\chart.bat 1h
```

```bash
C:\inetpub\Claude\ITSupport\backtest\chart.bat 15m 150 UTC
```

Or call the renderer directly, e.g. to reproduce a past window:

```bash
backtest\.venv\Scripts\python.exe -m backtest.tools.plot_chart --tf 30m --tz MYT --end 2026-08-21T12:00:00Z --out mychart.png
```

`--bollinger` and `--vwap` add those overlays; both are **off by default because the app
turns them off by default** (`ui/MarketViewModel.kt:70`).

### How to compare it with your phone

The chart uses the app's own palette (`ui/theme/Color.kt:23-46`) and its 90-bar initial
window (`ui/chart/CandleChart.kt:413`), so the two should look alike. The reliable check is
numeric, not visual:

1. Open the app's **Chart** page on the same timeframe.
2. Compare the **legend card** at the bottom — UT Dynamic Level, EMA 7/14/28, Bollinger
   upper/lower, VWAP — against the legend this tool prints.
3. Or **long-press the chart** to read a single bar's price and UT level, then find the same
   MYT timestamp in the printed table.

⚠️ **The UT level only matches if the gear-icon inputs match.** The port defaults to
**key value 2.0, ATR period 10** — confirmed as the user's setting. `2` vs `3` moves the
trailing level by 50%, so this is the first thing to check if the gold line disagrees.

⚠️ Small differences in the **last 1–2 bars** are expected and not a bug: the app draws the
forming (LIVE) bar, the backtest only draws closed (CONFIRMED) bars.

---

## Run a backtest and inspect it visually

**Double-click `backtest\report.bat`.** It runs the strategy on gold, BTC and ETH, builds
one self-contained interactive HTML report with a symbol switcher, and opens it.

```bash
C:\inetpub\Claude\ITSupport\backtest\report.bat backtest\configs\ut_mtf_ride_long.yaml XAUUSDT
```

Arguments are `config`, `symbols`, `timeframes`.

The report gives you a timeframe switcher (5m/15m/30m/1h/4h that holds the same
time window when you switch), entry markers coloured by win/loss with the exit
marked and joined, a per-signal filter, and a clickable trade list. Arrow keys
step through trades. Everything is embedded, so the file works offline.

The **trades panel** lists newest first, filters to winners or losers only, and
switches between the run's trades and your own imported fills. In **live mode** the
backtest's trades are replaced by **the signals the rule actually emitted**, split into
sent and vetoed — real decisions at a real time, and no invented P/L: an open signal has no
exit until the broker closes it. The outcome
filter drives the chart overlay too, so "show me only the losers" means the same
thing in both places — which is the fastest way to see what a set of bad entries
had in common.

The **rail** under the price panel has one lane per timeframe showing that
timeframe's own UT flips, plus a `signal` lane (the strategy's entries solid, exits
hollow) and a `mine` lane (your fills). Agreement and disagreement between the
three are then one glance apart rather than something to reconstruct by eye.

### Live-connect to MetaTrader 5 (no export, no password)

If the MT5 terminal is open and logged in, the history can be read straight out of
it — nothing to export, nothing to import:

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli sync-mt5
```

```
XM Global MT5 build 6140
  account 335445171 @ XMGlobal-MT5 9  USD 209.87 (equity 229.16)
  server clock UTC+3 measured from ETHUSD (5s old)
  370 positions written
```

**`watch.bat` now does this at the start of every cycle**, so your fills appear on
the chart by themselves. Pass `--no-mt5` to turn it off. An incremental sync takes
**0.03 s** and re-reads the last 7 days, so a position opened last week and closed
today still picks up its exit.

**No password is ever involved.** `MetaTrader5.initialize()` with no arguments
attaches to the terminal *already running and already logged in* on this machine.
The package does have an `initialize(login=…, password=…, server=…)` form; it is
deliberately not used, and `tests/test_mt5_live.py` parses the module and fails if
`initialize` is ever given an argument or if any identifier mentions a password.
**Don't send me your credentials — the tool cannot use them and does not want them.**

**Read-only by construction.** The only terminal calls in the module are
`initialize`, `terminal_info`, `account_info`, `symbol_select`, `symbol_info_tick`,
`history_deals_get`, `positions_get`, `shutdown`. A test fails if `order_send` or
`order_check` ever appears there.

**The clock is measured, not assumed — again.** MT5 returns timestamps on the
broker's clock while typing them as UTC. A live terminal can simply be asked: the
last tick of a symbol whose market is open right now *is* the server's current
time. The freshest quote across several symbols is used, because a closed market's
tick can only be older — on a weekend, gold's last tick is Friday's and would imply
a nonsense offset. This reports **UTC+3**, the same answer the .xlsx price-matching
heuristic reaches independently, and a test asserts the two agree.

MT5 stores *deals*, not positions, so entry and exit deals sharing a `position_id`
are recombined and their commission, swap and fee summed — the position's true P/L
is on no single deal. A test checks the rebuilt positions match the .xlsx export
field for field on every position both contain.

### Import an MT5 report file instead

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli import-mt5 "C:\Users\User\XM\2026\August\MT5ReportHistory-335445171.xlsx"
```

Use this when the terminal is not on this machine, or for an account you are no
longer logged into. Reads a MetaTrader 5 **Trade History Report** `.xlsx`, keeps the positions whose
symbol maps to a synced contract (`GOLD`/`GOLD24-7`/`XAUUSD` -> XAUUSDT,
`BTCUSD` -> BTCUSDT, `ETHUSD` -> ETHUSDT) and skips the rest rather than guessing.
Your fills then appear on every chart as **hollow chevrons** — up for buy, down for
sell, outlined green if that position made money — with a small circle where it
closed, plus a `mine` lane on the rail and a **Mine** tab in the trades panel.

⚠️ **MT5 exports carry the broker server's wall clock with no timezone marker
anywhere in the file.** Guess it wrong and every fill lands on the wrong bar while
still looking entirely plausible. So it is **measured**, not assumed: every
whole-hour offset is scored by how far each fill price sits from the 1-minute
candle it would land on, and the winner is reported with its margin.

```
broker clock measured as UTC+3 (median price error 3.38, next best 8.91, 360 fills matched)
```

That is a 2.6x margin, and it checks out downstream — **87% of fills land inside
the high-low of the 1h bar they were taken on**, none more than 15 away. Pass
`--tz-offset` to override, and note that a report spanning a DST change would need
splitting, since one offset is applied to the whole file.

⚠️ **Your broker's contract is not the exchange's.** XM's `GOLD` is spot XAU/USD;
Bybit's `XAUUSDT` is a perpetual. Measured across 348 fills, spot sits **3.38 under
the perp (−0.076%)**, tightly. Fills are stored and drawn **at the price you
actually got** — never nudged to fit the candles — so a marker can sit slightly
below the wick, and that is the honest picture rather than a bug.

Useful coincidence: XM gold is 100 oz/lot, so **0.01 lot = 1 oz = the backtest's
default `qty: 1.0`**. Your P/L per trade and the engine's are directly comparable.

⚠️ **If `watch.bat` was already running when you imported, restart it.** Python
caches modules at import, so a watcher left running across a code change keeps
building payloads with the old code — while the HTML template, which is re-read on
every render, picks changes up immediately. The result is a page with half the new
features and nothing anywhere saying why. The watcher now detects this and the page
shows a banner, but the fix is always the same: close the window and start it again.

### Hover a candle to see what the app would have said

Pointing at any bar shows the dashboard's **TECHNICAL STATE** and **KEY LEVELS**
panels as they stood at that bar — the point being to read history the way the app
reads the present.

```
2026-08-22 12:00 MYT
2026-08-22 00:00 EDT · 2026-08-22 04:00 UTC
O 4,615.96   H 4,615.96
L 4,611.63   C 4,613.76

TECHNICAL STATE · 1h
UT Dynamic Level          Bullish
EMA 7/14/28               Bullish
RSI                            62
ADX                            37
VWAP                        Below
BB %B                        0.56
Tick volume                 0.37x
UT level                 4,592.26
ATR 14                      12.84

KEY LEVELS
RESISTANCE     4,617.80 – 4,619.99
               0.3 ATR away · strength 2.2
CURRENT                   4,613.76
SUPPORT        4,607.80 – 4,610.65
               0.2 ATR away · strength 3.5
also R 4,637.02 · S 4,501.08–4,510.95 · S 4,362.03–4,417.91
30m swing high · Previous day close · Session high
```

Two things about that card are worth understanding, because they are not cosmetic.

**TECHNICAL STATE is per-timeframe; KEY LEVELS is not.** The app builds zones from
M30 + H1 + H4 swings plus H1 session levels and splits them into support and
resistance using the **H1 confirmed close**, whatever chart you happen to be looking
at (`MarketAnalysisEngine.kt:141-151`). So the zone card lives on the H1 grid. On a
5m chart it can therefore be up to 59 minutes old, and it says so —
`KEY LEVELS as of 11:00 (30m before)`. It is never *newer* than the bar you are
pointing at.

**Zones never come from the UT level.** The app is explicit about this
(`SupportResistanceEngine.kt:46-47`): the UT Dynamic Level is a trailing stop, not a
price anyone has defended. The sources line under the card tells you what the zone
actually is — a swing, a previous-day extreme, or a session extreme.
`tests/test_live.py` fails if a UT-derived source ever appears.

The `N ATR away` line is measured from **this bar's** close to the nearest zone edge,
using the H1 reference ATR — the same measure the app's alert layer uses to decide
"price is at a zone".

Zone snapshots are cached in the `zone_snapshots` table because they cannot be
derived by indexing into a single pass: the app rebuilds from a trailing 500-bar
buffer and swing chains accumulate, so each snapshot is a genuine recompute
(~3 ms × ~4,000 bars ≈ 12 s per symbol, once). A closed bar's snapshot depends only
on closed bars, so caching it is exact rather than an approximation. In watch mode
they refresh on the slow loop, not the 5-second one — an H1 grid does not need
5-second updates.

### Time zones

The **Time** buttons switch the axis, the trade list and the header between
**MYT** (Malaysia, UTC+8), **NY** (New York) and **UTC**. The header carries a live
clock in both MYT and New York, so you can see at a glance whether the US session is
open.

New York is resolved through the IANA zone `America/New_York`, not a fixed offset — it
is UTC−4 in summer and UTC−5 in winter, and this dataset spans both. The abbreviation
shown (`EDT` / `EST`) tells you which applies to the bar you are looking at.

The **tooltip always shows all three zones**, labelled, regardless of the switch.
Everything is stored as epoch milliseconds UTC and converted only for display, so
switching zones can never change a result.

### Measure a move

Click **Measure** (or hold **Shift**) and drag on the chart. The readout gives the price
change, the percentage, the size **in ATR-14 as of where you started**, the bar count, the
elapsed time, and the dollar value at that run's position size. It stays on screen after you
release so you can read it; Esc or a plain click clears it.

The ATR multiple is the number worth looking at — every threshold in the configs is written
in ATR, and "60 points" means something completely different on gold than on BTC.

### Compare instruments

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli backtest backtest/configs/ut_1h_long_v1.yaml --symbols XAUUSDT,BTCUSDT,ETHUSDT
```

Runs each symbol over the window **all of them cover** and prints a comparison table.

⚠️ **Position size is rescaled automatically, and it has to be.** `$25 stop, qty 1.0` is
1.7×ATR on gold and **0.06×ATR on BTC** — about the spread. Run unchanged, BTC would stop
out on nearly every trade for a reason that has nothing to do with the strategy. `qty` is
scaled by the ATR ratio so every symbol risks the same dollars per trade, and `tickSize` is
taken from the instrument. Every adjustment is printed, and written into the config stored
on the run. Pass `--qty` to force a size, or `--no-normalise` to keep the config's (the
results are then not comparable, and it says so).

What this found: on gold the entry is **gross-positive** and only loses to fees; on BTC and
ETH it is **gross-negative**. See `docs/ANALYSIS_BRIEF.md` §6.1.

Or drive the pieces separately:

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli backtest backtest/configs/ut_mtf_ride_long.yaml
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli runs
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli report --symbols XAUUSDT,BTCUSDT,ETHUSDT --open
```

### Keep it running against live data — LIVE MODE, no backtest

**Double-click `backtest\watch.bat`.** Serves the page at `http://127.0.0.1:8787/` and
keeps it current. Full detail: **`docs/LIVE_MODE.md`**.

**There is no backtest in this loop any more.** It used to re-run the strategy over 166
days every 300 s per symbol to serve a page that only ever shows the newest state. Now
each cycle evaluates the **entry rule on the newest CLOSED bar**, reads the **risk
vectors**, and emits a signal when the rule fires and the risk gate allows it. Pass
`--backtest` to get the old behaviour back.

```bash
C:\inetpub\Claude\ITSupport\backtest\watch.bat backtest\configs\ut_1h_long_v1.yaml XAUUSDT 120
```

Arguments are `config`, `symbols`, `seconds between rule + page rebuilds` (default 300),
`port` (default 8787).

**The rule fires on Bybit gold and its signals are drawn on the gold chart**, in the same
`signal` rail lane as before and directly under the `mine` lane holding your own broker
fills — so the two can be compared at a glance. `MT5:GOLD` was tried as a chart symbol and
dropped: the terminal only serves fresh bars while it is open and logged in with the symbol
in Market Watch, and in normal use it fell **eight hours behind** while every number on the
page stayed plausible. The MT5 connection is still used for your fills, where a few minutes
of lag costs nothing.

⚠️ **The live bracket is a fixed `+25 / -25` in price** (= $25 either way at 0.01 lot), by
the owner's choice, and it is **not** the measured system — `OPERATING_PLAN.md` §8 chose
TP 5.0 / SL 2.5 × ATR14, and §8.2 measured a symmetric bracket as the cell that took a $200
account to −$130 on the real sequence. Every signal carries `bracket_measured: false`.
`bracket_mode="atr"` in `RuleConfig` returns to the measured one.

**Two loops, because they change at completely different speeds:**

| loop | every | does |
|---|---|---|
| live | **5 s** | one 1-minute request per symbol; folds the forming bar for every timeframe; re-reads the **risk vectors** against the new price; patches the newest bars into the page |
| rebuild | 300 s | sync, evaluate the rule, rebuild the ~2 MB payload |

**The rule decides; the risk read can only veto.** The 9-leg entry has a measured edge
(n=588, 42.5% against a 34.2% break-even, **+3.3 points against a matched random-entry
null**). The `RiskExecutiveEngine.kt` port that produces Danger% / Confidence% has
**hand-set weights that have never been measured on gold**, so it gates rather than
triggers — `gate()` returns a verdict carrying no price, side or size, and a test asserts
it cannot open a trade. Defaults are loose (`danger <= 70`, `confidence >= 25`); tune with
`--max-danger` / `--min-confidence` / `--require-bias`, or `--no-gate` to log without
vetoing.

⚠️ **There is no order book anywhere in this project.** The risk matrix's 30%
"Order-Book" factor is fed by this repo's own **support/resistance zones** as a stand-in —
zone distance for wall distance, zone strength for notional. **Open interest IS fetched**
on Bybit symbols and has its own chart pane; long/short ratio is not, and neither exists on
`MT5:*` — an absent term lowers Confidence rather than reading as low danger. The page says
all of this, on the page, every time.

Open interest is also the one series here that **cannot be recomputed**: Bybit serves about
170 days of it and then a past reading is gone for good, so the table is never pruned and
`python -m backtest.cli oi` backfills what the forward-only live sync can never reach. A
full year of all three symbols costs about 3 MB. See `docs/LIVE_MODE.md`.

**Three buttons in the toolbar:**

* **☕ Keep awake** — holds the screen on and stops Windows sleeping. Two locks, because
  the browser drops its wake lock the moment the tab is hidden: the page takes
  `navigator.wakeLock` *and* the watcher process holds `SetThreadExecutionState`. It does
  not override the workstation lock, a lid close, or battery saver.
* **📋 Copy analysis** — ~17 KB of pasteable text carrying three things: **the mandate**
  (the owner's own instruction set in Traditional Chinese — macro-desk persona, the
  engine's scoring rules, session timing, and the required output shape), **the snapshot**
  (ADX with its slope, the engine's volume bands with the participation-divergence flag,
  BB %B, multi-timeframe alignment, funding measured against the venue baseline, open
  interest resolved into its four quadrants, KEY LEVELS, the risk read, the rule's legs and
  the gate's verdict, your open positions, and the measured facts with their sample sizes),
  and **the reading notes**.

  It opens with an explicit **"what is absent"** section — no order book, no economic
  calendar, no long/short ratio — printed *before* the numbers it qualifies, because a
  model told nothing about a gap does not decline; it writes confident prose about
  liquidity and events it cannot see.
* **🔊 Sound** — a chime when a signal fires, alongside a banner.
* **ℹ Details** — the full hover card, **off by default**: it fired on every pixel of
  mouse movement and covered the price you were reading. In its place the chart now has a
  proper **crosshair** that prints the price on the price axis and the time under the time
  axis, and keeps **18% of the window clear to the right of the newest bar** so the live
  edge is never pinned against the axis.

**KEY LEVELS need a one-time cache build per symbol.** The live loop reads that cache and
never writes it, because a snapshot is a genuine recompute (~3 ms × 81,922 H1 bars on
`MT5:GOLD`) and building it inline would stall the page for minutes:

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli zones --symbols XAUUSDT,BTCUSDT,ETHUSDT
```

**The page is never reloaded.** New bars are patched into the arrays already in the browser,
and when a fresh backtest finishes its payload is swapped in place. Your zoom, your selected
trade, your timeframe and any measurement all survive. The pill in the header shows the last
tick and the current price.

The forming bar is drawn **hollow**. It is display-only:

> **Signals are read from the last CLOSED bar, never the forming one** — the same rule the
> whole engine follows. A signal read off an unfinished bar can un-fire before that bar
> closes. UT cross markers, the Live panel and every entry condition come from closed bars.

The Live panel shows the last closed bar, the current price, UT level, ATR/RSI/ADX, and —
most useful — **which entry conditions are true right now and which one is blocking**:

```
entry not met - blocking leg(s) below
  x ut_cross_up
  v ut_bias_bullish
  x ut_level_near
```

**Now** jumps to the live edge and follows it. Panning away stops the following; **Now**
resumes it.

⚠️ **Restart it after any code change.** Python caches modules at import; the
report template does not. A watcher left running across an edit therefore serves a
page where the template is new and the data is old. It now compares the package's
file mtimes against what it loaded and both warns in the console and shows a banner
on the page, but restarting is the only fix.

⚠️ **Only one watcher at a time.** They all write `backtest/reports/`, so two running at
once overwrite each other and the page shows a mix of both. A lock file enforces this — a
second watcher exits and tells you which pid and port to close. If a window was killed
rather than Ctrl+C'd, the stale lock is reclaimed automatically.

---

### Parameter sensitivity

```bash
backtest\.venv\Scripts\python.exe -m backtest.tools.sweep backtest/configs/ut_mtf_ride_long.yaml --trail-sweep
```

Prints **every** combination sorted by net P/L, not just the best one. Read the
column, not the row: a setting that works at one point and fails at both
neighbours is noise. It also prints the confidence interval next to the winner,
because at these sample sizes the ranking itself is not reliable.

---

## Run the tests

```bash
backtest\.venv\Scripts\python.exe -m pytest backtest/tests -q
```

**248 passed, 1 skipped.** The skip is `test_golden_csv_parity`; parity was instead
confirmed directly against the live app, so it is no longer needed.

The ones that matter most:

* **`test_no_lookahead.py`** — truncates the dataset at several cut points and requires
  every earlier value to be bit-identical; separately mutates all *future* bars and
  requires the past not to move. This is the test that catches the class of bug that
  makes a backtest look profitable and a live account not.
* **`test_parity.py`** — self-consistency plus **bit-identical** comparison against
  literal transcriptions of the Kotlin `sma`/`rma` over 20,000 bars.
* **`test_fills.py`** — slippage direction, exit-level resolution, and 1-minute
  ambiguity replay resolving in *both* directions (not just confirming the stop).
* **`test_signal_docs.py`** — fails if the engine and `SIGNAL_MAP.md` drift apart, and
  evaluates every one of the 87 signals to catch typo'd column references.
* **`test_live.py`** — the live feed's two shortcuts. That folding 1-minute bars into a
  higher timeframe reproduces what the exchange itself reports, that it never leaves a hole
  when the DB is behind (it did, once — see `docs/HANDOVER.md` §5.0), and that recomputing
  indicators over a 1,500-bar tail is **bit-identical** to recomputing over all of history.

---

## Layout

```
backtest/
  docs/         INDICATORS.md · SIGNAL_MAP.md · OPEN_QUESTIONS.md   <- read these first
  data/         bybit_client.py · db.py · sync.py · schema.sql · market.db
  indicators/   mathseries · trend (ATR/EMA/UT Bot) · context (RSI/BB/ADX/VWAP/Vol)
                structure (swings/BOS/CHoCH/zones) · regime · registry · base
  tests/        test_parity.py · test_no_lookahead.py
  tools/        export_golden.md
  engine/       backtester · signals · live_signal (the RULE, measured)
                risk_engine (the RiskExecutiveEngine port) · risk_feed · live_engine
  keepawake.py  SetThreadExecutionState, for the Keep-awake button
  cli.py        resolve · sync · coverage · indicators · signals · zones · watch
```

---

## Four things to know before reading the code

**1. CONFIRMED only.** The app runs every indicator twice — over closed bars (CONFIRMED)
and over closed + forming (LIVE) — and reads LIVE for "where is price right now"
questions. A bar-closed backtest cannot use LIVE without look-ahead, so every LIVE input
is replaced by its CONFIRMED equivalent. **Backtest signals are therefore strictly fewer
and later than the app's live alerts.** That is the honest direction to err, but it means
these results are a lower bound on the app's signal count, not a reproduction of it.
(`docs/OPEN_QUESTIONS.md` Q28.)

**2. Structure is folded per bar.** `SwingDetector.kt:93` replaces the last accepted pivot
*in place* when a more extreme one arrives later, so `lastSwingHigh` can change with no
new bar closing. Computing structure once over the whole series and indexing backwards
would be a look-ahead bug. It is folded incrementally with a snapshot at every bar.

**3. Parity vs the live app is CONFIRMED.** Checked by comparing the app's Chart-page
legend against this tool's output on 1H and 30m with UT inputs 2.0/10. The golden-CSV export
in `tools/export_golden.md` is therefore superseded.

**4. Cross-symbol numbers are only comparable because they are normalised.** See
`engine/symbols.py`. Comparing a `$25` stop across instruments without scaling measures tick
size, not strategy.

---

## Disclaimer

An analysis tool. It does not place trades and does not give financial advice.
A backtest is a statement about the past under stated assumptions, not a forecast.
