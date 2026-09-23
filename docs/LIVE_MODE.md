# LIVE_MODE.md — the always-on page, the risk read, and the gate

What `watch.bat` does now, why the backtest is no longer in the loop, and exactly
how much of the risk read is measured (some) and how much is not (most).

`ENTRY_RULES.md` = the rule · `OPERATING_PLAN.md` §8/§10.5 = the shipping config ·
`AUTOMATION.md` = the bot spec · **this file** = what runs while you watch.

---

## 1. What changed

`watch.bat` used to re-run a **backtest over 166 days, every 300 seconds, per
symbol** — about 7 s each — and rebuild an 11 MB payload from the result. A
backtest cannot change until a bar closes, so almost all of that work re-derived
a number that had not moved.

It is gone. The slow loop now:

1. syncs candles from Bybit, and your fills from the MT5 terminal,
2. evaluates the **entry rule on the newest closed bar**,
3. reads the **risk vectors**,
4. rebuilds a ~2 MB page payload.

| | before | now |
|---|---|---|
| slow cycle, 3 symbols | ~30 s + 3 backtests | **~31 s**, no backtest |
| fast tick, 3 symbols | ~1 s | **~1.5 s** (adds 3 risk reads) |
| payload per symbol | 11 MB | **~2 MB** |
| what the trades panel holds | simulated backtest trades | **signals the rule actually emitted** |

`--backtest` restores the old behaviour when you genuinely want to look at a run.

### The trades panel is now a signal log

`build_live_payload` fills it from `reports/signals.jsonl` — every bar where
`would_enter` was true. These are real decisions taken at a real time, which is
better evidence than a simulated trade, and the chart's `signal` rail keeps
working. **No P/L is invented**: an open signal has no exit, `net` is 0, and
realised money lives in `my_trades` (your actual broker fills).

The Summary panel changed with it — bars evaluated, legs-true rate, would-enter
rate, sent, vetoed. **Deliberately not a win rate.** These signals have no closed
outcomes; a win rate over open positions would look like the measured 42.5% and
mean nothing.

---

## 1b. What the rule trades, and on which bracket

```
symbol    XAUUSDT (Bybit gold)   anchor 15m   side  LONG ONLY
bracket   TP +25 / SL -25 in price, FIXED     time stop 96 bars (24 h)
size      0.01 lot (= 1 oz, so 25 points = $25 either way)
session   gold_session, no hour-of-day window
```

**Bybit gold, not `MT5:GOLD`.** The MT5 terminal's rates went stale by eight
hours in normal use — it only serves fresh bars while it is open, logged in and
has the symbol in Market Watch, and a live page whose feed silently stops is
worse than no page. Bybit's `XAUUSDT` is always current. The cost is a basis:
XM's spot `GOLD` sits **~3.38 under this perp (−0.076%)**, measured over 348
fills, so a level quoted here is that much above the one your terminal shows.
Constant, small against a 25-point bracket, and not zero.

Signals are drawn **on the gold chart**, in the same `signal` rail lane as
before, directly under the `mine` lane holding your own broker fills — which is
the point: the two are one glance apart.

⚠️ **The +25 / −25 bracket is NOT the measured system**, and the page says so on
every signal (`bracket_measured: false`). `OPERATING_PLAN.md` §8 chose TP 5.0 /
SL 2.5 × ATR14 on survival grounds, and §8.2 measured a **symmetric bracket as
the cell that took a $200 account to −$130** on the real 2022–23 sequence. On the
MT5 panel a symmetric bracket measured 53.5% against a 51.3% break-even — real,
but +2.2 points with a confidence interval that includes failure.

A fixed bracket also scales with nothing. 25 points is ~1.8 ATR when ATR is 13.5
and ~3.7 ATR when ATR is 6.7, so the same trade is a different bet in a violent
week than a quiet one. That is the trade-off you are taking, stated once.

`bracket_mode="atr"` in `RuleConfig` returns to the measured bracket.

### Your own positions do not silence the rule

`max_concurrent` counts **only positions this rule opened**, tracked by the
`link` records in `signals.jsonl`. With `execute: false` nothing has ever been
linked, so every slot is free and every firing bar produces a visible signal.

This matters: you routinely hold several manual GOLD positions, and counting
those against the rule's cap made it go quiet for exactly as long as you were
trading — which is when a second opinion is worth having. Set
`manual_positions_block = True` only when an executor is live and sharing one
account, where the binding constraint is margin rather than the rule's queue.

## 2. Who decides what — and this is the important part

```
engine/live_signal.py    the RULE    decides WHEN      MEASURED
                                                       n=588, 42.5% vs 34.2% break-even,
                                                       +3.3 pts vs a matched random null
engine/risk_engine.py    the RISK    decides WHETHER   NOT MEASURED
                         read        (veto only)       hand-set 40/30/30 weights,
                                                       ported from the Android app
```

**The rule fires. The gate can only subtract.** `gate()` returns a `GateResult`
that carries no price, no side and no size — it is structurally incapable of
opening a trade, and `tests/test_risk_engine.py` asserts that.

This split is not caution for its own sake. `FEATURE_MINE.md` records **six mined
filters that were tested and rejected**, two of them for the same structural
reason (they were true at the moment the entry fired). Nothing in
`RiskExecutiveEngine.kt` has been through that. Making it the trigger would spend
the only measured edge in the project to buy one with no evidence behind it.

The defaults are correspondingly loose:

```
max_danger       70     veto only a clearly bad setup
min_confidence   25
require_bias     off
```

Tighten them with `--max-danger` / `--min-confidence` / `--require-bias`, turn the
gate off entirely with `--no-gate` (it still logs, it just never vetoes).

**Every evaluation is logged either way.** A veto is written into `signals.jsonl`
under `extras.gate` with the Danger/Confidence that caused it, so "the rule fired
and the gate blocked it" stays distinguishable from "the rule never fired" and
from "the process was not running".

---

## 3. The risk read — what is a faithful port and what is not

`engine/risk_engine.py` transcribes `RiskExecutiveEngine.kt` exactly: the
**Trend & EMA 40% / Order-Book 30% / Momentum 30%** danger matrix, every reference
constant, the confidence blend, the structural bias, the "Show Why" reasons, and
the double-barrier analogue sim at its 20% weight.

Three of its **inputs** do not exist in this project. That gap is where a
plausible-looking number becomes a lie, so all three are named on the page:

| Kotlin input | here |
|---|---|
| `LiquidityWall` (resting bid/ask + notional) | **does not exist.** Substituted with this repo's **KEY LEVELS zones** — zone distance for wall distance, zone `strength` for notional. A zone is a price people defended in the past; a wall is liquidity resting now. `orderbook_is_proxy` is always True. |
| `oiDeviationPct` | **fetched for Bybit symbols** (`Watcher._sync_context`), measured against a 96-bar mean by `risk_feed._oi_deviation_pct`. **MT5 has no OI at all**, so on `MT5:*` the term is **skipped and the rest renormalised** — its absence lowers Confidence rather than reading as low danger. |
| `longRatio` / `shortRatio` | not fetched. Same handling. |
| `TfCandleSignal.wickBias` | **derived here**, in `wick_bias()`, from candle geometry. The rule is stated in that function, not hidden. |

Two adaptations worth knowing:

* **The bracket is in ATR or in dollars, never in percent.** The Kotlin takes
  `takeProfitPct`; nothing here trades in percent. `RiskSettings` carries the live
  bracket in its own units — ATR multiples, or the fixed `tp_usd`/`sl_usd` — and
  converts at evaluation time, so the percent maths is unchanged and the barriers
  being simulated are the ones actually sent. The Kotlin's TP_MIN/TP_MAX clamps
  still guard an ATR bracket, where a spike really can produce an absurd target;
  they are **not** applied to a fixed one, because silently widening a $10 stop to
  satisfy a 0.5%-of-price floor would have the sim measure a trade nobody takes.
* **The sim horizon matches the trade.** The Kotlin fixes 48 bars of 5m (4 h).
  The shipped time stop is 96 × 15m = **24 h**, so `settings_for()` converts it
  into sim-timeframe bars. A sim that stopped at 4 h would report the odds of a
  different trade.

### The analogue sim is not a backtest

It has **no entry rule, no sizing, no fees and no sequencing**. It finds past bars
whose EMA28 distance (±0.5%) and RSI zone match now, walks each forward to the
bracket, and counts which barrier was touched first. A bar touching both counts as
**stop first** — the same conservative tie-break `validate_rule.walk_forward`
uses. That is why it costs milliseconds and why it is blended at only 20%.

---

## 4. The three buttons

**☕ Keep awake.** Two locks, because neither alone is enough. The page takes a
`navigator.wakeLock` screen lock — but the browser releases it the instant the tab
is hidden, so minimising restarts the display timer. So the button also calls
`/api/awake`, and the watcher process holds
`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)`,
which is what actually stops Windows' display and sleep timers whatever the
browser is doing. The wake lock is re-requested on every `visibilitychange`.

It does **not** override the workstation lock (that is a security timer, not an
idle timer — the machine stays awake behind it), a lid close, or battery saver.
The setting survives a reload. It is cleared on shutdown, and by process exit
anyway.

**📋 Copy analysis.** ~17 KB, built server-side in `analysis/brief.py` because it
needs the database (open positions, KEY LEVELS, funding, open interest, the
signal log), none of which is in the page payload. Three parts, in this order:

1. **The mandate** (`brief.MANDATE`) — the owner's own instruction set, in
   Traditional Chinese: the macro-desk persona, the engine's scoring rules, the
   session timing rules, and the required output shape (four core modules plus
   three that fire only when their condition holds).
2. **The snapshot** — gathered by `analysis/context.py`, which exists so that no
   rule in the mandate has to answer *數據不足* about a value the database
   already holds. See below.
3. **The reading notes** — what each number is, and is not.

### The mandate separates two kinds of "don't know"

The **engine's own scoring is fixed and must be obeyed literally.** Reading in an
outside convention — "below 1.0x volume is fake strength" — changes the verdict
on every bar between 0.8 and 1.2, and this engine has no 1.0 line at all. Its
bands are ≥1.5 / ≥1.2 / 0.8–1.2 / 0.5–0.8 / <0.5, and the blob prints them next
to the value every time.

**Market background is a different kind of gap** — externally checkable — and the
mandate explicitly authorises going and checking. Conflating the two made a model
stall on questions it could have answered.

### What `analysis/context.py` adds, and why

| the mandate's rule needs | now supplied |
|---|---|
| ADX **slope**, not one reading | last 4 bars + slope. ADX 20 rising and ADX 20 falling are opposite signals. |
| the engine's volume **bands** | `relative_volume` against the 20-bar mean, plus the band label |
| **participation divergence** | flagged when `trend_strength > 85` while volume is under 1.2x — the score is then carried by structure, not by real trade |
| funding vs a **baseline** | Bybit's neutral is 0.0100%/8h; the blob prints rate, baseline, deviation and a verdict. Paying exactly the baseline is **not** crowding. |
| **ΔOI** four-quadrant | open interest is now synced each cycle (`Watcher._sync_context`), so ΔOI vs Δprice resolves to one of the four quadrants — or says plainly that it cannot |
| session applicability | `timing_rules_apply` is resolved for the reader, so the three MYT windows are never applied on a weekend |

Syncing OI also **switched the risk engine's own OI momentum term on** — it had
been reporting "open interest: missing" since the port. `risk_feed._oi_deviation_pct`
measures the current level against its own 96-bar mean, because Bybit publishes
the level and the Kotlin expects a deviation.

### Open interest is the one series that cannot be recomputed

Everything else here can be rebuilt from scratch: delete a candle and re-fetch
it, and the bytes come back identical. Open interest does not work that way.
Bybit serves roughly **170 days** of 15m OI and then a reading is gone from the
source permanently — measured, not read off the docs: on 2026-08-25 an ask for
200 days returned 16,257 points beginning 2026-03-09.

So the `open_interest` table is **never pruned**, and two things protect it:

* **the live sync resumes from `MAX(ts)`**, not from a fixed lookback. The
  original 200-bar (50 h) window turned any longer outage into a permanent hole
  — nothing re-requested the missing middle and nothing reported it, so the gap
  simply stayed. Resuming makes the request self-sizing: a point or two on a
  normal cycle, a few hundred after a night off, floored at source retention.
* **`python -m backtest.cli oi`** backfills history from *before* the watcher
  first ran, which the forward-only sync can never reach. Re-running it is safe
  (the upsert is keyed on `symbol, interval, ts`) and is the right move after
  any long outage.

    python -m backtest.cli oi                     # all three symbols, ~170 days
    python -m backtest.cli oi --symbol XAUUSDT --days 30

### OI on the chart

The `Open interest` toggle adds a pane below MACD. Three choices in how it is
drawn, each with a plausible-looking wrong answer:

| choice | why not the alternative |
|---|---|
| a bar's OI is the **last point inside it** | OI is a level, not a flow. Summing invents size that never existed; averaging smears the bar where OI actually turned, which is the one thing the pane exists to show. |
| timeframes finer than 15m get **no pane at all** | OI is published on a 15m grid. Repeating one reading across three 5m bars draws a flat line indistinguishable from genuinely flat OI — a fabricated observation, worse than an admitted gap. |
| a bar with no point stays a **hole** | Joining across a stretch the watcher missed would draw a straight line the market never traded. |

The dashed reference is **the engine's own baseline** — the mean of the last 96
points of the 15m series, the number `risk_feed._oi_deviation_pct` measures
against and that feeds Danger%. It is drawn rather than merely described so the
input to the score is visible on the same screen as the score.

The hover card adds OI, its deviation from that baseline, ΔOI on the bar, and
the **Δprice × ΔOI quadrant** — worded exactly as `analysis/context.py` words it
for the AI brief, so the chart and the brief cannot tell you different stories
about the same bar.

### The "what is absent" section is not optional

A model handed a factor labelled "Order-Book 30%" will reason about an order
book. There isn't one. A mandate that mentions `NEXT IMPORTANT EVENT` will make
it hunt for a calendar; this project has none. Left unsaid, a model does not
decline — it writes confident prose about liquidity and events it cannot see,
indistinguishable from the parts that are real. So ABSENT is printed **before**
the numbers it qualifies, and `tests/test_brief_context.py` asserts that ordering
— a caveat printed after 300 lines of confident values is one nobody applies.

**🔊 Sound.** A synthesised two-tone chime on each new signal (no audio file — the
page is self-contained). Browsers block audio until a user gesture, so clicking
the button plays the chime, which both confirms the setting and unlocks the audio
context so the first real signal is not the one silently swallowed.

**ℹ Details.** The full TECHNICAL STATE / KEY LEVELS hover card, **off by
default**. It used to fire on every pixel of mouse movement and cover the price
you were trying to read. What replaced it as the default is a proper
**crosshair**: it follows the pointer, snaps to the bar on the x axis, stays free
on the y, and prints the **price on the price axis** and the **time under the
time axis**. Past the newest bar the time tag reads `—` rather than extrapolating
a timestamp for a bar that does not exist.

## 4b. Room to the right of the live edge

The chart keeps **18% of the visible window clear beyond the newest bar**, so the
live edge is never pinned under the price axis. **Now** parks the view there;
panning runs into that room and stops, so the chart cannot be dragged into
blankness.

Two things had to change for that to work. Bar width now comes from the *window*
(`state.count`) rather than from how much data happens to be inside it — dividing
by the visible bar count made the candles stretch to fill the pane and silently
ate the room. And the projection anchors on `state.start` (a float) rather than
its floor, which makes panning smooth instead of snapping bar to bar and makes
`x()` and `xToI()` exact inverses.

---

## 4c. The session map — 亚盘定范围，欧盘扫流动，美盘定方向

A full field dictionary, label definitions and entry/SL/TP spec for
Signal 6 was written out to `C:\Users\User\Downloads\SIGNAL6_SPEC.md`
(the owner's copy, outside the repo). The RETRAINING pipeline built from
`SIGNAL6_RETRAIN_SPEC.md` lives in `engine/signal6_retrain.py` +
`tools/retrain_signal6.py` (Model 0 replicates the shipped numbers
exactly; Model 1's regime P(win) gate measured 57.6% walk-forward /
56.5% cross-feed — research only, not deployed).

The owner's intraday framework (`XAUUSD_黄金日内交易口诀与框架.md`), mechanised as
an **observation**. `engine/session_map.py` is the single definition; the fast
loop rebuilds it each tick from the minute frame it already fetched and writes
it into `live.json`, `status.json`, the cockpit and the Copy-analysis brief.

**Windows (MYT, one definition):** Asia **07:00–15:00** · Europe **15:00–20:00**
· US **20:00–05:00**. This settles Q26 for user-facing surfaces only —
`features.session_myt` and Signal 4's mining inputs keep their own windows
because they describe what was measured.

**Levels per MYT day:** Asia high/low (the range), previous day high/low,
previous US-session high/low, day open. The first three groups draw as **dashed
lines** on the chart (toggle: **Sessions**); gold = Asia, orange = prev day,
purple = prev US.

**Detection, on 5m bars that had closed** (a bucket whose five minutes have not
elapsed is a forming bar and does not exist to this map — same CONFIRMED rule
as everything else):

* **sweep** — a poke beyond the level that returned: a single-bar wick that
  closed back inside, or one close beyond followed by a close back inside.
  扫盘不追单.
* **break** — two consecutive 5m closes beyond the level. The event carries
  `vol_ok`: whether the breaking bars' median volume beat 1.2× the trailing
  20-bar median (突破有量). A quiet break is still reported, flagged.
* **reclaim** — after a break, a close back inside the range. A failed
  breakout is information, not silence.
* **US read** — after 20:00, whether New York **confirms** or **reverses**
  Europe's move off the Asia mid (needs ≥15% of the Asia range to count).
* **日内倾向 (day bias)** — the phase-aware read, every line with its stated
  basis (the facts it was read from, printed next to it): **亚盘** range by
  definition (the direction is not called in Asia); **欧盘** Europe's
  statement from what it did to the range (a volume-confirmed break holding
  reads long/short; a sweep only counts as a statement once price has crossed
  back through the mid; nothing happened is range); **美盘 20:30 MYT** — half
  an hour in, the owner's own timing — the DAY VERDICT: long, short or range.
  Europe must have travelled ≥0.15R off the mid to have chosen a side; a US
  confirmation carries the direction, a reversal that clears the mid flips
  the day, one that does not makes it a two-sided range, and a Europe that
  never travelled is a range day outright. The verdict pushes once per CHANGE
  on the `session` channel (`日内判定 — 看多/看空/震荡` with its basis lines),
  and every surface that shows it labels it a framework read, not a signal.

**The map itself is not a signal.** Nothing in `session_map.py` enters the
rule, the gate or any decision log. Session events push on their own Telegram
channel (`session`, on by default, P2) with a message shape that cannot be
mistaken for `signal_fired`; the page's alert banner says "structure, not a
signal" in so many words. A restart absorbs the day's existing events silently
rather than replaying them onto a phone. What this gives the *measured* rule is
exactly what the framework says comes first: 先判断「市场在哪里」，再判断
「市场想扫哪里」.

**Signal 6 trades ONE shape — the 扫盘→突破 sequence — and its geometry
("C3") was chosen by the walker.** v1 traded sweep reversals with TP at the
range mid and SL beyond the wick; measured on the full 1m history it lost in
every variant tested (23-28%, net negative throughout). A ~30-configuration
search then found the robust winner, each piece framework-meaningful:

* **The sequence is the signal**: a break of the Asia level trades only when
  the SAME side was swept earlier in the day (扫盘→突破, the 口诀's third
  layer read literally), then two closes beyond the level WITH volume
  (突破有量), confirmed 15:00–22:00 MYT, on a range worth having (≥0.1% of
  price). The prior-sweep requirement was the single biggest factor found:
  61.9% alone vs the 53.6% no-sequence baseline.
* **The retest is the invalidation**: SL inside the range at level − 0.50R.
  TP one range beyond the level. The buffer plateau runs 0.35–0.50R.
* **Sweeps do not trade** — observed and announced as structure, never
  entered. 扫盘不追单 is the framework's own instruction and every tested
  sweep geometry agreed with it. `trade_sweeps=True` restores them.
* **Every qualifying sequence trades** (`max_trades_per_day=0`, ~0.6/day,
  1.7 on a signal day). The owner asked for AT LEAST one trade every
  weekday; measured honestly that is not achievable with quality — ~25% of
  weekdays produce no structure to trade at all, and every fallback tier
  below A trades under its break-even. The measured curve, causal:

  | mode | trades | win | net |
  |---|---|---|---|
  | **A-grade only, unlimited (shipped)** | **105** | **62.9%** | **+1333.71** |
  | A-grade only, 1/day | 61 | 63.9% | +865.55 |
  | + C tier (any break ≥19:00) | 65 | 61.5% | +802.53 |
  | + B + C (`daily_ladder=True`) | 85 | 55.3% | +689.59 |

  The B tier (late volume breaks, 25 trades · 32%) and the D last resort
  (act at 20:00 on an early sweep of a break-less day, 5 · 40%) are the
  poison; the ladder stays as config for coverage at the documented cost.

The entry timing was measured both ways before freezing. `break_entry:
"retest"` (回踩不破 as an ENTRY — wait up to an hour for a pullback to the
level that closes back on the far side, enter there) is implemented and
config-driven, but it LOST to the confirmation entry on the full history:
**52.4% / +857 vs 53.6% / +1450** on the pre-C3 baseline, because 25% of
confirmed breaks never retest and the missed runners outweigh the better
fill price. The shipped default enters the bar after the second
volume-confirmed close.

The shipped record on the full history, as of 2026-09-20: **105 trades ·
62.9% vs a ~42% break-even · net +1333.71** — chronological halves 61.5% /
64.2%, both net positive; **Europe entries 54 · 66.7%** / **US entries 47 ·
59.6%** (break_low 57 · 66.7%, break_high 48 · 58.3%). **The honest caveat:
in-sample selection** — chosen from ~30 configurations read on this same
data.

**The out-of-sample check is now on file and it is the number to trust.**
The same untouched rules walked over `MT5:GOLD` spot 5m, 2025-03 → 2026-08
(17 months, a different feed at a different price, mostly before the tuning
window; `data_interval="5m"`): **201 trades · 47.8% vs 42.1% break-even ·
net +1204** — pre-Bybit fully-independent part n=127 · 43.3% · +336;
overlap n=74 · 55.4%. The edge is real but thin: expect slightly above
break-even live, not the in-sample 62.9% (caveats: 5m path resolution,
broker tick volume). Loosening for sample size on the SAME data
(`require_volume=False`, window to 24:00) gives ~197 trades · 57.9% —
break_low 67.9% carries it, break_high 46.2% barely clears.

**Signal 7 — the ExpD regime gate, live as an EXPERIMENTAL channel.**
Runs beside Signal 6 (which is untouched): every break event gets the
SAME features the retraining pipeline computed (`features_for_break` —
one shared definition, so train/serve skew is structurally impossible;
pinned bit-for-bit by `tests/test_signal7.py`), a logistic P(win), and a
per-regime acceptance threshold frozen in `configs/signal7_model.json`
(fitted by `tools/fit_signal7.py` on MT5:GOLD full history, thresholds
picked on train only). Brackets are the fixed baseline, identical to
Signal 6. **Backtested by `tools/backtest_signal7.py`:** in-feed
walk-forward **60.6% / PF 2.54** (104 trades, MT5 17 months); the honest
time-respecting QUARTERLY cross-feed on Bybit — model trained strictly
before each quarter — **50.0% / net −64**. (The "cross-feed 65.8%" quoted
while building the channel came from a model trained on data concurrent
with its test window; it does not count, and every surface that quoted
it has been corrected.) Its push and panel say EXPERIMENTAL; the
promotion gate (n≥30 settled PASS, win≥52%) is pre-committed. Without
the model artifact the channel stays quiet and says why.

**Not done, on purpose:** CPI/NFP/FOMC expectation-difference logic. There is
no economic calendar in this project, and a fabricated one would be worse than
the ABSENT note the brief already carries. 数据前不赌 remains a human rule.

---

## 5. Staleness is stated, loudly

A live page showing an eight-hour-old bar as current is the worst failure it can
have — every number stays perfectly plausible. So:

* the risk panel shows a **red banner** when the newest closed anchor bar is more
  than 3 intervals old, naming the age and the bar;
* the copy-analysis blob carries `<-- STALE, the feed is behind`;
* a separate amber banner fires when the **zone cache** is behind the candles you
  already have (a fixable, different problem from a broker being offline).

This is why `MT5:GOLD` was dropped as a chart symbol: it comes from the terminal
and goes stale whenever MetaTrader 5 is closed, logged out, or does not have
`GOLD` in Market Watch — eight hours behind, in normal use, while every number on
the page stayed plausible. Bybit symbols do not share that failure mode. The MT5
connection is still used for **your fills** (`broker_trades` → the `mine` lane),
where being a few minutes behind costs nothing.

---

## 6. Running it

```
watch.bat
watch.bat backtest\configs\ut_1h_long_v1.yaml MT5:GOLD 120 8788
```

New flags on `cli watch`:

| flag | does |
|---|---|
| `--backtest` | re-run the strategy every cycle (the old behaviour) |
| `--no-gate` | log the risk read, never let it veto |
| `--max-danger N` / `--min-confidence N` | gate thresholds (default 70 / 25) |
| `--require-bias` | also require the structural bias to agree with the side |
| `--no-emit` | evaluate and display, do not append to `signals.jsonl` |
| `--max-bars N` | bars per timeframe in the payload (default 1500) |
| `--report-dir DIR` | give a second watcher its own directory — and its own `signals.jsonl`, so a test run cannot append to the real audit trail |

One-time per symbol, because the live loop reads the KEY LEVELS cache and never
builds it (a snapshot is a real recompute; `MT5:GOLD` is 81,922 H1 bars):

```
.venv\Scripts\python.exe -m backtest.cli zones --symbols XAUUSDT,BTCUSDT,ETHUSDT
```

⚠️ **One watcher at a time.** They all write `backtest/reports/` and will
overwrite each other's page. A lock file catches it; `--report-dir` is the way to
run two on purpose.

⚠️ **Restart after any code change.** Python caches modules at import, the HTML
template does not, so a watcher left running across an edit serves a new page with
old data. It detects this and shows a banner, but restarting is the only fix.

---

## 6b. The scheduled analysis journal

`tools/analysis_tick.py` + a scheduled task write `reports/analysis/YYYY-MM-DD.md`
every 30 minutes (`7,37 * * * *` — off the :00/:30 marks on purpose).

**The change test is deterministic Python, not the model.** The tool fetches
`/api/analysis`, reduces it to a small fingerprint, and compares it to the last
one. Only if something decision-relevant moved does it print the blob and let a
model analyse it.

That split is the whole design. A model asked "analyse this" 48 times a day, on a
15-minute anchor where maybe 3–5 ticks a day carry news, will **manufacture
significance to fill the format** on the other 43 — which is the failure the
mandate itself names as the worst one, and a journal of 43 padded entries is one
nobody reads on the day it matters.

The fingerprint tracks what would change a decision, not price:

```
new signal · gate pass/veto · which legs are blocking · structural bias
long/short danger BAND (not the number) · feed stale · session
ADX ranging vs trending · OI quadrant · funding verdict · timing rules apply
```

Two cases are deliberately treated as news rather than silence:

* **the watcher being unreachable** — a journal that looks quiet because nothing
  is running is the worst possible failure of a journal, so it is written down;
* **the feed going stale** — the same reason.

A quiet tick still writes one skimmable line, so a whole day reads as a timeline:

```
· 23:02 · 4675.1 · danger L25/S39 · bias BULLISH · blocking: reclaim
· 23:37 · 4671.8 · danger L28/S41 · bias BULLISH · blocking: reclaim
## 00:07 MYT — NEW SIGNAL: None -> 1787580000000; blocking legs: ['reclaim'] -> []
<the full analysis>
```

### Recording and analysis are scheduled separately, on purpose

The Claude-app scheduler turned out to be three fragile links in series: the app
must be **open**, it must be **idle** (a tick due mid-turn is skipped outright,
with no trace), and the command must match a **permission allow-rule** or the run
stalls forever waiting for an approval nobody is there to give. Two consecutive
ticks were lost to two different links before this was understood.

So the two halves now run under different schedulers:

| | runs under | needs Claude? | writes |
|---|---|---|---|
| **recorder** | Windows Task Scheduler (`GoldAnalysisTick`, every 30 min) | **no** | one heartbeat line, always |
| **analyst** | Claude scheduled task (`gold-analysis-tick`) | yes | the heading + the full write-up |

`analyse_silent.vbs` runs the recorder with **no console window** — a black box
flashing every half hour on a machine someone is watching a chart on is a reason
the task gets deleted, and a deleted task records nothing at all.

**They keep separate fingerprints** (`.state-recorder.json` /
`.state-analyst.json`), and that separation is load-bearing: one shared file
would let the recorder consume every change seconds before the analyst looked,
and the analyst would then see a permanently quiet market.

A change the analyst never wrote up still appears in the journal, flagged
`**[CHANGED: ...]**` on the recorder's line. Visibly incomplete beats silently
absent — the same reason the CHANGED heading is written before the analysis
rather than after it.

```
schtasks /query /tn "GoldAnalysisTick"      check it
schtasks /run   /tn "GoldAnalysisTick"      force one now
schtasks /delete /tn "GoldAnalysisTick" /f  remove it
```

⚠️ `watch.bat` is still the thing that must be running for either half to have
anything to read, and it keeps writing `signals.jsonl` with no model and no app
involved.

## 7. What is still not true

* **`execute` is still false and no order is ever placed.** This emits signals and
  draws them. `exec/mt5_exec.py` is unbuilt and `terminal.trade_allowed` is False.
  `OPERATING_PLAN.md` §9 steps 4–7 are unchanged by any of this.
* **The gate has never been measured.** Nobody has checked whether vetoing at
  danger > 70 improves or worsens the rule's 42.5%. It is one thing the live log
  can eventually answer — `extras.gate` records the counterfactual on every
  entry, so the vetoed signals can be scored later against what they would have
  done. Until then the gate is loose on purpose.
* **The live firing rate should be checked against the panel.** The measured
  panel says ~15.2/week raw leg-agreement and ~5.0/week entries at two slots. If
  the live logger reports far from that, the live evaluator and the harness
  disagree, and `OPERATING_PLAN.md` §10.4's bar-for-bar reconciliation has to find
  out why before anything is funded.
