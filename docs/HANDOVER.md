# HANDOVER.md — session state, environment, and everything learned the hard way

Written 2026-08-23 to survive a context reset. This captures what lived only in the
conversation: environment facts, bugs found and why they mattered, decisions and who made
them, and the questions still open.

**Reading order for a new session:** `ANALYSIS_BRIEF.md` → this file → `PROGRESS.md`.
Then `INDICATORS.md` / `SIGNAL_MAP.md` / `OPEN_QUESTIONS.md` as reference.

---

## 1. Where things stand

| | |
|---|---|
| Phases | 0–3 complete, 4 partly (metrics + report + sweep done; feature mining not) |
| Tests | **248 passed, 1 skipped** |
| Signals | **87** implemented (`SIGNAL_MAP.md` also lists ~57 documented-but-not-built) |
| Indicator parity | ✅ **confirmed against the live app** by the user, UT inputs 2.0/10 |
| Data | XAUUSDT + BTCUSDT + ETHUSDT, same 166 days, zero gaps each |
| Result | **No edge found in any variant tested.** See §6. |
| PAXGUSDT | ✅ synced by a parallel session — 4.4 years. See §7 and `docs/ENTRY_RULES.md` |
| Real fills | 360 imported from the XM MT5 report — **+3,574.62 net on gold**, 348 trades |
| Live | `watch.bat` — chart updates every 5 s, backtest re-runs every 300 s, no reloads |

### Not built yet

- **PHASE 5 FastAPI server** — `api/` is an empty package. `watch.py` covers the live use
  case for now (localhost only, no auth).
- **Feature mining** — `by_feature`, `by_time`, `compare(run_a, run_b)`, in-sample/
  out-of-sample split. Deliberately deferred: the sample is too small to mine honestly.
- **Zone signals** (`zone_*`) — need per-bar rebuild from a trailing 500-bar window across
  M30/H1/H4. Costly; not wired into the engine loop.
- **Decision chain** (`setup_*`, `avoid`, `wait_*`, `entry_risk_*`) — `ConfluenceEngine` and
  `DecisionEngine` are **not ported**. Substantial work, and they depend on news data.
- **`trailing.mode: atr_mult` / `ut_level`** — accepted by the config schema, **not
  implemented**. The engine now raises loudly rather than silently ignoring them.

---

## 2. Environment (things that will bite a fresh session)

| Fact | Detail |
|---|---|
| Python | 3.12.10, installed via `winget --scope user` at `%LOCALAPPDATA%\Programs\Python\Python312` |
| venv | `C:\inetpub\Claude\ITSupport\backtest\.venv` |
| Permissions | `C:\inetpub` is Administrators-only by default. The user ran `icacls "C:\inetpub\Claude\ITSupport" /grant "LAPTOP-E1CN03PN\User:(OI)(CI)M" /T`. Without that, **nothing can be written**. |
| Console encoding | Windows console is cp1252 and **crashes on `→`, `∞`, box-drawing**. Always run CLI with `PYTHONIOENCODING=utf-8`; `cli.py` also calls `sys.stdout.reconfigure`. |
| winget | The `msstore` source fails with a cert error; use `--source winget`. |
| Ports | `watch` serves on 8787 by default. |

### Shell gotchas that cost time

- `sed -i '1i import sys; sys.path.insert(0, r"C:\\...")'` **eats backslashes**. Write
  Python helper files instead of patching with `sed`.
- Heredocs mangle `\n` inside nested Python string literals — it landed as a real newline
  and produced a `SyntaxError`. Prefer `Write` for anything with escapes.
- Running a script from `/tmp` breaks `import backtest` (Python adds the *script's* dir to
  `sys.path`, not the cwd). Put tools inside the package.
- `.bat` files need **CRLF**; write then convert.

---

## 3. ⚠️ The app source changed mid-session

Between PHASE 0 and PHASE 3, `app/src` gained files that did not exist when the
archaeology was done — **not by us; we only ever read it**:

`core/Instrument.kt`, `core/PriceFormat.kt`, `data/InstrumentRouter.kt`,
`data/remote/BinanceMarketDataSource.kt`

The app now supports GOLD / BTC / ETH via an `Instrument` enum.

**What this costs, checked file by file:**

- ✅ **All 10 indicator files unchanged** (`UtBot.kt`, `Ema.kt`, `Atr.kt`, `MathSeries.kt`,
  …) — **the port and the confirmed parity still hold.**
- ✅ `AlertEngine.kt` unchanged; all 9 alert kinds intact, so `SIGNAL_MAP.md`'s conditions
  are still accurate.
- ⚠️ Only two files under `analysis/` changed: `alerts/Alert.kt`, `news/NewsRiskEngine.kt`.
- ⛔ **Stale citations:** the data-layer sections of `INDICATORS.md`, and Q1/Q31 in
  `OPEN_QUESTIONS.md`. `BybitApi.SYMBOL_GOLD` no longer exists — it is now
  `Instrument.GOLD.bybitSymbol`, still `"XAUUSDT"`.

Notable: `Instrument.GOLD.binanceSymbol = "PAXGUSDT"` — the app itself reaches for PAX Gold
as its gold fallback, independently supporting the Q0 recommendation.

**If the app changes again, re-check the indicator file mtimes before trusting parity.**

---

## 4. Decisions taken, and by whom

| Decision | Value | Source |
|---|---|---|
| Symbol | `XAUUSDT` (not `XAUTUSDT`) | Recommended, user said "follow ur result". Verified live: `baseCoin XAU`, `LinearPerpetual`, tick 0.01 |
| UT inputs | **2.0 / 10** | **User confirmed** from the app's gear icon |
| History | full history + 500-bar burn-in | Recommended, accepted |
| Zones | **always** windowed to 500 bars | Correction found while building the chart — see §5 |
| News | `news_available = false` | Recommended, accepted (no historical calendar exists) |
| Session filter | real gold session, not naive Mon–Fri | Measured, user accepted |
| Exit mode | both fixed-$ and ATR built | User did not pick; both shipped so they can be compared |
| Trail arming | `arm_on_recross: true` | Found necessary — see §5 |

---

## 5. Bugs and traps found — the most valuable section

Each of these would have produced a **plausible but wrong** answer.

### 5.0 The live feed skipped closed bars, leaving a hole in the indicator input

The fast (5 s) loop folds the in-progress bar from 1-minute data so the chart moves between
syncs. The first version appended **only** the forming bar onto whatever the DB held. But
the slow loop syncs every 300 s, so by the time a tick ran, one or more *real* bars had
usually closed and not been stored — observed live: the 1H series ended at 02:00, the fold
appended 04:00, and **03:00 simply did not exist** in the series the indicators were then
computed over. EMA, ATR and the path-dependent UT trailing stop were all computed as if
that bar never traded.

It was invisible on screen because the browser's gap guard refused to plot a bar past the
end of its own array — so the symptom was "the live bar never appears", not "the numbers
are wrong". A shorter sync interval would have hidden the guard and started plotting
silently corrupted values.

Fix: `watch.fold_from_minute` produces the **entire contiguous run** from the last stored
bar to the bucket in progress, or nothing at all if the 1-minute window cannot reach back
far enough. Pinned by `tests/test_live.py`, including a check that a folded bar matches
what Bybit itself reports for that same higher-timeframe bar.

### 5.0b Cross-symbol comparison is meaningless without normalising size

`$25 stop, qty 1.0` is 1.7 ATR on gold and **0.06 ATR on BTC** — roughly the spread. Run
as-is, BTC stops out on essentially every trade and looks catastrophic for a reason that
has nothing to do with the strategy. `engine/symbols.py` scales `qty` by the ATR ratio so
every symbol risks the same dollars per trade, pins all symbols to one calendar window
(BTC has 6 years of history, gold has 166 days), and takes `tickSize` from the instrument
rather than inheriting gold's 0.01. Every adjustment is printed and written into the stored
config, because a silent rescale is exactly the thing that makes a backtest lie.

### 5.0c Two watchers, one output directory — a bug that was not in the code

Symptom: the served page threw `Cannot read properties of undefined` on
`BOOK.runs[BOOK.active]`, and `index.html` on disk had an **older payload format** than the
code that was running. Both looked like a serialisation bug.

Cause: a `watch.bat` from earlier in the day was still running on port 8787, writing the
same `reports/` directory every 300 s. It had imported `analysis/report.py` before that
module gained `build_book`, so Python's module cache kept it emitting the old single-run
shape — and it overwrote whatever the new process had just written. Two servers, one
directory, alternating output.

Second-order effect: `os.replace` onto `index.html` was raising `PermissionError`, leaving
orphan `.tmp` files. **On Windows a rename onto an open file fails**, and the other
watcher's HTTP server holds `index.html` open for as long as it takes to stream 8 MB.

Both fixed:

* `watch._DirLock` — one watcher per report directory, enforced by a `.watcher.lock`
  carrying the pid. A second one exits with a message naming the pid and port to close.
  A stale lock from a killed process is reclaimed.
* `analysis.report.atomic_write` — retries the rename for ~5 s, names the temp file with
  the pid so two processes cannot corrupt each other's, and falls back to an in-place write
  with a warning rather than leaving the page stale.

**The general lesson:** before debugging output that does not match the code, check that
only one process is producing it. `Get-CimInstance Win32_Process -Filter "Name='python.exe'"`
shows the command line.

### 5.0d MT5 exports have no timezone, and a wrong guess is invisible

A MetaTrader 5 trade-history `.xlsx` records the **broker server's** wall clock and states
the zone nowhere in the file. XM Global runs EET/EEST, so the answer here is UTC+3 in
summer — but assuming that and being wrong would put every fill one to three hours from
where it happened, and the chart would look completely normal.

So `data/mt5.detect_offset` measures it: score every whole-hour offset by the median
distance between each fill price and the 1-minute candle it would land on, and take the
winner only if it beats the runner-up by 2x. This account: **UTC+3, median error 3.38 vs
8.91 next best**, 360 fills. Confirmed downstream — 87% of fills then land inside the
high-low of the 1h bar they were taken on, none more than 15 away. At +2 or +4 that
collapses.

⚠️ One offset is applied to the whole file. A report spanning the October EEST->EET switch
would need splitting; nothing currently detects that.

### 5.0e Two contracts, not one: never adjust a real fill to fit a chart

XM's `GOLD` is spot XAU/USD, Bybit's `XAUUSDT` is a perpetual. After the clock is right
there is still a **3.38 gap (−0.076%, IQR 2.2–4.7)** — spot trades under the perp. That is
why only 16% of fills sit inside their *1-minute* candle while 87% sit inside their *1-hour*
one: a gold minute bar is often thinner than the basis.

The tempting fix is to shift the fills by the median basis so the markers line up. That
would falsify the one thing in the whole report that actually happened. Fills are drawn at
the price paid and the basis is measured and printed instead, so a marker under a wick has
an explanation.

### 5.0f A long-running watcher serves new HTML with old data

After adding the zone card and the broker-trade overlay, the page showed the new rail lane
(from the template) but neither the zones nor the trades (from the payload), with no error
anywhere. It looked like a failed import; the import was fine.

Cause: **Python caches modules at import, the HTML template is re-read on every render.** A
watcher started at 12:39 kept calling the 12:39 `analysis/report.py` — which had no
`_zone_block` and no `_broker_trades` — while happily picking up every template edit. Half
a feature, silently.

Two fixes:

* `watch._stale_sources` fingerprints every `.py` in the package at startup and re-stats
  each cycle. Anything newer is logged as a warning and published in `status.json` as
  `stale_sources`, which the page renders as a banner naming the files.
* The `except Exception` around `_broker_trades` and `_zone_block` was too broad. It was
  written for "table absent on an old database" but swallowed a transient failure on the
  first cycle too, publishing an empty section rather than complaining. Both now re-raise
  anything that is not a missing table.

**Rule of thumb for this project:** any change to a `.py` file needs `watch.bat` restarted.
Template-only changes do not.

### 5.0g A validation harness has to be validated before it is believed

`tools/validate_rule.py` was written to re-measure the ENTRY_RULES trigger on the broker's
bars (`AUTOMATION.md` gate §5.1). Run first against the Bybit panel the study used, it
returned **53.0% on n=149** where the study says **67.0% on n=112**.

Had that been run only on the new feed, the honest-looking conclusion would have been "the
edge died when we changed feed" — and the automation build would have been abandoned for a
reason that was entirely a bug in the measuring instrument.

Two causes, both mine:

* **The `gold_session` filter was missing.** §4 says "Session: gold_session, unchanged" in
  one line and it is easy to skim. Without it, weekend bars on a 24/7 perpetual enter the
  sample — thin off-hours flow that is not the market the rule was measured on. Worth 14
  points of win rate.
* **Timeouts were priced at the bar extreme against the position** rather than its close,
  charging losses that never happened.

With both fixed it reproduces **n=112, 67.0%, long 70.5 / short 64.7** — every figure in
§4.3 and §6 to the decimal. `tests/test_mt5_rates.py` now pins that reproduction, so anyone
editing a leg, the walk-forward or the sequencing breaks it loudly.

**The general rule: run a new measurement tool against a known answer before pointing it at
an unknown one.**

### 5.0h A leg whose timeframe has no data kills the panel silently

The rule's leg 5 reads the 5m UT bias. MT5 serves only ~17 months of M5 against 4.2 years of
M15. An aligned HTF column with no data cannot equal `BIAS_BULLISH`, so **every signal before
the 5m history starts simply never fires** — and the run reports a shorter panel with no
error anywhere. The first MT5 measurement silently covered 3 months instead of 4.2 years.

Leg 5 is documented as optional (§4.3), and dropping it buys 2.8 more years. `validate_rule`
now warns by name: `panel truncated to 2025-03-25 by 5m (starts 2025-03-25)`.

### 5.0i A wrong clock hides in the median and shows in the dispersion

Cross-checking `MT5:GOLD` against Bybit `XAUUSDT` at matching UTC timestamps, the **median**
price difference is ≈ −3.4 whether the feed is aligned correctly, shifted an hour, or shifted
two hours. The median cannot detect a clock error at all.

The **scatter** can. Median absolute deviation: **1.34 aligned, 5.9 at ±1h, 10.5 at ±2h.**
The test asserts MAD < 3.0, and a second test shifts the feed by ±1h and asserts the first
one would have failed — a check whose sensitivity is itself checked.

### 5.0j A spread read from a closed market is not a spread

See `AUTOMATION.md` §1. The figure the whole automation case rested on was taken from a tick
**35.5 hours stale** because gold was shut for the weekend. It happened to be pessimistic,
but that was luck. `symbol_info_tick` returns the last tick, not the current one, and says
nothing about its age — always check `tick.time` against the clock before using a quote.

### 5.1 Two self-contradictory strategy conditions

Both were user-proposed, both sound reasonable, both are structurally impossible:

1. **"Only buy when price is above EMA 7, 14 and 28"** combined with a pullback entry —
   fires **once in 166 days**. A pullback deep enough to reach the UT line is essentially
   never still above the fast EMA. Fix: use `ema_stack_bullish` (the 7>14>28 *ordering*),
   which gives 71 signals at the same distance.
2. **"Ride until 5m closes below EMA14"** — held trades **1.7 bars**; 169 of 171 exits were
   the trail. Same shape: a pullback entry *starts* below the fast EMA, so the exit is
   already true at entry. Fix: `arm_on_recross` — the trail stays dormant until the LTF
   first closes back on the favourable side. Win rate 16.4% → 25.7%.

**Pattern to watch for:** any filter that could be true at the instant the entry fires.

### 5.2 Zones are window-dependent, not just warm-up sensitive

Assumed all indicators converge with more history. **Recursive ones do; the swing chain
accumulates.** Full history vs 500-bar window: **1,633 vs 104** accepted swings on 30m.
Because `SupportResistanceEngine.cluster` merges *transitively*, 9× the levels collapse
into enormous bands. First chart replica had a "support zone" 300 points tall.
Fixed via `IndicatorConfig.app_window_bars = 500`, applied to zone construction always.

### 5.3 Numerical port bugs (found by adversarial audit, with a live interpreter)

- **`sma` cumsum-differencing** — I used it and claimed better numerics. **Backwards.**
  `cumsum` grows to `n × price` so subtraction cancels significant bits; measured ~250×
  less accurate, and it **flipped an `sma(10) > sma(30)` comparison** on tick-rounded data.
  That is a trade appearing or vanishing. Replaced with the Kotlin's literal running sum.
- **`rma` multiplied by `1/period`** instead of dividing — differs on ~36% of steps.
- **`rma` seeded with `np.sum`** (pairwise) instead of a sequential loop — 1 ULP off on
  ~19% of period-14 windows, and the seed is a recursive filter's initial condition.
- **`percentile_rank` stripped NaN** from the denominator; Kotlin counts them.
- **`to_tick_volume` propagated NaN**; Kotlin's `Double.toLong()` maps NaN to 0.

All now pinned by **bit-identical tests against literal Kotlin transcriptions** over 20,000
bars at periods 14/20/200.

### 5.4 Tests that passed vacuously

The synthetic fixture used a constant drift, running price 2,000 → 25,000 over 40 days.
The UT stop never flipped back, so **zero trades were generated and every engine test
passed on an empty list.** Only caught because a test asserted the fixture produced trades.
**Always assert your fixture actually exercises the thing.**

### 5.5 Front-end bugs (caught by pixel-probing, not by looking)

- **Chart grew to 425,500 px tall** — flex/canvas feedback loop; the canvas sized to 100%
  of a `flex:1` parent with no `min-height:0`. Fixed with `min-height:0` + absolute canvas.
- **Canvas backing store stuck at 1×1** — `resize()` ran before layout settled. Fixed with
  a `ResizeObserver` so it self-corrects.
- **VWAP painted 6 pixels** — `C.vwap` was never defined in the report's palette, so
  `strokeStyle` silently inherited the previous colour and VWAP drew *on top of* EMA28 in
  orange. Invisible. Found by toggling it off and counting pixels — turning it off made
  teal go **up**, because the teal being counted was the 15m rail label.

### 5.6 Docs drifting from code

`SIGNAL_MAP.md` fell **26 signals behind** the engine and named ~57 that were never built,
including ones I had recommended myself. A new session would have written a config that
raised `KeyError`. Fixed, and `tests/test_signal_docs.py` now fails on drift — it also
**evaluates all 87 signals** to catch typo'd column references.

### 5.7 Silent config acceptance

`trailing.mode: ut_level` was accepted by the schema and silently ignored by the engine —
a run would have reported results for a strategy nobody tested. Now raises.

---

## 6. Results — nothing has an edge

All: full history, gold session, fees + slippage + funding on.

### On XAUUSDT

| config | n | win% | net | exposure |
|---|---|---|---|---|
| `ut_1h_long_v1` ($25/$25) | 99 | 51.5% | −347.76 | 39.5% |
| `ut_1h_long_atr` (1.75×ATR) | 95 | 50.5% | −361.02 | 24.8% |
| `ut_mtf_ride_long` (best of 12 sweep) | 169 | 26.6% | −374 | 8.8% |
| `ut_mtf_ride_short` | 190 | 27.9% | −882.38 | 6.4% |
| **buy & hold 1 unit** | — | — | **−524.90** | 100% |

**Gold FELL 10.1%** over the window (5127 → 4608). The long variants losing less than
buy-and-hold is **not an edge** — they are flat ~91% of the time, so they did not
participate in the decline.

### Across instruments — the one genuinely new finding

`ut_1h_long_v1`, identical window (2026-03-09 → 2026-08-23), size scaled by the ATR ratio
so every symbol risks the same dollars per trade:

| symbol | n | win% | 95% CI | **gross** | fees | net |
|---|---|---|---|---|---|---|
| XAUUSDT | 99 | 51.5% | 42–61% | **+137.59** | 482.28 | −347.76 |
| BTCUSDT | 97 | 44.3% | 35–54% | **−290.81** | 279.11 | −575.01 |
| ETHUSDT | 90 | 44.4% | 35–55% | **−237.12** | 195.02 | −439.32 |

**Read the gross column, not the net.** On gold the entry is gross-POSITIVE and loses only
to costs. On BTC and ETH it is gross-NEGATIVE — the signal itself has no edge there, so no
amount of cost reduction would save it.

That reframes the whole problem. It was previously "a decent signal eaten by fees"; it is
now "a decent signal on **gold specifically**, eaten by fees". The two instruments where
the fee burden is *lighter* (BTC 279, ETH 195, versus gold's 482, because equal ATR risk
needs less notional on a more volatile instrument) are the two where it performs worse.

⚠️ **The confidence intervals overlap.** 42–61 vs 35–54 does not establish that gold is
better at p<0.05. The gross sign flip is the stronger signal, and it is still one sample.

⚠️ This says nothing about *other* strategies on BTC/ETH. It says this UT-pullback entry,
tuned on gold, does not transfer.

**Two independent gaps (gold):**

| Gap | Current | Breakeven | Lever |
|---|---|---|---|
| Hit rate at 2:1 payoff | 26% | 33% | better entry filtering |
| Hit rate at 1:1 payoff | 51% | ~60% | larger targets |

**Cost floor: $5.06 round trip = 20% of a $25 target.** At a 4×ATR target the same fee is
~9%. The arithmetic points hard at bigger moves; nothing tested has held long enough.

---

## 7. ✅ PAXGUSDT — resolved, and by a parallel session

**It is synced.** 1m / 5m / 15m / 30m / 1h / 4h from **2022-03-15**, 4.4 years,
2.3M 1-minute bars. It tracks XAUUSDT to 0.237% (better than `XAUTUSDT`'s 0.393%) and is
the app's own gold fallback.

This was done by a **different Claude session working in this repo at the same time**, which
also wrote `docs/ENTRY_RULES.md`. That file is that session's decided trigger spec, measured
across PAXG + XAU + BTC + ETH, and it lists what the engine still needs before it can run.
Read it before changing any entry or exit rule.

Two sessions editing one repo is worth knowing about in itself — see §5.0c for the concrete
damage two processes writing one directory caused, and check `docs/` mtimes if a file
disagrees with your memory of it.

Why it mattered: every XAUUSDT run produces 95–190 trades. At n=100 the 95% CI on a win rate
is ±10 points, and ~15 configurations were already tested against that one 166-day sample.
Tuning against XAUUSDT alone is fitting noise. With PAXG the sample question is answerable:
mine on PAXG, confirm out-of-sample on XAUUSDT, and use BTC/ETH (§6) as a transfer check.

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli coverage --symbol PAXGUSDT
```

Note `resolve_symbol` only hard-asserts `baseCoin` for symbols listed in
`EXPECTED_BASE_COIN` (XAUUSDT/BTCUSDT/ETHUSDT). PAXGUSDT resolves through the generic path.

---

## 8. Other open items

- **MACD and sessions are NOT in the app** — backtest-only, outside the confirmed parity.
  Anything built on them is unvalidated against anything real.
- `bb_width_percentile` and `ema_spread_trend` are computed by the app and **read by
  nothing**; ported and exposed as `bb_squeeze` / `bb_expansion`, marked BACKTEST-ADDED.
- The engine enforces the app's own ≥60-bar `usable` gate, which **the app's orchestrator
  forgets** (OPEN_QUESTIONS Q8) — a deliberate divergence.
- Backtest signals are **strictly fewer and later** than the app's live alerts, because the
  app reads the forming bar and a backtest cannot (Q28). Results are a lower bound.
- 12 stale `ut_1h_long_v1` runs are in the DB from repeated testing. Runs are append-only
  by design; filter by `run_id` when analysing.

---

## 9. Commands

Double-click either of these. Defaults are gold + BTC + ETH.

```bash
C:\inetpub\Claude\ITSupport\backtest\watch.bat
```

```bash
C:\inetpub\Claude\ITSupport\backtest\report.bat
```

Positional overrides — `watch.bat [config] [symbols] [rebuild_secs] [port]`,
`report.bat [config] [symbols] [timeframes]`:

```bash
C:\inetpub\Claude\ITSupport\backtest\watch.bat configs\ut_mtf_ride_long.yaml XAUUSDT 120
```

```bash
C:\inetpub\Claude\ITSupport\backtest\chart.bat 30m 90 MYT
```

```bash
backtest\.venv\Scripts\python.exe -m pytest backtest/tests -q
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.tools.sweep backtest/configs/ut_mtf_ride_long.yaml --trail-sweep
```

Every CLI command needs `PYTHONIOENCODING=utf-8` on this console.

---

## 10. If you change one thing next

Not another filter on the existing entry.

1. **Read `docs/ENTRY_RULES.md` first.** A parallel session has already specified a trigger
   against the PAXG sample and listed what the engine is missing to run it (§8 of that
   file). Building that is more valuable than another variant of the current config.
2. **Test 3–5×ATR targets** — the cost arithmetic says the current target cannot clear its
   own fees at the observed hit rate, and no variant so far has held long enough to find out.
3. **Check anything you propose on BTC and ETH.** They are synced over the same window and
   one flag away (`--symbols`). A change that helps gold and leaves BTC/ETH gross-negative
   is fitted to 99 trades. It is the cheapest falsification available.
