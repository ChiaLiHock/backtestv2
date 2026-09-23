# ANALYSIS_BRIEF.md — read this before suggesting any change

**Purpose.** This is a handover briefing for a fresh session being asked *"where should
we optimise the entry or the exit?"* It describes what the system is, what every number
means, what has already been tested, and — most importantly — **what has already been
tried and disproved**, so the same suggestions are not made again.

Read `INDICATORS.md` for exact formulas, `SIGNAL_MAP.md` for the full signal catalogue,
and `OPEN_QUESTIONS.md` for the decisions that were made and why.

---

## 1. What this system is

A local backtesting engine for **XAU/USDT gold** that replicates, exactly, the indicator
logic of an existing Android app in this repo (`../app`). Nothing was invented: every
formula is a cited port of Kotlin source, and indicator parity was **confirmed against
the live app** by the user comparing the app's on-screen legend to the port's output.

The point of the project is to answer *"which of the app's setups actually win, and under
what conditions"* — not to build a new indicator.

```
data/        Bybit V5 sync -> SQLite (data/market.db)
indicators/  exact Kotlin ports, CONFIRMED (closed-bar) only
engine/      signals -> fills -> trades + per-trade feature snapshots
analysis/    metrics, interactive HTML report
tools/       chart replica, parameter sweep
```

---

## 2. The instrument, and the constraint that dominates everything

| Fact | Value | Why it matters |
|---|---|---|
| Symbol | **XAUUSDT** (Bybit linear perp, `baseCoin XAU`) | NOT `XAUTUSDT` (Tether Gold), a different, thinner market |
| Listed | **2026-03-09** | This is the entire history that exists |
| History | **166 days** | ⚠️ The binding constraint on every conclusion |
| Tick size | 0.01 | |
| Taker fee | 5.5 bps per side | **$5.06 round trip at gold ≈ 4600** |
| Trades 24/7 | yes (perpetual) | weekend bars are thin, off-hours flow |

Stored bars, **zero gaps** on every timeframe:
`1m 239,378 · 5m 47,876 · 15m 15,959 · 30m 7,992 · 1h 4,006 · 4h 998`

> ### ⚠️ The single most important number in this document
> **Round-trip cost is $5.06.** On a $25 target that is **20%**. A strategy must win
> ~20% more often than a coin flip just to break even. Any proposal that shortens the
> hold or tightens the target makes this worse, and most "improvements" do exactly that.

### Sample-size ceiling

Runs so far produce **95–190 trades**. At n=100 the 95% confidence interval on a 50% win
rate is roughly **±10 points**. That is wide enough that:

- ranking two configs by net P/L is **not a reliable ordering**;
- any feature bucket with n<30 is noise;
- roughly 15 configurations have already been tested against this one sample, which is
  already deep into overfitting territory.

**`PAXGUSDT` is the unblocker, and it is now synced** — 1m/5m/15m/30m/1h/4h from
2022-03-15, 4.4 years, 2.3M 1-minute bars. It tracks XAUUSDT to 0.237% and is the app's own
gold fallback. Mine on PAXG; confirm out-of-sample on XAUUSDT's 166 days; use BTC/ETH
(§6.1) as a transfer check. A parallel session has already done a pass of this and written
it up in `docs/ENTRY_RULES.md`.

---

## 3. The indicators (principle, not just names)

All are exact ports. Parameters are the app's shipping defaults.

| Indicator | Params | Principle |
|---|---|---|
| **UT Bot / "UT Dynamic Level"** | keyValue **2.0**, ATR period **10** | An **ATR trailing stop**, not support/resistance. Ratchets via `max(prevStop, close − k·ATR)` and only resets on a cross. This is the app's headline level — the gold stepped line on the chart. |
| **ATR** | 14, Wilder RMA | Every distance threshold in the system is in **ATR units**, so it is scale-free — the same rule works at gold 1,800 and 4,600. |
| **EMA stack** | 7 / 14 / 28, SMA-seeded | Ordering (7>14>28) is the trend read. Separation is measured in ATR. |
| **RSI** | 14, Wilder | Momentum **context**, never a 30/70 reversal trigger. |
| **ADX / DMI** | 14, Wilder | Trend *strength*, direction from DI dominance. ≥20 = "trending". |
| **Bollinger** | 20, 2.0, **population** stdev | %B = position in channel. Width percentile = squeeze. |
| **VWAP** | session, anchored 00:00 UTC | Suppressed on 4H (too few bars per session). |
| **Relative volume** | 20-bar mean, **excluding** the current bar | Confirmation only, unsigned. |
| **MACD** ⚠️ | 12 / 26 / 9, SMA-seeded EMAs | **NOT in the app.** Backtest-only addition, so it is *outside* the confirmed indicator parity — there is nothing on screen to compare it against. |
| **Swing structure** | pivot lookback 3 (1H/4H) or 2, min separation 0.5 ATR | Fractal pivots + zigzag filter → HH/HL/LH/LL, BOS, CHoCH. |
| **Volatility band** | ATR% percentile over 150 bars | LOW <0.25, NORMAL <0.75, HIGH <0.90, else EXTREME. Adaptive, not absolute. |

**Why UT is the centre of gravity:** it is the one line the app treats as a decision level,
it is path-dependent (so it carries state), and it is what the user watches on the phone.

---

## 4. What the chart markers mean

Generated by `report.bat` → interactive HTML.

- **▲ apex** = the **fill**: exact bar and price the trade opened at. A horizontal rule
  marks the price.
- **✕** = the exit.
- **Dots on the gold UT line** = that timeframe's own UT cross.
- **The UT rail** below the price panel shows **every** timeframe's UT flips on one shared
  time axis, one lane per TF. This is the multi-timeframe confluence view — seeing 4H, 1H
  and 15m flip together is different information from any one of them alone, and it is what
  the chart exists to make visible. UT cross counts over the full history:
  5m 1,679 · 15m 481 · 30m 204 · 1h 86 · 4h 30.
- Optional panels: **Volume**, **RSI**, **MACD**, and a **VWAP** overlay.
- **Colour = win/loss, NOT buy/sell.** A long-only run shows red triangles for losing buys.
- **The triangle is one bar to the RIGHT of the signal bar.** The condition is confirmed at
  bar *t*'s close; the fill is at bar *t+1*'s open plus slippage. When reasoning about
  "why did it enter here", look one bar left.

---

### 4.1 The hover card — reading history the way the app reads the present

Pointing at any bar in the report reproduces the app's two dashboard panels as they
stood at that bar: **TECHNICAL STATE** (UT bias, EMA alignment, RSI, ADX, VWAP
above/below, BB %B, tick volume — for the timeframe on screen) and **KEY LEVELS**
(nearest resistance and support zone, the H1 "current" price, the other zones, and the
sources each zone is built from).

Three things to know before reasoning from it:

1. **KEY LEVELS is market-wide and on the H1 grid.** The app builds zones from
   M30 + H1 + H4 swings plus H1 session levels and splits them with the H1 confirmed
   close, regardless of the chart you are on (`MarketAnalysisEngine.kt:141-151`). On a
   5m chart the card can be up to 59 minutes old; it is labelled with how stale it is
   and is never newer than the bar you are pointing at.
2. **Zones never derive from the UT level** (`SupportResistanceEngine.kt:46-47`). If a
   proposal treats the UT line as support, it is contradicting the app's own source.
   The sources line under the card shows what the zone actually is.
3. **`N ATR away`** is measured from that bar's close to the nearest zone edge using the
   H1 reference ATR — the same measure the alert layer uses for "price is at a zone",
   and therefore the number a zone-based rule should be written in.

This is the cheapest way to form a hypothesis: step through losing trades with the arrow
keys and read what the card said at each entry.

---

### 4.2 The user's own fills are on the chart

360 real positions were imported from an XM MetaTrader 5 report (`data/mt5.py`) and are
drawn alongside the backtest: hollow chevrons at the fill, a `mine` lane on the rail, a
**Mine** tab in the trades panel, and the position listed in the hover card of the bar it
was taken on.

**This is the most important number in this document for judging the strategy:**

| | trades | window | net |
|---|---|---|---|
| Their real gold trading | 348 | 2026-08-06 → 08-21 | **+3,574.62** |
| `ut_1h_long_v1` backtest | 99 | 2026-03-29 → 08-23 | −347.76 |

Different windows and different sizing, so it is not a controlled comparison — but the
discretionary record over that fortnight is profitable and every mechanical variant tested
is not. Before proposing a refinement to the mechanical entry, it is worth stepping through
their winners with the hover card and asking what the card said at those moments, because
that is a signal source the config does not currently encode.

Two caveats attached to the import, both measured rather than assumed:

* **Times were converted from UTC+3**, the broker server's zone, determined by scoring every
  whole-hour offset against fill-vs-candle price distance (2.6x margin over the runner-up).
* **Prices are the broker's spot contract**, which trades 3.38 (−0.076%) under the Bybit
  perpetual. Fills are never adjusted, so markers can sit slightly under a wick.

---

## 5. Engine rules that constrain any proposal

These are deliberate and should not be "optimised away" — each one exists to stop the
backtest flattering itself.

1. **CONFIRMED bars only.** The app reads the *forming* bar for "where is price now"
   questions; a backtest cannot without look-ahead. Consequence: **backtest signals are
   strictly fewer and later than the app's live alerts.** Results are a lower bound on
   signal count, not a reproduction.
2. **Signal at close → fill at next open + slippage.**
3. **TP/SL checked against each bar's high/low**, never its close.
4. **Ambiguous bars replayed at 1-minute resolution.** If both target and stop are inside
   one bar, the 1m path decides. If 1m is unavailable, **the stop is assumed to fill
   first** (pessimistic).
5. **Higher-timeframe values are as-of-CLOSED.** A 15m bar cannot see a 4H bar that has not
   finished. This is enforced and tested.
6. **Session filter = the real gold session** (Sun 21:00 UTC → Fri 22:00 UTC), not naive
   Mon–Fri. Measured: naive Mon–Fri in MYT keeps 115 bars of dead weekend flow and discards
   144 bars of prime time (Friday's NY afternoon, which in MYT is already Saturday).
7. **Structure is recomputed per bar** on an expanding window — the app's pivot filter
   mutates its own history, so computing once and indexing backwards would be look-ahead.
8. **Zones are windowed to 500 bars**, matching the app's buffer. Swings *accumulate* with
   history (1,633 vs 104 on 30m), and the transitive merge collapses them into giant blobs.
9. **No news data.** ForexFactory is current-week only. All runs have
   `news_available = false`, which in the app's own logic is +2 entry-risk points.

---

## 6. Results so far — everything tested, and what it showed

All runs: XAUUSDT, full available history, gold session, qty 1, fees + slippage + funding on.

| config | entry | exit | n | win% | net | exposure |
|---|---|---|---|---|---|---|
| `ut_1h_long_v1` | 1H `ut_cross_up` OR 1H UT-pullback | $25 / $25 | 99 | 51.5% | −347.76 | 39.5% |
| `ut_1h_long_atr` | same | 1.75×ATR both sides | 95 | 50.5% | −361.02 | 24.8% |
| `ut_mtf_ride_long` | 1H UT bullish → 15m pullback to 15m UT | $25 stop, ride to 5m EMA break | 171 | 25.7% | −576 | 8.8% |
| ↳ best of 12-way sweep | same | 5m/ema_14/closes=3 | 169 | 26.6% | −374 | — |
| `ut_mtf_ride_short` | mirrored | mirrored | 190 | 27.9% | −882.38 | 6.4% |
| **buy & hold 1 unit** | — | — | — | — | **−524.90** | 100% |

### Context that changes the reading

**Gold FELL 10.1% over the window** (5127 → 4608). So:

- Long variants "beating" buy-and-hold is **not an edge** — they are flat ~91% of the time,
  so they simply did not participate in the decline. Normalised for exposure they are worse.
- The short side lost *more* than the long side **in a falling market**, which is a strong
  negative signal about the exit, not the direction.

### Per-branch breakdown (`ut_1h_long_v1`)

| branch | n | win% | gross | net |
|---|---|---|---|---|
| `ut_cross_up` | 65 | 52.3% | +108.22 | −211.74 |
| `ut_bias_bullish + ut_level_near + ema_stack_bullish` | 34 | 50.0% | +29.37 | −136.02 |

Both roughly coin flips. **Gross positive, net negative** — costs are the entire deficit.

### 6.1 Across instruments — the strongest single result in this project

`ut_1h_long_v1` on BTCUSDT and ETHUSDT over the **identical** window, with position size
scaled by the ATR ratio so every symbol risks the same dollars per trade (see
`engine/symbols.py` — without this the comparison measures tick size, not strategy):

| symbol | median ATR-14 | qty | n | win% | 95% CI | **gross** | fees | net |
|---|---|---|---|---|---|---|---|---|
| XAUUSDT | 15.02 (0.335% of px) | 1.0 | 99 | 51.5% | 42–61% | **+137.59** | 482.28 | −347.76 |
| BTCUSDT | 393.78 (0.587%) | 0.038 | 97 | 44.3% | 35–54% | **−290.81** | 279.11 | −575.01 |
| ETHUSDT | 15.09 (0.751%) | 1.0 | 90 | 44.4% | 35–55% | **−237.12** | 195.02 | −439.32 |

**Read the gross column.** On gold the entry is gross-POSITIVE and loses only to costs. On
BTC and ETH it is gross-NEGATIVE — the signal itself has no edge there, so no cost
reduction could rescue it.

This reframes the problem. It was "a decent signal eaten by fees". It is now "a decent
signal **on gold specifically**, eaten by fees". Note the direction: the two instruments
with the *lighter* fee burden (BTC 279, ETH 195, versus gold's 482 — equal ATR risk needs
less notional on a more volatile instrument) are the two that perform worse.

**Consequences for any proposal:**

1. A change justified by "this is how trend-following works generally" is weaker than it
   looks. The generic version of this entry does not work on the two most liquid perpetuals
   in the same window.
2. If a filter improves gold *and* flips BTC/ETH gross-positive, that is real evidence.
   If it only improves gold, it is very likely curve-fitting to 99 trades.
3. **BTC and ETH are now the out-of-sample set you did not have.** They are not gold, so
   they are not a substitute for PAXG history — but a proposal that survives all three is
   substantially better supported than one tested on gold alone.

⚠️ The confidence intervals overlap (42–61 vs 35–54), so this does not establish gold's
superiority at p<0.05. The **sign flip in gross** is the load-bearing observation, and it
is still one sample per instrument.

⚠️ This says nothing about *other* strategies on BTC/ETH. It says this UT-pullback entry,
tuned on gold, does not transfer.

---

## 7. ⛔ Already tried and disproved — do not re-propose

### 7.1 "Only buy when price is above EMA 7, 14 and 28"

**Fires once in 166 days** when combined with a pullback entry. A pullback deep enough to
reach the UT line is essentially never still above the fast EMA — the two conditions
contradict each other. Use `ema_stack_bullish` (7>14>28 *ordering*) for "trend intact",
which gives 71 signals at the same distance.

### 7.2 "Ride the trend: exit when 5m closes below EMA14" (naive)

Held trades **1.7 bars**; 169 of 171 exits were the trail. Same trap: a pullback entry
starts below the fast EMA, so the exit is already true at entry. Fixed with
`arm_on_recross` — the trail stays dormant until the LTF first closes back on the
favourable side. That lifted win rate 16.4% → 25.7%, still net negative.

### 7.3 Tuning the trail

12 combinations swept (trail timeframe 5m/15m × ema_14/ema_28 × 1/2/3 confirming closes).
**All 12 lose**, net −374 to −717, win rate 24–29%. A flat surface of losses is not a
tuning problem.

### 7.4 Exit style in general

Fixed $25 vs 1.75×ATR changed net P/L by **$13 across ~100 trades**. The exit style is not
where the problem is.

---

## 8. Diagnosis — where the deficit actually is

The mechanism works; the hit rate doesn't.

- **Average MFE is 12.7–27.7** — trades genuinely move in favour.
- The trailing exit achieves a **~2:1 payoff** (avg win 22.8 vs avg loss 11.3).
- Breakeven at 2:1 needs a **33%** hit rate. Actual: **26%**.
- The fixed-target variants sit at ~51% win with a ~1:1 payoff, where breakeven after
  costs needs ~60%.

So there are two independent gaps, and a proposal should be explicit about which it targets:

| Gap | Current | Needed | Lever |
|---|---|---|---|
| Hit rate at 2:1 payoff | 26% | 33% | **better entry filtering** |
| Hit rate at 1:1 payoff | 51% | ~60% | **larger targets** (dilutes the $5.06 fee) |

**The arithmetic strongly favours bigger moves.** At a 4×ATR target the same $5.06 fee is
~9% of the move instead of 20%. Nothing tested so far has held for long enough to find out.

**But §6.1 adds a third gap that costs cannot explain.** BTC and ETH lose money *before*
fees on the same entry logic, over the same days. Whatever makes this setup work on gold is
not captured by any rule currently in the config — so "tune the exit" and "tune the target"
may both be optimising a signal that is only accidentally positive.

---

## 9. Where the levers are

### Entry filters — untested, and every trade already carries the data

Every trade stores **~150 features** across 5m/15m/30m/1h/4h in `trade_features`
(`value_num` for numbers, `value_txt` for labels). Nothing has been mined yet **because the
sample is too small to mine honestly**. Candidates worth testing once the sample supports it:

- `volatility_band` / `atr_percentile_250` — does the setup only work in NORMAL volatility?
- `adx_14` ≥ 20 on the 1H — filter out chop
- `htf_4h_ut_bullish` — align with the 4H, not just the 1H
- `structure_state` — only take longs in BULLISH structure
- `hour_of_day_myt` / `session` — the London/NY window vs Asia
- `relative_volume` — participation behind the move
- `bb_width_percentile` — enter out of a squeeze rather than mid-expansion

### Exit ideas not yet tested

- Larger fixed targets (3–5×ATR), which is what the cost maths points at
- Partial exit at 1×ATR + trail the remainder
- Trail on the **UT level itself** rather than an EMA (`trailing.mode: ut_level` exists in
  the config schema but is **not implemented in the engine yet**)
- Time-of-day exits (flatten before the weekend gap)

### The 87 available `signal_id`s

⚠️ **`SIGNAL_MAP.md` also documents ~57 signals that are NOT implemented** (zones, the
decision chain, news, the MTF engine). Using one of those in a config raises `KeyError`.
That file's status header lists both sets, and `tests/test_signal_docs.py` fails if they
drift apart again. **This list is the implemented one.**

```
  adx_meaningful                  adx_strong                      adx_trending
  adx_very_weak                   bb_at_lower_band                bb_at_upper_band
  bb_expansion                    bb_lower_band_area              bb_squeeze
  bb_upper_band_area              bos_bearish                     bos_bearish_event
  bos_bullish                     bos_bullish_event               choch_bearish
  choch_bearish_event             choch_bullish                   choch_bullish_event
  di_bearish                      di_bullish                      dynamic_level_touch
  ema_spread_compressing          ema_spread_expanding            ema_stack_bearish
  ema_stack_bullish               ema_stack_flip_bearish          ema_stack_flip_bullish
  ema_stack_mixed                 htf_1h_bearish                  htf_1h_bullish
  htf_1h_ema_stack_bearish        htf_1h_ema_stack_bullish        htf_1h_ut_bearish
  htf_1h_ut_bullish               htf_1h_ut_cross_down            htf_1h_ut_cross_up
  htf_4h_bearish                  htf_4h_bullish                  htf_4h_not_bearish
  htf_4h_ut_bearish               htf_4h_ut_bullish               macd_above_signal
  macd_above_zero                 macd_below_signal               macd_below_zero
  macd_cross_down                 macd_cross_up                   macd_hist_rising
  market_closed                   price_above_all_emas            price_above_vwap
  price_below_all_emas            price_below_vwap                price_extended_above_ema28
  price_extended_below_ema28      price_near_mean                 regime_range_bound
  regime_trend_down               regime_trend_up                 rsi_above_50
  rsi_above_70                    rsi_below_30                    rsi_below_50
  session_asia                    session_london                  session_ny
  structure_bearish               structure_bullish               structure_range
  usable                          ut_bias_bearish                 ut_bias_bullish
  ut_cross_down                   ut_cross_up                     ut_level_near
  ut_position_long                ut_position_short               volatility_extreme
  volatility_high                 volatility_low                  volatility_measured
  volatility_normal               volume_above_average            volume_dead
  volume_normal                   volume_surge                    volume_thin
```

Conditions nest arbitrarily with `all_of` / `any_of`, and `expr:` allows column expressions
such as `"close > ema_28"`.

---

## 10. How to run things

```bash
C:\inetpub\Claude\ITSupport\backtest\report.bat backtest\configs\ut_mtf_ride_long.yaml
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli backtest backtest/configs/ut_1h_long_v1.yaml
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.tools.sweep backtest/configs/ut_mtf_ride_long.yaml --trail-sweep
```

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli runs
```

Run one config against several instruments on one shared window, with size normalised so
the results are comparable (this is what produced §6.1):

```bash
backtest\.venv\Scripts\python.exe -m backtest.cli backtest backtest/configs/ut_1h_long_v1.yaml --symbols XAUUSDT,BTCUSDT,ETHUSDT
```

Keep it running against live data. The chart updates every **5 seconds** with the forming
bar folded from 1-minute data; the backtest re-runs every 300 s; neither ever reloads the
page, so your zoom, selected trade and any measurement survive. The page gains a symbol
switcher, a Live panel showing which entry conditions are true right now and which is
blocking, a **Measure** tool (or Shift+drag) reading Δ price, %, ×ATR, bars and elapsed
time, and a **MYT / NY / UTC** switch for the time axis (tooltips always show all three):

```bash
C:\inetpub\Claude\ITSupport\backtest\watch.bat
```

```bash
backtest\.venv\Scripts\python.exe -m pytest backtest/tests -q
```

Tests: **248 passed, 1 skipped**. The skipped one activates if a golden CSV is exported;
parity was instead confirmed directly against the live app, so it is not needed.

Runs are append-only — every run is a new `run_id`, and the same config always produces the
same `config_hash` and an identical trade list.

---

## 11. How to judge a proposed change

1. **Does it survive the cost floor?** $5.06 round trip. If the change shortens holds or
   tightens targets, it must raise the hit rate enough to pay for that.
2. **Is the condition self-contradictory with the entry?** Two of the ideas tested so far
   failed this way. Check whether the filter can be true at the moment the entry fires.
3. **Does it need more than 166 days to evaluate?** If the answer is "we'd need to check the
   feature buckets", measure it on **PAXGUSDT** (4.4 years, synced) and treat XAUUSDT as the
   out-of-sample confirmation, not the training set.
4. **Is it one change or several?** With n≈100–190, testing many variants against one sample
   is how a spurious winner is manufactured. Prefer one hypothesis with a stated mechanism.
5. **Would it look good only because gold fell 10% in this window?** Long-only and short-only
   results are not symmetric here. Check both.
6. **Does it hold on BTC and ETH?** They are synced over the same window and one command
   away (§10). A filter that helps gold and leaves BTC/ETH gross-negative is a filter fitted
   to 99 trades. This is the cheapest out-of-sample check available today.

---

## 12. Honest summary for the next session

Across three strategy variants, twelve sweep combinations and both directions, **nothing
tested so far has an edge on this data**. The entries are close to coin flips; the costs are
20% of the target; and the sample is too small to distinguish a weak real edge from noise.

Running the same config on BTCUSDT and ETHUSDT over the same window (§6.1) added the one
genuinely new fact: **gold is the only one of the three where the entry is gross-positive.**
BTC and ETH lose before fees. So the working description is no longer "a good signal eaten
by costs" but "a signal that only looks good on gold, eaten by costs" — and the second is a
much weaker claim.

The three most promising directions, in order:

1. **Use the PAXGUSDT sample** — now synced, 4.4 years, 1m through 4h. A parallel session
   has already specified a trigger against it in `docs/ENTRY_RULES.md`; read that before
   proposing anything, and note §8 there lists what the engine still lacks to run it.
2. **Test materially larger targets** (3–5×ATR), because the cost arithmetic says the current
   target size cannot clear its own fees at the observed hit rate.
3. **Use BTC/ETH as the cheap out-of-sample check** on anything proposed for gold. It will
   not prove a change works, but it will kill curve-fits quickly and for free.

Anything that adds another filter to the existing entry and re-runs on 166 days will produce
a number, but not a trustworthy one.
