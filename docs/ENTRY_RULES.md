# ENTRY_RULES.md — the trigger specification

**Status:** decided. This file is the *what and why*. Implementation is a separate job —
§8 lists exactly what the engine is missing.

**Who this is for:** the session that will change code and run backtests. Read
`ANALYSIS_BRIEF.md` first for the system, then this for the rule.

Everything below was measured on this repo's own data through this repo's own indicator
port. §6 describes the measurement so it can be reproduced or attacked.

> **PAXGUSDT is now synced** — 5m/15m/30m/1h/4h and 1m from 2022-03-15, 4.4 years.
> `docs/README.md` §2 still calls it "the unblocker and has not been synced". It has been,
> and it is used throughout this file. That note wants updating.

---

## 1. Bottom line

One trigger, symmetric long and short, on a **15m** entry timeframe (1H for thinner
instruments):

> **Enter when every timeframe's UT Dynamic Level agrees on direction, the 4H EMA stack
> agrees with it, and price has just dipped to the 15m EMA-14 and closed back through it.**

Measured pooled (long+short) win rate against a **49.8% coin-flip baseline**:

| panel | n | pooled WR | vs baseline |
|---|---|---|---|
| XAUUSDT 15m, 166 days | 110 | **68.2%** ±8.7 | +18.4 |
| PAXGUSDT 15m, 2025–26 (liquid era) | 482 | **56.6%** ±4.4 | +6.8 |
| PAXGUSDT 1h, 2025–26 (liquid era) | 237 | **57.8%** ±6.3 | +8.0 |

> **This rule survived an exhaustive attempt to improve it.** 683 conditions across seven
> feature families, 127 combinations and 12 adversarial verifications found nothing that
> adds to it — see `FEATURE_MINE.md`. Read that file **before** proposing a new entry
> filter; it also documents the scoring mistake (comparing an additive filter to the random
> null instead of to the rule's own other trades) that made three dead findings look real.

The edge replicates on an independent instrument over a period that predates XAUUSDT's
listing entirely. **Its size is 6–9 points on the large sample, not the 17 points the
166-day XAUUSDT window suggests.** Plan against the small number.

⚠️ **Break-even win rate on XAUUSDT 15m at a 2.5×ATR symmetric bracket is 62–64%.** So the
honest expectation is a system that sits *near* its own cost floor, and the levers in §5
(maker exits, right anchor timeframe) matter as much as the entry does.

---

## 2. Why "65% win rate" is the wrong target

Win rate is not a property of a strategy. It is a property of the **bracket**. Measured on
XAUUSDT 15m, every in-session bar, long, n=11,580:

| target | stop | win rate |
|---|---|---|
| 1.0×ATR | 2.0×ATR | **64.7%** |
| 1.0×ATR | 3.0×ATR | **73.2%** |
| 1.0×ATR | 4.0×ATR | **78.8%** |

A 78.8% win rate is available right now with **no rule at all** — buy at random, target
1 ATR, stop 4 ATR. Expectancy −$5.60 a trade. Any win rate can be bought by widening the
stop, and buying it always destroys the expectancy.

The number that cannot be manufactured:

```
break-even WR  =  (SL·ATR + cost) / ((TP + SL)·ATR)
```

At gold ≈ 4,600 with 5.5 bps taker per side, **cost = $5.06 round trip per unit** = 0.62 ×
the 15m ATR-14:

| anchor | ATR-14 median | cost / ATR | break-even WR at 2.5/2.5 |
|---|---|---|---|
| 5m | 3.51 | 1.44 | 78.8% — **not tradable at any skill level** |
| 15m | 6.90 | 0.73 | 64.4% |
| 30m | 10.25 | 0.49 | 59.7% |
| 1h | 15.02 | 0.34 | 56.6% |
| 4h | 30.29 | 0.17 | 53.3% |

**Optimise `achieved WR − break-even WR`, never WR.** Every judgement below uses that margin.

Note this ratio is **independent of position size** — the fee is a percentage of notional, so
trading larger does not dilute it. Only bigger moves and cheaper fills do.

### What this does to trade frequency

Ten-plus trades a day was the ask. A trade must clear ~0.11% of notional in costs, so it
needs to target ~0.4–0.6% to be worth taking. Gold moves 0.156% per 15m ATR, so a target that
size takes 40–60 minutes. Roughly ten such windows exist in a day — but each needs a >62% hit
rate, and the measured hit rate for an *unfiltered* entry is 49.8%.

**The rule fires 0.6–0.8 times a day.** That is not a limit to be tuned away; it is what
survives the cost floor. Three to four trades a week, each with a real edge. If you want more
trades, the route is **more instruments running the same rule**, not a lower bar on this one.
The rewritten `watch.py` already takes a symbol list, so that path is open.

---

## 3. The measurement standard — use this, reject anything that doesn't

Gold fell 10.1% over XAUUSDT's 166 days. Long-only results in that window are unreadable.
Instead:

> **Evaluate the rule and its mirror image together, at a symmetric bracket, and pool them.**

At a symmetric TP/SL, `P(long wins) + P(short wins) ≈ 1` on any bar, so the **pooled win rate
is 49.8% by construction whatever the market did.** Verified — every year, both instruments:

```
PAXG 2022 49.6%   2023 49.9%   2024 49.8%   2025 49.9%   2026 49.8%
XAU  2026 49.6%
```

That is a drift-free zero point. A pooled 56% is 6 points of genuine directional information
no matter which way gold went. A long-only 56% is evidence of nothing.

**Always report the two sides separately as well.** A rule whose lift lives entirely on one
side is a bet on the window. This is what disqualified the app's own setup B (§7).

---

## 4. The trigger

Anchor **15m** (XAUUSDT) or **1h** (thinner instruments — §5.2).
Session: `gold_session`, unchanged. Direction: **both**, mirrored exactly.

### 4.1 LONG

| # | leg | condition |
|---|---|---|
| 1 | **HTF trend** | 4H UT bias bullish — `close_4h > ut_level_4h` |
| 2 | | 1H UT bias bullish |
| 3 | | 30m UT bias bullish |
| 4 | | 15m UT bias bullish (own timeframe) |
| 5 | | 5m UT bias bullish — **optional, see below** |
| 6 | **HTF structure** | 4H EMA stack bullish — `ema7 > ema14 > ema28` on 4H |
| 7 | **the dip** | `low < ema_14` on any of the last **3** closed 15m bars |
| 8 | **the reclaim** | this bar closes **above** `ema_14` … |
| 9 | | … **and** above the previous bar's close |

All nine ANDed. Closed bars only, higher timeframes as-of-closed — exactly what the engine
already enforces.

### 4.2 SHORT

Mirror every leg: all UT biases bearish, 4H stack bearish, `high > ema_14` within the last 3
bars, close below `ema_14` and below the previous close.

### 4.3 Which legs are load-bearing

Ablation, pooled WR at TP2.5/SL2.5, all three panels:

| variant | XAU 15m | PAXG 15m ’25–26 | PAXG 1h ’25–26 |
|---|---|---|---|
| **full rule** | **67.0** (n112) | **56.6** (n482) | **57.8** (n237) |
| drop the 5m leg | 59.2 | 55.5 | 59.3 |
| drop 5m + 30m | 57.6 | 54.2 | 56.5 |
| 1H + 4H UT only | 53.3 | 54.3 | 56.0 |
| no 4H EMA stack | 58.0 | 55.8 | 53.5 |
| no dip/reclaim event | 57.9 | 55.4 | 57.8 |
| **dip/reclaim event alone** | **51.6** (n546) | **49.8** (n2107) | **53.4** (n569) |

Read the last row first. **The pullback pattern on its own is worth nothing** — 49.8% on
n=2107 is the coin flip to one decimal place. The edge lives in the multi-timeframe
agreement; the pullback only decides *when* to act on it. Any future proposal that keeps the
pattern and relaxes the alignment is pointed the wrong way.

**Leg 5 (5m) is optional.** Worth +7.8 points on XAUUSDT, ±1 point on both PAXG panels. Ship
it as a switch, default on, and let live data settle it. Off buys ~12% more trades.
⚠️ Later work sharpened this — §11.5. The 5m leg is **inert in the 19–22 MYT window and
load-bearing in the 06–09 MYT one** (the trades it excludes there win 41.5%). Keep it on in
both; the case for dropping it in the evening window was refuted on all three lenses.

**Legs 7–9 are the least-supported part.** +9.1 on XAUUSDT, +1.2 on PAXG 15m, 0.0 on PAXG 1h.
Keep them: they cost nothing on any panel, they halve the trade count, and they put the fill
at a measurably better price — which is what makes §5.1 possible. But do not defend them if a
later test kills them.

**Deliberately absent:** ADX, RSI, Bollinger, relative volume, structure state, session hour.
All screened, none survived (§7).

---

## 5. The exit

### Option A — symmetric bracket

```
take_profit: 2.5 × ATR-14 (entry bar, frozen)
stop_loss:   2.5 × ATR-14 (entry bar, frozen)
time_stop:   none
```

### Option B — partial + breakeven + trail  ← recommended

```
stop_loss:  2.5 × ATR-14                     frozen at entry
TP1:        2.5 × ATR-14, closes 50%         then stop -> breakeven
runner:     chandelier trail 2.5 × ATR-14 below the running peak,
            armed only after TP1 fills
```

B beat A on **profit factor on every panel**:

| panel | A: PF / WR | B: PF / WR | B best trade |
|---|---|---|---|
| XAU 15m 166d | 1.66 / 68.2% | **1.85 / 67.0%** | 2.7 R |
| PAXG 15m ’25–26 | 0.82 / 56.6% | **1.07 / 54.9%** | 10.9 R |
| PAXG 1h ’25–26 | 1.02 / 57.8% | **1.29 / 57.1%** | 7.5 R |

This is the shape that was asked for. The win rate stays in the 55–67% band because TP1 is a
high-probability level and the stop is at breakeven from then on, **and** the runner is
uncapped — 10.9 R on one PAXG trade is exactly the "one-way move, one position, held a long
time" case. Splitting the exit costs nothing: fees are charged on notional, so two half-exits
cost the same as one full exit plus a tick.

A variant worth running second: **`SL 3.0 / TP1 2.0 / trail 2.5`** — win rate **73.5%** on
XAUUSDT and **63.7%** on PAXG 1h, at slightly lower profit factor. If the headline number
matters more than the last 10% of expectancy, that is the one.

Note also that arming the trail only after TP1 is the same fix `ANALYSIS_BRIEF.md` §7.2 found
for `arm_on_recross`, applied one level up. An unarmed trail on a pullback entry fires
immediately; that trap has been paid for once already.

### 5.1 The single biggest lever: stop paying taker on the exit

Break-even win rate at 11 bps round trip vs 7.5 bps (taker in, **maker out**):

| panel / bracket | @ 11 bps | @ 7.5 bps | achieved |
|---|---|---|---|
| XAU 15m 2.5/2.5 | 64.4% | **59.8%** | 67.0% |
| XAU 15m 3.0/2.5 | 58.5% | **54.4%** | 61.9% |
| PAXG 1h 3.0/3.0 | 57.5% | **55.1%** | 58.5% |

A take-profit **is** a resting limit order. There is no reason to ever pay taker on it. That
is 3–4 points of break-even win rate for free, and it roughly doubles the margin over
break-even — a larger effect than anything else in this document. Do it before tuning
anything else.

The entry is harder: the reclaim close is a momentum event. The same logic points at a
resting limit at `ema_14` once legs 1–6 are true, accepting non-fills — but the fills you
miss are the ones that ran, so that has to be *measured*, not assumed.

### 5.2 Choosing the anchor timeframe

15m is right for XAUUSDT because its ATR% is 0.156%, high enough that 2.5 ATR clears the cost
floor. PAXGUSDT's ATR% is 0.102%; at 15m its break-even win rate is 70.4%, which the rule does
not reach, and on the 1h anchor it falls to 57.5%, which it does. **The anchor is a function
of the instrument's ATR%, not a preference.**

> Rule of thumb: use the smallest timeframe where `cost / ATR-14 ≤ 0.5`.

---

## 6. What was measured, and how

1. For every bar of the anchor timeframe: entry at the **next bar's open**, then the forward
   path walked at 5m resolution (XAUUSDT cross-checked at 1m) to find which of TP/SL is
   touched first, over a grid of 9 targets × 6 stops in ATR units. Ties resolve as **losses**
   — measured tie rate 0.11% on XAU, 0.46% on PAXG, immaterial either way.
2. **Non-overlapping sequential simulation.** A candidate is skipped while a position is open.
   Bar-level screens count the same trend five times over and their confidence intervals lie;
   the first version of this study reported ±2.1 on a number whose honest interval was ±8.
3. Costs are **proportional** — 11 bps of entry notional + 2 ticks, not a fixed $5.06. PAXG
   traded 1,780–4,540 across its history and a fixed figure is wrong at both ends.
4. Long and short measured separately, then pooled (§3).

### What holds up

**Parameter plateau** (XAUUSDT, dip lookback × reclaim EMA, TP3/SL3): win rate 58.7–67.0%
across `look ∈ {2,3,4,5,6,8}` × `ema ∈ {7,14}` — **twelve of twelve cells net positive**. A
broad plateau, not a spike. Contrast `ANALYSIS_BRIEF.md` §7.3, where all twelve trail-sweep
cells *lost*: that is what a dead surface looks like, and this is not one.

**Bracket plateau** (XAUUSDT, look 3 / EMA-14): every cell of TP ∈ {2, 2.5, 3} × SL ∈
{2, 2.5, 3} is net positive — WR 59.8–73.9%, PF 1.26–1.64.

**Symmetry.** Long and short legs track each other on all three panels (XAU 70.5/64.7,
PAXG 15m 60.1/50.3, PAXG 1h 58.2/57.1). A window artefact would not do that.

### What weakens it — read this before sizing anything

* **Second-half fade.** Splitting XAUUSDT's 166 days: first 83 days 65.2% WR / PF 1.50,
  second 83 days 57.6% WR / PF 0.90. The win rate held; the expectancy did not.
* **One outlier month.** March 2026 contributed +403 of the +556 total. Ex-March, XAUUSDT
  expectancy is +$1.65 a trade over 93 trades, not +$5.06.
* **PAXG's full 4.4 years are flat — pooled 49.2% over n=1349.** By era: 2022 39.9%,
  2023 45.7%, 2024 48.7%, 2025 55.8%, 2026 58.6%. Two explanations fit that ramp and this
  data cannot separate them:

  1. **Liquidity.** PAXG turnover per 15m bar: $2,969 (2022) → $170,702 (2026); XAUUSDT is
     $634,199. Gating the *whole* history on turnover ≥ $50k lifts the pooled win rate from
     49.2% to **54.7%**, and it is monotone in the threshold. Indicators computed on a
     $3k/15-minute book are noise with a chart drawn on it.
  2. **Regime.** It is a trend-continuation rule. 2022–23 gold was range-bound and the rule
     was actively *wrong* — 39.9% is meaningfully **below** the coin flip, which is the
     classic trend-follower-in-a-range signature, not randomness.

  Both readings say the same operational thing: **this rule needs a liquid, trending market
  and it will bleed in a chop.**

* **No regime gate was found.** ADX (own TF, 1H, 4H, at 20 and 25), 4H EMA separation in ATR
  (0.5 / 1.0 / 1.5), ATR percentile, a rolling-median ATR% filter, macro trend (close vs close
  1/3/5/10/20/30 days back) and a Kaufman efficiency ratio over 1/3/10 days were all tested.
  **None separated the good years from the bad.** Finding one is the highest-value open
  problem in this project.

---

## 7. Screened and rejected — do not re-propose without new evidence

Pooled long+short, both instruments where data allowed. Lift over the 49.8% baseline in
brackets.

| idea | result |
|---|---|
| `ut_cross_up` / `ut_cross_down` as the trigger (app setup A) | 39.0% (**−0.7**) on 15m. Below baseline — the flip is late by construction. |
| `ut_bias + ut_level_near + ema_stack` (app setup B) | 43.2% at 3:2 (+3.5) — but long 36.3 / short 49.9. The lift is the downtrend, not an edge. Fails §3. |
| ADX ≥ 20 or ≥ 25, own timeframe or 1H | +0.1 / +0.1. Nothing. Also nothing as a regime gate. |
| RSI > 50; RSI 45–60 | +1.8 / +0.9. Inside the noise. |
| Relative volume ≥ 1.2 | +0.4. Nothing. |
| Volatility band low / normal / high | +0.1 / −0.2 / +0.1. Nothing. |
| Market structure BULLISH/BEARISH, 15m or 1H | +0.4 / −1.0. Nothing. |
| Session and hour-of-day buckets | −0.5 to +0.1. Nothing. |
| Price extended > 1.5 ATR from EMA-28 | +2.2 on XAU only; absent on PAXG. |
| 4H UT bias **alone** | **−2.1.** Worse than nothing on its own. |
| Kaufman efficiency ratio as a chop filter | +1.2 to +3.2 on PAXG; does not fix 2022–23. |
| Turnover / liquidity gate | +5.5 on PAXG full history. **This one is real** — but every XAUUSDT bar already clears it, so it is an *instrument-selection* rule, not a bar filter. |

Still true from `ANALYSIS_BRIEF.md` §7 and unchanged: `close > ema_7 AND ema_14 AND ema_28`
as a pullback filter is self-contradictory, and a naive `ltf_ema_break` trail fires at the
entry bar.

---

## 8. What the engine needs before any of this can run

Nothing in §4 is expressible in a config today. Smallest first.

**8.1 Signals — one generalisation covers legs 1–6.**
`htf_1h_ut_*` and `htf_4h_ut_*` are hard-coded per timeframe (`engine/signals.py:615-649`).
Generalise `_htf_col` into a registered family so any timeframe in `features.timeframes` gets
`tf_<interval>_ut_bullish` / `_bearish` and `tf_<interval>_ema_stack_bullish` / `_bearish`.

Name them `tf_`, not `htf_`: 5m is *below* a 15m anchor. `build_htf_alignment` already handles
that correctly — the last closed 5m bar ends at the same instant as the 15m bar, so there is
no look-ahead — but calling it "higher timeframe" would be a lie in the config.

**8.2 A windowed event signal for legs 7–9.** `expr:` sees only the current bar
(`engine/signals.py:674-686`), so the dip cannot be written as an expression. Add a
parameterised signal:

```yaml
- signal: pullback_reclaim_long
  params: { ema: ema_14, lookback: 3, require_higher_close: true }
```

defined as `any(low[t-k] < ema[t-k] for k in 0..lookback-1) AND close[t] > ema[t] AND
close[t] > close[t-1]`, mirrored for short. If `Condition` cannot carry params without schema
churn, register the handful of useful combinations by name instead
(`pullback_reclaim_ema14_3_long` …). Less elegant, no schema change, same result.

**8.3 Both directions in one run.** `entry.side` is a single `Literal["long","short"]`
(`engine/config.py:96`). The rule is symmetric and §3 *requires* both legs measured together;
running two configs and adding them by hand invites exactly the mistake §3 exists to prevent.
Add `side: both` with mirrored trees, or a `short:` block alongside `entry:`.

**8.4 Partial exit + breakeven + chandelier** for Option B:

```yaml
exit:
  stop_loss: { mode: atr_mult, value: 2.5 }
  partial:   { at: { mode: atr_mult, value: 2.5 }, fraction: 0.5, then_stop: breakeven }
  trailing:  { mode: chandelier, value: 2.5, arm_after: partial }
```

`trailing.mode` already accepts `ut_level` and `atr_mult` in the schema and the engine raises
on both (`engine/backtester.py:197-205`). `chandelier` — peak minus k·ATR — is the one that
was measured. Keep the existing pessimistic intrabar ordering: the stop is tested against a
sub-bar's high/low **before** any target on that sub-bar.

**8.5 Put break-even WR on the report.** Add pooled WR, per-side WR, and
`(SL·ATR + cost)/((TP+SL)·ATR)` at the run's median ATR and price to the summary panel.
A win rate without its break-even next to it is unreadable — that is the whole of §2 in one
line of UI.

**8.6 Proportional costs and a maker fee.** Confirm the fill model charges fees on entry
*price*, not a frozen dollar figure, and add `maker_fee_bps` so §5.1 can be measured instead
of argued.

---

## 9. Running it on `watch.bat`

The blocking-leg panel becomes a live checklist, which is the point:

```
entry not met — blocking leg(s) below
  ✓ tf_4h_ut_bullish
  ✓ tf_1h_ut_bullish
  ✓ tf_30m_ut_bullish
  ✓ tf_15m_ut_bullish
  ✗ tf_5m_ut_bullish
  ✓ tf_4h_ema_stack_bullish
  ✗ pullback_reclaim_long
```

```bash
backtest\watch.bat backtest\configs\mtf_confluence_pullback.yaml 60 8787
```

60 s on the slow loop, not 300 s: the trigger is a 15m close and the panel should be current
within a bar. The fast loop already ticks at 5 s for the chart.

**Order the legs slowest timeframe first** — 4H → 1H → 30m → 15m → 5m → event. The list then
reads as a countdown: the top rows change rarely, and when all six are ✓ the setup is
*arming*. That is when to be at the screen. A 4H UT bias holds for days, so this is also how
the "one trade that runs a long way" case announces itself hours ahead.

---

## 10. Before it is trusted with money

1. **Measure the pooled win rate live against 49.8%** — not P/L. P/L needs hundreds of trades
   to say anything. At 0.7 trades/day, 60 trades is about three months, and at n=60 the 95%
   interval on a win rate is ±13 points. Three months can only detect a *large* break.
2. **Kill criteria, decided now.** Stop if the pooled win rate over the last 50 closed trades
   falls below **52%** — break-even minus a full 10-point margin, at which point the edge is
   gone whatever the P/L says — or if either side alone drops below 45% over 30 trades.
3. **Assume the small number: 56.6%, not 67.0%.** Size so the drawdown implied by a 56% win
   rate at 1:1 — roughly 8 consecutive losses in 60 trades — is survivable and boring.
4. **The open problem is the regime gate** (§6). Until something reliably separates 2022–23
   behaviour from 2025–26 behaviour, this rule is a bet that gold keeps trending. Say that out
   loud now rather than discovering it in a range.

---

## 11. Trading it inside a restricted window — 06:00–09:00 and 19:00–22:00 MYT

The rule was re-measured under the constraint *"new positions may only be opened in these
two windows"*. Exits stay unconstrained. **The rule does not change.** What changes is which
window you prefer, what you must not do to buy back frequency, and one fill detail.

A second full mine ran inside the windows — 779 conditions, six archetypes, six adversarial
verifications — and produced **no new rule**. See `FEATURE_MINE.md` §8. Everything below is
what did survive.

### 11.1 The two windows are not the same market

MYT = UTC+8, so **A = 06:00–09:00 MYT = 22:00–01:00 UTC** (NY close into the Asia open) and
**B = 19:00–22:00 MYT = 11:00–14:00 UTC** (London PM into the NY open — the NY open itself,
13:30 UTC, falls *inside* window B).

Measured on XAUUSDT 15m, in-session bars:

| window | % of session | ATR% (median) | turnover / bar | relative volume |
|---|---|---|---|---|
| A 06–09 MYT | 12.3% | **0.1430** | $370,266 | 0.93 |
| B 19–22 MYT | 12.4% | **0.1922** | **$807,581** | **1.19** |
| rest of day | 75.3% | 0.1883 | $410,819 | 0.70 |

B is the highest-participation three hours of the gold day. A is the *lowest-volatility*
block of the three — lower than the rest of the day, not just lower than B.

### 11.2 Prefer window B, and the reason is arithmetic, not a pattern

Cost is a fixed 0.11% of notional, so a lower ATR% raises the break-even win rate. At the
2.5/2.5 ATR bracket on XAUUSDT:

| window | break-even WR | base rule WR | edge | n |
|---|---|---|---|---|
| A | **65.7%** | 69.7% | +4.0 | 33 |
| B | **60.8%** | 78.3% | **+17.5** | 23 |
| A+B | 64.3% | 72.7% | +8.4 | 55 |

**Window B starts five points ahead of window A before any rule is applied.** This is not a
discovered edge and carries no multiple-testing cost — it is the volatility difference
divided into a fixed fee. It is the most reliable statement in this section.

⚠️ `be` must be computed from the ATR of the **trades actually taken**, not the panel median.
Selecting hours changes the volatility; the panel-level figure understated window A's
break-even by ~4 points. Fixed in the study tooling; make sure the engine does the same when
§8.5 is built.

### 11.3 The constraint does not hurt the rule

| panel | edge, all day | edge, A+B |
|---|---|---|
| XAUUSDT 15m | +6.5 (n=110) | **+8.4** (n=55) |
| PAXGUSDT 15m ’25–26 | −9.1 (n=482) | −9.3 (n=244) |
| PAXGUSDT 1h ’25–26 | −0.3 (n=237) | **+4.2** (n=104) |
| PAXGUSDT 15m ’22–24 (hostile) | −23.9 | **−28.7** |

On the 1h anchor the constraint takes the rule from break-even to +4.2. The hostile panel
gets *worse* inside the windows, which is the correct tripwire direction. Trading only these
six hours is a free choice, not a sacrifice.

### 11.4 Do not relax the rule to buy back frequency

The constraint costs ~60% of the trade count (XAUUSDT 0.66/day → **0.33/day**, about two
trades a week). The obvious response is to loosen a leg. It was tested exhaustively inside
window B, scored against base-in-B, and the answer is a clean number:

| relaxation | trade-count multiple | the **marginal** trades win | cost to overall WR |
|---|---|---|---|
| 5-of-5 → **4-of-5** timeframes | 2.12× | **50.0%** (n=184) | −3.4 to −3.9 pts |
| 5-of-5 → **3-of-5** | 2.97× | 49.0% (n=292) | −5.5 pts on both panels |
| drop the dip/reclaim event | 1.81× | 53.8% (n=173) | −2.2 to −6.4 pts |
| loosen dip lookback 3 → 8 | 1.38× | 51.9% (n=81) | monotone cost at every step |

**The trades a relaxation buys win at 50% — the random null — against a break-even of 66.7%.**
Doubling the trade count does not dilute the edge, it adds trades with literally zero
directional information. Every relaxation tested failed, and every one is *more* expensive
in window A than in window B (1.4× to 3.4×), which is what the break-even gap predicts.

The `n_bull` response is cleanly monotone in window B — 50.4 / 52.0 / 55.9 at ≥3 / ≥4 / ≥5
timeframes — so the fifth timeframe is worth about 4 points and there is no knee to exploit.

### 11.5 Which legs are load-bearing, measured

Ranked by what the trades each leg *excludes* would have done:

1. **30m UT agreement — the most important leg.** The trades it excludes win 47.7% (PAXG
   15m) and 46.7% (PAXG 1h) inside window B — *below* the 49.8% null, i.e. actively adverse.
   Not droppable in either window.
2. **1h UT agreement.** Dropping it costs 10.4–11.5 points. Not droppable.
3. **5m UT agreement — window-dependent.** Inert in window B (marginal trades 55.8% against
   base-in-B 55.9%, z = −0.01) but load-bearing in window A (marginal 41.5%, z = −2.01).
   Same sign on all six panel/window cells. This is the one genuine A-vs-B difference found,
   and it is still only Stouffer z ≈ 1.9 — **not** significant, and dropping the leg in B was
   refuted on all three adversarial lenses. Keep it in both windows.

### 11.6 The fill must land inside the window

The engine signals at a bar's close and fills at the **next bar's open**. A 15m signal at
21:45 MYT therefore opens the position at **22:00 MYT — outside window B.** Measured on
XAUUSDT window B, 5 of 54 base signals fill at 22:00; on PAXGUSDT 1h it is ~15%, and for
some of the filtered variants it reached 36%.

Decide which the constraint means and implement it explicitly:

* **signal-in-window** (current behaviour) — XAU window B n=23, 78.3%, edge +17.5
* **fill-in-window** (strict; drop signals on the window's last bar) — n=21, 76.2%, edge +15.4

The difference is small on the base rule and does not change any conclusion here, but it was
large enough to overturn one candidate finding, so the engine should not leave it implicit.
The strict form is one line: require the window mask to be true on bar *t+1*, not bar *t*.

### 11.7 Exits — same direction in both windows

TP 3.0 / SL 2.5 beat TP 2.0–2.5 on mean R in **every** liquid panel-window combination, in
both windows, and the curve keeps improving out to TP 4.0–6.0. That is not a discovered
holding period — it is the fixed 0.11% cost being amortised over a bigger bracket, exactly
as §2 predicts. Read it as *"stop cutting winners at 1.5–2.0 ATR"*, not as a calibrated
number, and do not add a window-conditional exit: none was found.

### 11.8 What this means in practice

* Run **one rule**, unchanged, in both windows. The archetype question was settled
  decisively: 362 tests looking for a different archetype in window A (mean reversion,
  Bollinger and VWAP fades, range state, directional bias, hour splits) and 62 tests on
  breakout / fresh-UT-cross / opening-range archetypes in window B returned **zero**
  survivors between them. Trend continuation off the 15m EMA-14 reclaim is right in both.
* **Weight window B.** If you can only watch one, watch 19:00–22:00 MYT.
* Expect **~2 trades a week**, and accept it. §10's kill criteria now take about six months
  of calendar time to reach 50 trades — plan the review cadence around that, not around P/L.
