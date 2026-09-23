# FEATURE_MINE.md — the exhaustive entry-feature search, and what it returned

**Result: nothing survived.** 683 conditions across 7 feature families, 127 combinations,
12 independent adversarial verifications. Three finalists reached the verification stage;
**eleven of twelve verdicts refuted them.** The base rule in `ENTRY_RULES.md` is still the
only thing standing, and it is unchanged by this work.

That is a real answer, not a failed run. This file exists so the next session does not
spend another 1.8M tokens rediscovering it — and because the *method* the search
established is more valuable than any of the conditions it rejected.

---

## 1. What was searched

A per-bar feature matrix was built for six panels — XAUUSDT 15m/30m/1h (166 days),
PAXGUSDT 15m/30m/1h (4.4 years), BTCUSDT 15m, ETHUSDT 15m — with **496 columns each**:
every column of the app's indicator port on all five timeframes as-of-closed, plus new
derived families:

| new family | columns |
|---|---|
| UT flip timing | `age_5m … age_4h` (minutes since that timeframe's UT bias flipped), `lag_5m_15m`, `lag_15m_1h`, `lag_5m_4h`, `age_min_all`, `age_max_all`, `n_bull`, `n_bear`, `cascade_fast_first` |
| divergence | `div_{macd,rsi}_{reg_bear,reg_bull,hid_bear,hid_bull}` per timeframe — regular and **hidden**, lit only from the bar the second pivot is *confirmed* |
| volume | `vol_pctile_100`, `turnover`, alongside the port's `relative_volume` |
| geometry | `dip_depth_atr_5`, `pop_height_atr_5`, `low/high_vs_ema14_atr`, `close_vs_ut_atr`, `close_vs_vwap_atr`, `body_ratio`, `close_pos`, `rsi_min_5`, `rsi_max_5` |
| slopes | `adx_slope_5`, `rsi_slope_5`, `macd_hist_slope_3`, `sep_slope_10` |

Outcome labels are first-touch over a 9 × 4 TP/SL grid in ATR units, resolved by walking
the forward path at 1-minute resolution (5-minute for PAXG). Ties resolve as losses.

Causality is **machine-checked**, not asserted: `build.py --check-causal SYMBOL TF`
truncates the input at four cut points, recomputes every derived column and requires the
earlier values to be bit-identical. All three spot-checks are clean. An adversarial pass
also swept the 4H mask ±3 bars: *peeking into the future does not improve any finding*,
which is the opposite of a leakage signature.

---

## 2. The three finalists, and how each died

### `divvol` — 4H hidden MACD divergence AND 4H relative volume ≥ 1.0

The best-looking number produced all session: **XAU 86.5%** (n=37) against a base of 68.2%
and a break-even of 64.4%, PF 6.23. It replicated: PAXG 15m 66.7% (n=144), PAXG 1h 67.1%
(n=76). It survived the *time-stability* lens outright — positive in 5 of 5 PAXG years,
7 of 7 quarters, both XAU halves; excluding March 2026 made the lift **grow** to +20.5.

It died on the 2×2. Split into exclusive cells with the base rule underneath:

| cell | XAU 15m | PAXG 15m ’25–26 | PAXG 1h ’25–26 |
|---|---|---|---|
| base (all four cells) | 68.2 (n=110) | 56.6 (n=482) | 57.8 (n=237) |
| **both legs — the finding** | **86.5** (n=37) | **66.7** (n=144) | **67.1** (n=76) |
| divergence only | 57.7 (n=26) | 51.5 (n=101) | 50.8 (n=59) |
| volume only | 56.4 (n=39) | 52.7 (n=146) | 54.7 (n=75) |
| neither | 68.0 (n=25) | 55.0 (n=151) | 57.3 (n=82) |

**Every single-leg cell is worse than having neither leg.** There are no main effects at
all: the additive prediction for the corner is 45.4% against 86.5% observed on XAU — a
41-point pure interaction on n=37. Genuinely independent mechanisms produce main effects.
A 683-test search that lands on one lucky quadrant of a 4-cell partition produces exactly
this. (The finding's own "divergence alone 73.6 / volume alone 72.0" were *inclusive*
marginals that already contained the corner.)

Three further kills, each sufficient on its own:

* **Wrong comparator.** z = 4.46 / 4.05 / 3.02 was measured against the 49.8% random null.
  The rule's claim is "additive on base", so the comparator must be the base rule's *own
  other trades*. Two-proportion z against that: **2.82 / 2.69 / 2.14** (p = 0.005 / 0.007 /
  0.032). Against 683 tests, Bonferroni needs p < 7e-5.
* **Best-of-K.** An empirical null of 6,000 randomly-generated base-conditioned rules drawn
  from the panels' own columns reaches this triple-panel result **21.9%** of the time at
  K = 683. The pool contains a dice-rolled rule that replicates on all three panels *better*
  than the finding.
* **Discrete-parameter spike.** The numeric threshold is a genuine plateau (rel-vol 0.5 →
  1.5 stays 76–86% on XAU) — but the free parameter that matters is *which timeframe*, and
  there it is a knife edge: **1 of 16 timeframe pairings works.** Moving relative-volume off
  the 4H collapses PAXG straight back to base (66.7 → 56.0–57.6).

Also: 4H `relative_volume ≥ 1.0` is substantially a **time-of-day proxy** (P runs 0.23 at
00–03 UTC to 0.66 at 16–19 UTC), and session buckets are already on the worthless list.
And the 37 XAU trades come from only **14 distinct divergence episodes**, 3 of which carry
54% of the P/L — so the effective sample is a fifth of what n suggests.

### `flipV` — 5m UT flipped on the entry bar AND the reclaim bar is the dip bar

XAU 77.8% (n=27), PAXG 15m 67.0% (n=88). Look-ahead **clean** (truncation at four cut
points, zero differences). Leave-one-month-out robust.

Killed by the same comparator error: measured against the base trades it actually rejects,
the incremental z is **1.00 on XAU and 2.02 on PAXG** — inside the noise of the search. And
the second leg is conditionally uninformative given the first: within `age_5m == 0`, the
V-reclaim filter picks the **worse** half on the live instrument (77.8 n=27 versus 88.9
n=18 for the trades it discards).

### `flipsep_room` — 5m flip now + 15m flipped ≥ 90 min earlier + small 4H room

XAU 86.2% (n=29). Two of its three legs are **inert**. Perturbing `age_15m ≥ 90` by ±30%
changes nothing at all — identical n, identical win rate at every value — because 83–88% of
base bars with `age_5m == 0` already satisfy it. The room leg's kept and discarded trades
win at 70.1% and 70.4% respectively (Fisher p = 1.0000). The entire headline lift is three
trades removed from thirty-two, two of which happened to be losses. At 0.17 trades/day it
would take **294 days to accumulate 50 trades**.

---

## 3. Direct answers to the three questions that started this

**"How much time separates the 5m and 15m UT flip — is some separation a high-probability
buy?"** No. On base-rule bars the 5m almost never *leads* the 15m: the median is the 5m
flipping ~195 minutes **after** the 15m, and an ordered fast-first cascade occurs on
**0.0%** of base bars (`cascade_fast_first` is a dead column — delete it). `lag_5m_15m` as a
raw feature is U-shaped on XAU and exactly flat on PAXG. The only live form is `age_5m == 0`
— the 5m flipped on the entry bar itself — and that is a point mass, not a slope: loosening
it to ≤5 / ≤10 / ≤30 / ≤90 minutes leaves the win rate flat at base level (67.1 / 66.0 /
67.3 on XAU against a base of 68.2). Incrementally versus base it is z ≈ 1.0.

**"Volume high?"** No. 4H relative volume alone is *below* base on both PAXG panels
(52.7 vs 56.6; 54.7 vs 57.8). A flat `relative_volume ≥ 1.2` threshold was already known to
be worth +0.4 points; the newer forms — volume percentile, 5m volume at the anchor signal,
dip-versus-reclaim volume ratio — add nothing. Absolute *turnover* remains real but it is an
**instrument-selection** rule, not a bar filter (every XAUUSDT bar already clears it).

**"MACD top divergence?"** No — and note the direction: a regular *top* divergence is a
reversal signal, so it argues **against** a long. Tested all four kinds on all five
timeframes, as a trigger, as an additive filter and as a veto. Regular divergence is dead in
every form; using it as a veto is if anything mildly harmful (removing base trades that
carry a counter-divergence *lowers* the win rate). Hidden divergence only "works" inside the
`divvol` corner, and its own cell is below base. MACD is also not in the Android app, so
anything built on it sits outside the confirmed indicator parity.

---

## 4. The methodological lesson — this is the part worth keeping

Every one of the three finalists collapsed for the **same reason**, and it is a reason that
would have been invisible without the adversarial pass:

> **A filter added to a rule must be scored against that rule's own other trades, never
> against the random null.**

The 49.8% pooled null (`ENTRY_RULES.md` §3) is the right zero for asking *"does this
condition contain directional information?"* It is the wrong zero for asking *"does adding
this condition improve my rule?"*, because the base rule is already at 68%. Scoring an
additive filter against 49.8 credits it with the base rule's entire edge. Correcting the
comparator took z from 4.46 to 2.82, from 3.24 to 1.00, and from 3.92 to inert.

Three companion checks, all cheap, all of which caught something here:

1. **Print the full 2×2, not the marginals.** If each leg alone is below base, the
   combination is a selected corner and not a mechanism.
2. **Perturb every threshold ±30%.** If nothing changes, the leg is inert rather than
   robust — `flipsep_room` looked like a plateau and was actually a no-op.
3. **Count K, then build the best-of-K null empirically.** Random rules drawn from the same
   columns beat the finding 21.9% of the time. A p-value that ignores the search is fiction.

And one that did not fire but should stay in the checklist: **collapse trades into
episodes.** A signal that stays true for 20 bars turns 37 trades into 14 independent bets.

---

## 5. Everything tested and rejected

Beyond §3, all null on cross-panel replication. Do not re-propose in the same form.

**Flip timing (76 tests):** `age_15m/30m/1h` thresholds; `age_4h ≥ 1440` alone (+3.6 XAU,
+0.0 PAXG); "fresh cascade" (all ages young); "stale alignment"; `age_min_all` thresholds;
`age_15m == 0`; anchor-timeframe `ut_flip_age_min == 0`; `4h_stack_flip_age_min` (the two
panels rank it in **opposite** order); every 1h-anchor analogue of the family.

**Divergence (98 tests):** regular divergence as trigger, filter and veto on every
timeframe; the strict "no counter-divergence anywhere" veto is the *worst* cell in the
family; RSI divergence mirrors MACD's null.

**Volume (52 tests):** volume percentile; 5m volume at the signal; dip-versus-reclaim volume
ratio; volume-dead as a veto; turnover within a single year (which is what separates
liquidity from a recency proxy).

**Pullback geometry (39 tests):** the reclaim bar's own quality is worth **nothing** —
`body_ratio`, `close_pos`, body size in ATR, closing above the dip bar's high are all flat,
and PAXG mildly prefers the *weaker* bar. "Shallow dip beats deep dip" has the right sign on
both 15m panels but never exceeds ~2.6 points and inverts at 1h. **The whole family is
worthless standalone** (gap < 0.3 gives 51.3 on XAU and 50.6 on PAXG against the 49.8 null)
— it carries no direction without the base rule's timeframe agreement.

**Volatility / regime:** nothing separates PAXG's good years from its bad ones. The open
problem in `ENTRY_RULES.md` §6 is still open.

**Near-miss worth revisiting if more gold data arrives:** dip depth measured against the
**UT level** rather than the EMA — the only feature in the geometry family with a clean
monotone bin response on both 15m panels. Dead today on lift size (+5.3 XAU, +1.8 PAXG) and
on a wrong-signed hostile-panel result.

---

## 6. Re-running it

```
MINE = %TEMP%\claude\...\scratchpad\mine     (BRIEF.md carries the full tool contract)
```

```bash
C:\inetpub\Claude\ITSupport\backtest\.venv\Scripts\python.exe MINE\build.py XAUUSDT 15m 1m 2880
```

```bash
C:\inetpub\Claude\ITSupport\backtest\.venv\Scripts\python.exe MINE\build.py --check-causal XAUUSDT 15m
```

```bash
C:\inetpub\Claude\ITSupport\backtest\.venv\Scripts\python.exe MINE\screen.py bins XAUUSDT/15m age_5m --base
```

`screen.py` gives `Panel` / `simulate` / `screen` / `bins`; `exits.py` gives the two-stage
exit (`ENTRY_RULES.md` §5 option B) so a candidate can be re-measured under the exit it
would actually be traded with. `results.json` holds all 20 agent returns from this pass.

**The scratchpad is not durable** — it was cleared once mid-session already. If this
tooling is worth keeping, move it under `backtest/tools/`.

---

## 7. What this does and does not change

It does **not** change `ENTRY_RULES.md`. The base rule stands exactly as specified, with the
same modest and honestly-stated edge: +17 points over the null on 166 days of XAUUSDT, +6.8
on the large PAXG sample, break-even at 62–64%.

It **removes** the hope that a better filter is sitting in this indicator set waiting to be
found. Seven families, 683 conditions and 127 combinations say it is not. The remaining
levers are the ones `ENTRY_RULES.md` already names and they are unglamorous: **exit as a
maker order** (worth 3–4 points of break-even, more than any entry filter found here), pick
the anchor timeframe from the instrument's ATR%, and get more gold data.

One transfer result worth carrying: the base rule scores **52.7% on BTCUSDT and 50.0% on
ETHUSDT** — the null — over the same 166 days where it scores 68.2% on gold. So it does not
generalise to crypto, and running it there to raise the trade count would simply add coin
flips at negative expectancy. Breadth is not the answer to the frequency problem; the two
gold instruments are.

---

## 8. Second pass — the same search restricted to two trading windows

**Result: nothing survived again.** 779 conditions across six archetypes, 315 combinations,
6 adversarial verifications, **all six refuted at high confidence**. The operating
conclusions that *did* survive are in `ENTRY_RULES.md` §11; this section records what died so
it is not re-mined.

The question was different enough to be worth asking: entries restricted to **06:00–09:00
and 19:00–22:00 MYT**. The window is *exogenous* — it is the trader's availability — so
unlike a discovered time filter it carries no multiple-testing cost, and the two windows are
genuinely different markets (window B has 2.2× window A's turnover and 0.192% vs 0.143% ATR).
Both facts made a per-window archetype plausible. It is not.

### 8.1 The two finalists and how they died

**`B_VOL` — base rule + 4H volume percentile > 0.5, window B only.** Reported +15.0 on PAXG
15m, +35.8 on PAXG 1h, +21.7 on XAU.

* **The quintile response contradicts its own mechanism.** "High participation carries the
  pullback" predicts the top bin is best. Measured inside window B on PAXG 15m: bottom bin
  50.0% (n=28), middle **67.9%** (n=53), **top 51.3%** (n=39) — the top volume quintile is
  4.6 points *below* base. The `>0.5` threshold works only by pooling the one hot bin with
  the cold top bin. On PAXG 1h the same three bins run 40.0 / 63.2 / **92.9** — monotone the
  other way. **The two panels disagree about the shape**, on the same instrument over the
  same period.
* **A one-hour boundary shift destroys it, in opposite directions on the two panels.** PAXG
  15m loses 74% of the effect shifting +1h; PAXG 1h loses 64% shifting −1h. Each panel
  collapses on the shift the other survives.
* **It lives in single hours, and the panels pick different ones.** PAXG 15m by MYT hour:
  19 +27.4, **20 −12.1 (wrong sign)**, 21 +15.1. PAXG 1h: 20 +42.3, 21 +33.7. Hour 20 carries
  the 1h result and runs backwards on the 15m one.
* **Best-of-K.** 2,500 random base-conditioned single-leg rules restricted to window B: the
  finding's best z of 2.60 sits *at or below the median* of the best-of-K null at K≈1,100.
* **Unfalsifiable where it matters.** On XAUUSDT window B the leg deletes 2 of 23 trades — the
  MISS cell never reaches n=8 at any threshold in a ±30% sweep. It cannot be measured on the
  live instrument in the window where it would be traded.

**`B_5M` — drop the 5m UT-agreement leg in window B.** Reported as "free frequency".

* It is not free, it is **zero**: marginal trades 55.8% against base-in-B 55.9%, z = −0.01.
* On the live instrument the sign is **opposite** — XAU window B 78.3% → 66.7%, edge +17.5 →
  +5.7. It costs 11.6 points where it would actually be traded.
* Its profit factor is **below 1 in every subsample without exception** (0.30–0.97). "Free"
  was a win-rate-only statement; on R it is a small consistent bleed everywhere.
* One reported 2×2 **did not reproduce** — the claimed exclusive cells (n=79 and n=26) exceed
  the entire marginal cell (n=43), so they were not exclusive cells at all.

### 8.2 The archetype question, answered in the negative

* **362 tests** looking for a different archetype in window A — mean reversion, Bollinger and
  VWAP fades, extension fades, reversion targets, range state, directional asymmetry, hour
  splits — returned **zero** survivors.
* **62 tests** on breakout / fresh-trend archetypes in window B — `ut_cross` standalone and
  gated (re-tested because a UT cross at the NY open is not the same event as one at 03:00
  UTC), and an opening-range break of the 11:00–13:30 UTC London-PM range — returned **zero**.

Trend continuation off a 15m EMA-14 reclaim is the right archetype in **both** windows. One
rule, not two.

### 8.3 A real bug this pass found in the study design

The engine signals at a bar's close and fills at the **next bar's open**, so a 15m signal at
21:45 MYT opens the position at 22:00 MYT — *outside* a 19:00–22:00 window. Measured: 5 of 54
base signals on XAUUSDT window B, ~15% on PAXGUSDT 1h, and up to 36% for some filtered
variants. It does not overturn the base-rule conclusions (XAU window B 78.3% → 76.2% under
the strict definition) but it was enough to overturn one candidate finding, and any
window-restricted backtest must state which definition it uses. See `ENTRY_RULES.md` §11.6.

Related and now fixed in the tooling: `simulate()` computed break-even from the **panel**
median ATR. Once you select a subset of hours that is wrong — it understated window A's
break-even by ~4 points. It now uses the ATR of the trades actually taken.

### 8.4 What was quantified, and is worth keeping

The relaxation question — *"can I loosen the rule to trade more often?"* — now has a number
rather than an opinion. Inside window B, scored against base-in-B:

| relaxation | trade multiple | marginal trades win | vs a 66.7% break-even |
|---|---|---|---|
| 5-of-5 → 4-of-5 timeframes | 2.12× | **50.0%** (n=184) | −16.7 |
| 5-of-5 → 3-of-5 | 2.97× | 49.0% (n=292) | −17.7 |
| drop the dip/reclaim event | 1.81× | 53.8% (n=173) | −12.9 |

**The trades a relaxation buys carry zero directional information** — 50.0% is the random
null to one decimal. This is the cleanest negative result in the project, and it closes the
"trade more often" question for good: frequency cannot be bought from this rule at any price.

Leg importance was also measured properly for the first time, by what the trades each leg
*excludes* would have done: the **30m** UT leg is the most load-bearing anywhere (excluded
trades win 47.7% / 46.7% — below the null, actively adverse), then the **1h** leg (−10.4 to
−11.5 points), then the **5m** leg, which is inert in window B and load-bearing in window A.

---

## 9. Tested on request — "buy when 5m and 15m both sell but 1h/4h are still buying"

A chart-reading hypothesis from the trader: when the 5m and 15m UT lanes both print a **sell**
arrow while the 1H and 4H are still bullish, buying looks like a good opportunity.

**Verdict: do not add it. It is at or below the coin flip on gold, and it gets worse the more
exactly you specify what the chart shows.** But the instinct behind it is sound, and the
reason it fails is worth keeping — it is the cleanest available demonstration of why the base
rule is built the way it is.

### 9.1 The result

Standalone archetype (it carries its own direction), so the comparator is the 49.8% null.
Both readings of "sell signal" were tested — the *state* (`bias == -1`, price below the UT
line) and the *event* (the cross just printed, via `age_5m` / `age_15m` freshness).

XAUUSDT 15m, all variants, symmetric 2.5/2.5, break-even ≈ 62%:

| variant | n | pooled WR | edge | PF |
|---|---|---|---|---|
| 1H bullish gate only | 406 | 52.0 | −10.2 | 0.74 |
| 1H **and** 4H gate | 273 | 46.9 | −15.1 | 0.61 |
| + 4H EMA stack | 176 | 51.7 | −9.9 | 0.71 |
| **+ both flips within 120 min** | 223 | 45.7 | −16.3 | 0.60 |
| **+ both flips within 60 min** | 177 | 44.1 | −17.8 | 0.57 |
| **+ both flips within 30 min** | 124 | 42.7 | −19.3 | 0.42 |
| **+ both flips within 15 min** | 81 | **42.0** | −19.4 | 0.40 |
| *(the base rule, for reference)* | 110 | **68.2** | **+6.5** | **1.66** |

**The response is monotone in the wrong direction.** The fresher the two sell arrows — i.e.
the closer to the picture that prompted the question — the worse it performs. At "both flipped
within the last 15 minutes" it is 42.0%, nearly 8 points *below* a coin flip. PAXG 15m
(n=1,060) and PAXG 1h (n=447) agree: 50–54%, every variant below break-even, PF 0.51–0.96.

One coherent detail: the counter-trend version scores **higher on the hostile 2022–24 range
panel (52.9–57.9%) than on the trending ones**, which is exactly what a mean-reversion
archetype should do. It is still nowhere near that panel's ~70% break-even, so it is
*less wrong* in a range, not profitable in one.

### 9.2 Why it looks right on the chart — the ladder

Hold the slow gate fixed (1H and 4H bullish) and vary only how much of the fast-timeframe
pullback you wait out. XAUUSDT 15m:

| rung | n | WR | edge | PF |
|---|---|---|---|---|
| 0. both 5m + 15m still against — **the hypothesis** | 273 | 46.9 | −15.1 | 0.61 |
| 1. 5m flipped back, 15m still against | 176 | 44.3 | −17.4 | 0.47 |
| 2. 15m flipped back, 5m still against | 267 | 51.3 | −9.8 | 0.71 |
| 3. both flipped back (4 timeframes agree) | 353 | 52.1 | −9.1 | 0.75 |
| 4. + 30m agrees (all 5) | 320 | 53.1 | −8.1 | 0.80 |
| 5. **+ 4H EMA stack + dip/reclaim = the base rule** | 110 | **68.2** | **+6.5** | **1.66** |

Monotone up the ladder, on all three gold panels. **The hypothesis is the same setup as the
base rule, entered one step too early.** The trader correctly identified *where* the
opportunity is — a fast-timeframe pullback inside a slow-timeframe uptrend is exactly what
the base rule trades. What the data corrects is *when*: the base rule waits for the fast
timeframes to come back, and that wait is worth roughly 15 points of win rate.

### 9.3 The forward path, which is the mechanism

Median excursion in ATR from the signal bar, XAUUSDT:

| horizon | from the hypothesis' setup | from the base rule's setup |
|---|---|---|
| 60 min | +0.79 favourable / 0.78 adverse | +1.04 / 0.64 |
| 240 min | +1.47 / **1.70 adverse** | **+2.88 / 1.39** |
| 720 min | +2.29 / **3.30 adverse** | — |

From the hypothesis' entry price is a **coin flip for the first hour and then keeps going
against you**. From the base rule's entry it runs 2:1 in favour within four hours. The two
red arrows mark a pullback that is still in progress, not one that has ended.

### 9.4 The trap that makes it look profitable

A mean-reversion bracket appears to rescue it and does not. XAUUSDT, same setup:

| bracket | WR | break-even | edge | PF |
|---|---|---|---|---|
| TP 1.5 / SL 3.0 | **66.1%** | **79.7%** | −13.6 | 0.57 |
| TP 1.5 / SL 2.5 | 61.4% | 77.4% | −16.0 | 0.55 |
| TP 2.5 / SL 2.5 | 46.9% | 62.0% | −15.1 | 0.61 |

A **66% win rate** that loses money at PF 0.57 — §2 of `ENTRY_RULES.md` in one row. Anyone
trading this setup with a small target and a wide stop would see a healthy-looking win rate
in their journal and a shrinking account, which is precisely how this kind of pattern
survives in a trader's memory.

---

## 10. Tested on request — regime switching: "range strategy in chop, trend strategy in a trend"

**The regime gate is dead (3 of 3 adversarial lenses refuted, all high confidence).** So is
the per-regime exit, and so — for the third time — is the range/mean-reversion archetype.
Two things did survive, and one of them is the best-replicated finding in the project.

2,594 conditions across four miners, 181 combinations, three lenses, on the DST-corrected
MT5 GOLD bars at MT5 cost.

### 10.1 The regime gate looked excellent and was not

The classifier miner found one construction monotone on both anchors and in both halves:
`atr_pct_rank60 >= 0.33 AND er24h_rank60 >= 0.33` (two causal 60-day trailing percentiles —
ATR-percent and a 24h Kaufman efficiency ratio). It lifted the 15m base rule 56.7% → **61.0%**
(edge +5.8 → +10.2, PF 1.34 → 1.56) and the 1h rule 58.4% → **64.3%**, replicated on PAXG
2025-26 on both anchors, did not transfer to BTC/ETH, and its out-of-sample half beat its
in-sample half. Leakage was cleared outright — all three features recompute bit-for-bit on
frames truncated at 40 random signal bars.

It still died, three independent ways:

* **The "free skip" is an artifact of running two simulations.** The load-bearing claim was
  that the discarded bucket has exactly zero expectancy (51.3% against a 51.3% break-even).
  But gated n=249 plus skipped n=191 = 440, while the base rule only takes **393** — splitting
  the stream releases 47 trades the base rule never takes, because in the real sequence a
  position was already open. Partitioning the rule's *actual* 393 trades by the gate at entry:
  gate-false n=170, 52.4% against 51.3, mean R **+0.019 — mildly positive.** There was never
  anything to skip for free.
* **Best-of-K.** 596 random gates built from the same construction (60-day trailing rank of two
  real panel columns, threshold from a small set, either direction), restricted to the same
  retention band and n. Single-draw P(random ≥ F3) = 0.0134 — but at the combiner's own
  K = 181, **P(best-of-K ≥ F3) = 0.913**. Any such gate buys +0.7 edge on average; this one
  bought +4.5, and a 181-test search finds that or better nine times in ten.
* **The uniqueness claim was false.** "The only variant whose out-of-sample half beats its
  in-sample half on win rate, PF, mean R and drawdown simultaneously" — 4 of 7 variants satisfy
  it, including both baselines, and so do **55.4% of the 596 random gates**. It is a coin flip,
  not a discriminator. And it is a period artifact: at a 40% split point F3 goes IS 64.3 → OOS
  57.7, at 50% it goes 62.2 → 59.4. Only the 60/40 cut makes the claim true.

Scored the `FEATURE_MINE` §4 way — against the rule's own other trades rather than the 49.8%
null — the gate is z=2.04 (p=0.041) full, z=1.49 in-sample, **z=1.43 out-of-sample**.

### 10.2 The exit hypothesis is refuted, and backwards

"Small target in a range, hold long in a trend" (679 tests): **mean R is monotone increasing
in take-profit inside every regime bucket.** The optimal TP sits at the top of the grid in the
ranging bucket as much as the trending one. The range-minus-trend interaction is +0.09 R at
best against a standard error of 0.15–0.22 (t between −0.30 and +0.87 over 16 tests).

And the premise inverts: **the strong-trend bucket reaches its target FASTER — 3.2h against
5.6h.** "In a trend you hold longer" is the opposite of what gold does.

Choosing the exit per regime buys +0.062 R in-sample and **loses 0.032 R out-of-sample** across
24 half-sample splits, sign-flipping with split direction. One exit for everything.

### 10.3 What survived

**Long-only, and now on 13 years.** (268 tests.) The long leg beats its period-matched
unconditional long comparator on all four samples — 15m 59.4 vs 51.7 (p=0.021), 1h 63.3 vs
53.9, 4.2y 57.5 vs 52.0 (p=0.0027), **13y 54.8 vs 51.2 (p=0.022)**. The short leg clears its
own break-even nowhere across 13 years. Dropping it costs 5–9% of R per week and **cuts max
drawdown 21–51%**. It is not just a bull-market artifact: pooling the six gold-down years
lifts the 13-year short edge only from −3.3 to +1.8 (n=481, p=0.43), and every
*contemporaneous* downtrend definition leaves shorts between −1.3 and +1.7.
**The system is structurally long-only.** "The big trend is up" is best written as 4H UT bias
bullish **and** 4H EMA stack bullish together.

**The morning near-high setup — the only thing in this project to replicate on an untouched
hold-out.** (1,599 tests.) Buying the first 15m bar of the 06:00–09:00 MYT window on days that
open within ~2 ATR of the 10-day high wins **64.0%** (n=50, break-even 50.9, edge +13.1) in
2025-26 against a period- and window-matched unconditional long of 52.4% — and **63.9%**
(n=61, edge +11.9) on a 2022-05…2024-12 hold-out that was never touched during the search.

But not for the stated reason. **The "opens near a recent high" gate does all the work; the dip
and the bounce trigger add nothing** — the same dip-and-bounce without the near-high gate
returns 51.1% in-sample and 51.0% out-of-sample. The 19:00–22:00 mirror fails everywhere
(hold-out 36.4%, edge −16.1), so it is a morning effect, not a session-open effect. It fires
~0.5 times a week and overlaps the base rule on only 14–20% of its trades, so it is a small
independent addition rather than a replacement. **It has not yet had an adversarial pass.**

### 10.4 The account-size finding that changes the plan

The tradability lens found something that applies to every variant, not just this one:

**The gate is mechanically an ATR-percentile filter, so it systematically selects the more
expensive trades.** Median stop $19.73 (gated) vs $17.25 (base) vs **$13.41 (the discarded
bucket)** — rank-sum z=3.91 against base, z=8.27 against the skipped bucket.

At $200 with a 15%-of-equity cap, **42% of the gated rule's trades are unaffordable, and those
trades carry 54% of its total R and 69% of its dollars.** The affordable remainder is n=47 at
59.6% — not significant. At a 10% cap the gated rule takes 9 trades and loses $99, half the
account. Frequency after the cap is 1.67/week out-of-sample: **30 weeks to the 50 trades
`AUTOMATION.md` §10 needs for its own kill criterion**, or 17 months with the window guard on.

This is a general result and belongs in the sizing decision: **a small account does not merely
scale the strategy down, it selects against the trades that carry the edge.**

---

## 11. Candidates five and six — both rejected, 8 of 8 lenses

Adversarial verification of the last two survivors. **Nothing ships. The `ENTRY_RULES.md` rule
stands unchanged.**

### 11.1 NODIP (drop the dip/reclaim legs) — rejected, 4/4

The claim was that removing legs 7–9 gives 60.9% (n=688) against the base rule's 56.7%
(n=393) at 1.75× the trades. Every number reproduced. It died on **failure mode 4**, the same
one that killed the regime gate:

**The base rule's 393 trades are not nested inside NODIP's 688 — only 199 are shared.** The
194 base-exclusive trades (entries NODIP never takes because a position was already open) win
54.1%, against 59.3% for the shared ones. Partitioning NODIP's *own* trade list at entry by
whether the dip/reclaim event was true: **dip-on 60.4% (n=222) vs dip-off 61.2% (n=466),
z = −0.20, p = 0.84.**

**The legs are inert — neither costly nor valuable.** The comparator ladder: z=5.82 against
the 49.8 null → 1.34 against base as two simulations → **−0.20** partitioned correctly.

It also failed replication in the direction that matters: on **XAUUSDT — the live instrument —
NODIP is unprofitable** (n=213, 58.2%, PF 0.98, edge −2.5) while the base rule returns 68.2%
(PF 1.66). NODIP beats base on exactly two panels: MT5GOLD and the *hostile* PAXG 2022-24
tripwire, which is the wrong direction to look good in. Comparator-corrected across six gold
panels the leg effect Stouffers to **z = +0.815, p = 0.415**.

And the venue disagreement is bigger than the effect: on matched calendar, MT5GOLD says NODIP
61.0 / base 57.4 while XAUUSDT says NODIP 58.2 / base 68.2 — same asset, same period,
**opposite sign, 13.6-point difference-of-differences.** Cause unresolved.

Finally, inside the owner's own windows at an affordable size the gap disappears entirely:
NODIP +4.6 edge vs base +4.5.

⚠️ **`AUTOMATION.md` G4 row 1 is therefore wrong** and is corrected in place: the +10.1-vs-+5.8
comparison ran two separate `simulate()` streams sharing only 199 of 393 trades.

### 11.2 MORNING (buy the 06:00–09:00 MYT open near a 10-day high) — rejected, 4/4

This was the only finding in the project to replicate on an untouched hold-out (in-sample
64.0% n=50; hold-out 63.9% n=61). It reproduced exactly and still failed on four counts:

* **Neither period clears break-even at 95%.** Clopper-Pearson: in-sample [49.2, 77.1] against
  be 50.9; hold-out [50.6, 75.8] against be 52.1. This is the identical test that failed the
  base rule in the §5.1 gate.
* **It is a restatement of momentum, and not the best one.** "Within 2 ATR of the 10-day high"
  is rank-correlated **0.72–0.79** with plain 3–10 day momentum, and a matched-retention plain
  momentum gate **beats it on both periods** (in-sample 71.2% vs 67.8%; hold-out 65.6% vs
  63.9%) while sharing only 34–39% of its trades.
* **The window is the argmax of eight placebo blocks.** The same gate lifts **+12.4 (z=2.76)
  at 03–05 MYT** and +10.9 at 09–11 MYT. 06:00–09:00 is not distinguished; the failing
  19:00–22:00 mirror only shows the gate dies in London/NY hours.
* **The hold-out is carried by one year.** Its first 17 months return 53.3%; its last 13
  months return 74.2%. **97% of the hold-out's R comes from those last 13 months**, and
  dropping 2024 takes it from 63.9%/z=1.80 to 57.6%/z=0.65.
* Comparator-corrected, the hold-out lift is z=1.80, **p=0.073** — the replication does not
  clear 0.05. And out of sample both legs are inert on their own (near-high gate alone 51.5%,
  first-bar-of-window alone 53.0%) while the intersection is 63.9% — a corner with no main
  effects, exactly the §4 pattern.

At $200 it is also unrunnable: 2026 median stop **$27.69 = 13.8%** of the account, at a 15%
cap it takes **one trade**, and a single loss locks the account out permanently (bootstrap
P(lockout) = 56%).

### 11.3 The standing null on this panel is not 49.8% — it is ~56%

The most useful number produced by this pass. An empirical null of random 4–6 term trend-state
conjunctions on MT5GOLD 2025-03→2026-08 has a **median pooled win rate of ~56%**.

**The base rule's 56.7% sits at the median of random rules of its own shape.** Any future
candidate on this panel must beat ~56% before it is even interesting, and 683 + 779 + 2,594
conditions plus this pass have now been spent over the same 496 columns — best-of-K at that K
is ≈ 1.00 for anything short of a very large effect.

### 11.4 What is left standing, honestly

* The rule is unchanged and **no longer clearly better than a coin flip's well-dressed
  cousin** on the MT5 panel. Its 68.2% on XAUUSDT and 56.6% on PAXG remain the better evidence.
* **The short side clears break-even nowhere across 13 years.** By the project's own "a
  one-sided lift is not an edge" standard, that is unresolved for the incumbent too.
* **No hold-out era exists on the MT5 panel and one cannot be manufactured** — the broker
  serves 5m only back to 2025-03-25 and the labels are resolved on the 5m path, so the entire
  labelled sample is one bull regime.
* The cost finding (break-even 64% → 51% on the venue change) is the one large, robust,
  reproduced result of the whole project.
