# OPERATING_PLAN.md — what was decided, and the numbers behind it

The other docs describe the *system*. This one describes the *decisions* and the
owner's own baseline, neither of which lives anywhere else.

`ENTRY_RULES.md` = the rule · `AUTOMATION.md` = the bot spec · `FEATURE_MINE.md` = what was
rejected and why · **this file** = what we are actually doing and what it is worth.

---

## 1. The decision, as it stands

**Run the rule live on MT5 `GOLD`, every trade at a fixed 0.01 lot, on a $200–500 account,
while applying four already-confirmed findings to the owner's own manual trading.**

Fixed size is deliberate and is not a risk-control choice. It makes **every trade an
equal-weight sample**, which is the only way the live record can later be compared against
the backtest. Variable sizing would make a good month unattributable — skill or size.

Because size is fixed, `AUTOMATION.md` §3's `risk_pct` and its "skip the signal if the
minimum lot exceeds the cap" logic are **void and must not be implemented**. Every signal
trades at 0.01.

~~**Still blocking:** the DST bug~~ - **fixed and re-run 2026-08-23**. `data/broker_clock.py`
now localises to `Europe/Athens`; the corrected panel agrees with this session's independent
ingest on 99,844 bars to 0.0000. The gate held: +2.2 -> +2.1 edge. See `AUTOMATION.md` §4a.

**The bracket and the side are now decided by measurement - see §8, which supersedes the
2.5/2.5 both-sides assumption used everywhere above.**

---

## 2. What $200 at a fixed 0.01 lot buys

`GOLD` 0.01 lot = 1 oz. Stop = 2.5 × ATR dollars, and it cannot be sized down.
Median win **+$16.48**, median loss **−$17.12** (signal-bar ATR median 6.72, spread 0.32).

One year = 278 trades at 5.35/week. 40,000 Monte Carlo paths, real ATR distribution:

| start | P(ruin) @ 53.5% | median end | P(ruin) @ 56.7% | median end |
|---|---|---|---|---|
| **$200** | **39.6%** | $368 | 19.5% | $791 |
| **$300** | **23.6%** | $559 | 8.6% | $921 |
| **$500** | **7.4%** | $786 | 1.5% | $1,128 |
| $1,000 | 0.2% | $1,288 | 0.0% | $1,628 |

**53.5% is the honest planning number** — it is the 4.2-year, n=1,348 measurement. 56.7% is
the recent 17 months and sits inside one bull regime.

Two facts that explain the shape:

* **12 consecutive losses zero a $200 account**, and P(≥8 consecutive losses somewhere in 278
  trades at 53.5%) is **27.5%**.
* Median max drawdown is **$244–274 — larger than the account itself.** Survival requires
  absorbing a drawdown bigger than the starting balance, which is why ruin is this likely.

**$100 more capital is worth 16 points of survival** ($200 → $300 takes ruin from 39.6% to
23.6%). That is a larger effect than any parameter tuned in six mining passes.

### The equity floor

Stop at **$60**, not at zero. At that point ~80–100 real trades have been recorded — the data
is banked, and there is capital left to restart with. Running to zero buys nothing.

---

## 3. The owner's own record — the benchmark the bot has to beat

367 closed trades, 2026-08-01 → 08-23, from `broker_trades`. **This is better evidence than
anything the backtest produced**, and it is the thing to compare against.

```
net              +$3,231.15
win rate         56.7%          <- the backtest's own prediction is 56.6%
avg win/loss     +$48.58 / -$43.23     profit factor 1.47
expectancy       +$8.80 per trade
median hold      87 minutes
best day         +$1,609.04
```

Three things that are not visible from the headline:

* **Three days made everything.** Best day = 50% of total; best 3 days = **109%** of total;
  the other 15 days = **−$278.74**. Profitable days: 50%.
* **Peak equity $5,250.81 → final $3,231.15** — $2,106 given back, a 40% drawdown.
* **Size does nothing; frequency does everything.** Correlation of daily P/L with daily lots
  = **+0.14**; with daily trade count = **+0.70**.

Per instrument:

```
XAUUSDT   348 trades   +$3,574.62   58.0%
ETHUSDT    14 trades     -$127.57   42.9%
BTCUSDT     5 trades     -$215.90    0.0%   (0 for 5)
```

**All the money is gold.** The bot's expected 56.6% is the same hit rate the owner already
achieves at ~20 trades/day against the rule's 0.8/day — so the bot's case is discipline and
drawdown, not accuracy.

---

## 4. Plan B — four findings for the manual trading, usable today, no bot required

1. **Venue cost.** Break-even win rate 64.3% on Bybit → **50.9%** on MT5 `GOLD` (spread
   median 0.38 = 0.83 bps against 11 bps). The single largest and most robust result of the
   whole project.
2. **Gold long-only.** Across 13 years the short side clears break-even on no sample at any
   depth. Dropping shorts costs 5–9% of R per week and **cuts max drawdown 21–51%**.
3. **No crypto.** The rule scores 52.7% on BTC and 50.0% on ETH — the null — and the owner's
   own record says the same thing louder.
4. **Frequency, not size.** From his own P/L: +0.14 with lots, +0.70 with trade count.

---

## 5. Status and build order

| step | state |
|---|---|
| 1. Fix the DST bug, re-ingest, re-run gate §4b | DONE - edge held, windows died |
| 1b. Decide side + bracket (§8) | DONE - long-only, TP5.0/SL2.5 |
| 2. `signals.jsonl` — every evaluation, fired or not | not started |
| 3. Telegram notifier | not started |
| 4. `exec/mt5_exec.py` with the §C guards, `execute: false` | not started |
| 5. `live_vs_backtest.py` — bar-for-bar reconciliation | not started |
| 6. Paper run ≥ 30 signals | not started |
| 7. Flip `execute: true`, enable AutoTrading | **gated on 1–6** |

Guards at fixed 0.01 lot, in dollars rather than percentages:

```
max_concurrent 1 · GOLD only · all-day (no window gate)
daily_loss_limit  $60      consecutive_loss_halt  5
equity_floor      $60      max_spread_points      8
kill switch       reports/HALT
review trigger    pooled win rate over the last 50 closed trades < 52%
```

`terminal.trade_allowed` is currently **False** — leave it that way until step 7.

**Every order must carry its stop in the same `order_send`.** Four ETHUSD shorts sat open with
no stop on 2026-08-23; that is the failure this guard exists to prevent.

---

## 6. Two sessions are working on this

* **"XAU/USDT backtesting engine"** (`local_f8aa4da6-…`) — owns the repo build. Wrote
  `data/mt5_rates.py`, `tools/validate_rule.py`, 12 missing signal legs, 248 tests, and
  `AUTOMATION.md` §4b. It independently confirmed three things found here: shorts have no
  edge, 2023 is the only negative year (and 2023 gold ranged), and PAXG's year-slope is
  **mostly liquidity, not regime** — `GOLD` has a deep book throughout and its slope is far
  flatter (49–58% against 40–59%).
* This session — owns measurement and adversarial verification (`tools/mine/`).

Where they disagreed, the disagreement was real and mattered: the DST bug, and the
two-simulation comparison that made NODIP look good. **Keep both, and make them argue.**

---

## 7. What would still worry me

* **The incumbent is thinner than its headline.** 56.7% on the recent MT5 panel sits at the
  *median* of random rules of the same shape (`FEATURE_MINE.md` §11.3). Its better evidence
  is XAUUSDT's 68.2% and the 4.2-year 53.5%, and those disagree with each other.
* **The short side is unresolved for the incumbent too.** By the project's own "a one-sided
  lift is not an edge" standard, this should probably ship long-only.
* **No hold-out era exists on the MT5 panel and one cannot be manufactured** — the broker
  serves 5m only back to 2025-03-25 and labels resolve on the 5m path.
* **The search budget on these columns is spent.** 4,000+ conditions over the same 496
  columns; best-of-K is ≈1.00 for anything short of a very large effect. More mining is not
  the move. Live out-of-sample data is the only new information available.


---

## 8. THE SHIPPING CONFIG - decided 2026-08-23, supersedes §1's assumptions

```
symbol      MT5 GOLD          anchor 15m        cost 0.85 bps (broker's own per-bar spread)
side        LONG ONLY
bracket     TP 5.0 x ATR14    SL 2.5 x ATR14    both fixed at entry, never moved
time stop   96 anchor bars = 24 h, exit at market
legs        the 9 legs of ENTRY_RULES §4, with LEG 5 (5m UT bias) OFF
            dip/reclaim legs 7-9 ON (the retraction in FEATURE_MINE §11.1 stands)
session     gold_session only.  NO hour-of-day window.
size        fixed 0.01 lot, every trade
```

Measured on `MT5:GOLD` 15m, 2022-05-30 -> 2026-08-21, path walked at 15m, non-overlapping
sequential, ties resolved as losses:

| | n | win% | 95% CI | break-even | net/unit | PF |
|---|---|---|---|---|---|---|
| **long only** | **588** | **42.5%** | **38.6-46.5** | **34.2%** | +1,363.35 | 1.38 |
| pooled (reference) | 994 | 40.3% | 37.3-43.4 | 34.2% | +2,346.19 | 1.38 |

**This is the first configuration in the project whose 95% CI lies entirely above break-even.**
The incumbent TP2.5/SL2.5 was 53.4% against a 51.3% break-even with a CI of 50.8-56.1 - the
lower bound sat *below* the bar. Here the lower bound clears it by 4.4 points.

### 8.1 The comparator that changes the interpretation

Every earlier judgement scored the rule against break-even or against the 49.8% pooled null.
Neither is the right comparator for a **long-only** rule on an instrument that went 1,800 ->
4,300: a random long clears break-even on drift alone. So a matched random-entry null was
run - same panel, same session filter, same next-bar-open entry, same non-overlapping
sequential protocol, same cost, ties as losses, 300 replicates per cell. Only the entry is
random.

| bracket | side | rule | break-even | random-entry null | **drift** (null - be) | **skill** (rule - null) |
|---|---|---|---|---|---|---|
| TP2.5/SL2.5 | long | 55.2 | 51.4 | 51.6 | +0.3 | **+3.6** |
| TP2.5/SL2.5 | short | 50.9 | 51.2 | 47.9 | -3.4 | **+3.0** |
| TP3.5/SL2.5 | long | 48.1 | 42.8 | 44.7 | +1.9 | **+3.4** |
| TP5.0/SL2.5 | long | 42.5 | 34.2 | 39.1 | **+4.9** | **+3.4** |
| TP5.0/SL2.5 | short | 37.2 | 34.1 | 34.1 | +0.0 | **+3.1** |
| TP5.0/SL2.0 | long | 38.2 | 29.5 | 34.1 | +4.6 | **+4.0** |

Null sd is 0.6-1.0 points; the rule sits at the 100th percentile of 300 replicates in almost
every cell. Three things follow, and two of them contradict what this document said before:

1. **The rule's skill is ~+3.3 points and is the same at every bracket and on BOTH sides.**
   It is a real, direction-neutral entry edge, and this is the strongest evidence the project
   has produced for the entry rule itself - larger and far more consistent than any of the six
   filters that were mined and rejected.
2. **The short side is not broken - gold's drift is.** Shorts beat their own matched null by
   +3.0 at the 100th percentile. They lose money because a symmetric-bracket short pays a
   -3.4 drift tax that +3.0 of skill cannot cover. The earlier phrasing ("the short side has
   no edge") was wrong about the cause and right about the conclusion.
3. **Most of the big-TP advantage is gold, not the rule.** At TP5.0 the drift term is +4.9
   while the skill term is unchanged. Widening the target is a bet on gold's continuation.
   That bet has paid on 13 years and on both anchors, but it should be named for what it is.

WARNING: the 0.6-1.0 null sd measures **entry-timing** variation on one price path. It is not
path uncertainty. The year-to-year swing (34.9% -> 45.6%) is far larger, and that is the
honest error bar on next year.

### 8.2 Why TP 5.0 / SL 2.5 - chosen by survival, not by expectancy

At a fixed 0.01 lot the account is not ranked by expectancy, it is ranked by whether the
drawdown arrives before the drift. Every trade was re-expressed in R and repriced at 2026's
median signal ATR (10.07), because a 2022 trade's raw dollars are a sample of 2022's gold
price, not of what the account will meet next year. Then the **real trade sequence** was
walked from $200 - the bootstrap destroys the ordering, and the ordering is where 2022-2023
lives:

| long-only bracket | $200 becomes | lowest equity reached | worst streak | verdict |
|---|---|---|---|---|
| TP2.5/SL2.5 | $1,836 | **-$130** | 6 | BLEW UP |
| TP3.0/SL2.5 | $2,083 | $25 | 6 | below the $60 floor |
| TP3.5/SL2.5 | $2,316 | $60 | 6 | exactly at the floor |
| **TP5.0/SL2.5** | **$2,862** | **$98** | 9 | **SURVIVED** |
| TP5.0/SL2.0 | $2,917 | **-$21** | 9 | BLEW UP |

**Exactly one bracket survives the actual history from $200, and it is not the configured one.**
The reason is sequence: the panel's first 1.7 years are flat-to-negative for every bracket, so
a $200 account meets its drawdown before its profit. TP5.0/SL2.5 is the only cell whose 2022
and 2023 are both non-negative (+$9.5, +$83.0) - the wide target is what carries the range year.

40,000-path bootstrap over one year, same repricing, i.i.d. and 20-trade moving block:

| long-only | trades/yr | exp/trade | $/yr | P(ruin) iid / block | P(hit $60) | median end | 10th pct |
|---|---|---|---|---|---|---|---|
| TP2.5/SL2.5 | 192 | $2.05 | +$394 | 20.7% / 16.7% | 32.6% | $595 | $156 |
| **TP5.0/SL2.5** | **141** | **$4.53** | **+$638** | **18.6% / 15.8%** | **29.9%** | **$834** | **$310** |
| pooled 2.5/2.5 | 324 | $1.24 | +$402 | 36.0% / 33.5% | 48.7% | $609 | $35 |
| pooled 5.0/2.5 | 239 | $3.09 | +$738 | 31.3% / 30.2% | 43.6% | $934 | $250 |

**Adding the short side halves per-trade expectancy ($4.53 -> $3.09) and nearly doubles ruin
(18.6% -> 31.3%) for 16% more annual dollars.** On a $200 fixed-lot account that trade is not
close. Long-only is confirmed a third time, now on the metric that actually binds.

### 8.3 What this costs, and what it does not fix

* **Trade frequency falls to ~141/year - 2.7 a week, not 10 a day.** The wide target holds
  positions longer and the sequential simulation skips more candidates. If more trades are
  wanted the answer is more instruments or a second anchor, **not** a narrower target: the
  narrow target is the cell that blew the account up.
* **The win rate is 42.5% and that is correct.** Break-even is 34.2%. Reading 42.5% as a bad
  number is reading a win rate without its bracket, which `ENTRY_RULES.md` §2 exists to
  prevent. Expect long losing streaks: **9 consecutive is in the real record.**
* **$200 is still marginal.** The best bracket survived history with $98 of headroom on one
  path, and the bootstrap still says roughly 1 in 6 accounts dies. $300-500 remains worth more
  than any parameter in this table (§2).
* At SL 2.5 x ATR(10.07) a loss is about $26, so §2's "12 straight losses zero a $200 account"
  becomes **8 straight** - and 9 straight is in the real record. ~~The consecutive-loss halt at
  5 is doing real work and must not be raised.~~ **Wrong - corrected in §10.3.** At a 57.5% loss
  rate a 5-run is ordinary, and the halt as specified would stop the bot about four times a
  year for nothing.

### 8.4 Executor requirements this creates

1. **The 96-bar (24 h) time stop is part of the measured system.** Every table above closes
   the position at 96 anchor bars; 11% of trades exit that way at this bracket (65 of 588).
   An executor without it is running a different strategy than the one that was measured.
2. **TP and SL are fixed at entry and never moved.** `ENTRY_RULES.md` §5's partial-at-2.5 and
   chandelier trail were never measured on this panel - and a partial at 2.5 ATR is precisely
   the amputation of the winner that makes TP5.0 work. **Do not ship the trail.**
3. **Leg 5 must be OFF in the live config**, not merely absent from the data. It caps the
   panel at 5m depth and every number above was measured without it.
4. All three must be asserted by `live_vs_backtest.py`, not merely configured.

### 8.6 Verification of §8's three measurements — independent, 2026-08-23

All three scripts were re-run and re-derived before any of §8 was built on.

**Headline reproduces exactly** via `tools/validate_rule.py` (itself pinned against
`ENTRY_RULES.md` on Bybit at n=112 / 67.0%): long 588 / **42.5%** / CI 38.6–46.5 /
net +1,363.35 / PF 1.38, break-even 34.2%, **65 timeouts of 588 = 11.1%**, by-year
34.9 → 45.6. Pooled 994 / 40.3% / CI 37.3–43.4.

**`null_longonly.py`** — all twelve cells reproduce. Skill ranges +1.5 to +4.0 and clusters
at +3.0 to +3.9 across brackets and both sides. One methodological check: the script
reimplements Wilder ATR locally instead of importing the project's. Compared over 99,830
bars the two are **identical at the median with only 33 bars differing by >0.1%**, so the
null's brackets match the rule's and the concern is void.

**`byyear.py`** — the survival table reproduces exactly: TP2.5/SL2.5 → −$130 (blew up),
TP3.0 → $25, TP3.5 → $60, **TP5.0/SL2.5 → $98 (survived)**, TP5.0/SL2.0 → −$21. And
TP5.0/SL2.5 is confirmed the only cell with both 2022 (+$9.5) and 2023 (+$83.0)
non-negative. **Its worst streak is 9** — which is why the halt at 5 must not be raised.

**Two caveats on `bracket_ruin.py`, neither of which changes the ranking:**

1. **P(ruin) does not model the guards.** The Monte Carlo keeps trading through a blown
   account; the real system halts at 5 consecutive losses and at a $60 floor. So 18.6% is an
   *unguarded* figure — conservative, which is the right direction, and `p_floor` (29.9%) is
   reported separately.
2. **`med_end` and `p10_end` let bankrupt paths recover**, which cannot happen. Those two
   columns only are optimistic. The ruin and floor probabilities are unaffected.

Also worth naming: the real-sequence walk in §8.2 is **one path**, not a distribution. It is
the actual history, which is exactly why it is worth having — but n=1.

---

### 8.5 Reproduce

```
py -m backtest.tools.validate_rule --symbol MT5:GOLD --anchor 15m --no-5m    --path-tf 15m --tp 5.0 --sl 2.5 --by-year
```

`--path-tf 15m` is required: the 1m series only reaches 2026-05-12, and letting the harness
auto-pick it silently truncates the panel to 76 trades. The bracket/ruin sweep and the
random-entry null are `tools/mine/bracket_ruin.py` and `tools/mine/null_longonly.py`.

---

## 9. BUILD LOG — step 2 of 7 complete

| step | state |
|---|---|
| 1. DST fix + reload + §4b re-run | ✅ reported and reviewed |
| **2. `signals.jsonl`** | ✅ **`engine/live_signal.py`, `cli signals`** |
| 3. Telegram notifier | ⏸ next |
| 4. `exec/mt5_exec.py`, `execute: false` | ⏸ |
| 5. `live_vs_backtest.py` | ⏸ |
| 6. paper run ≥30 signals | ⏸ |
| 7. `execute: true` | ⏸ **owner's decision** |

### The distinction the log had to make

A first version logged only `fired` — every bar where the legs agree. That is **15.1/week on
the panel**, not 2.66: the measured system is non-overlapping sequential, so 5.7 out of every
6.7 firings happen while a position is already open. Reporting that as the live rate would
have overstated it 5.7x.

The log therefore records `fired` and **`would_enter`** separately, with `position_since_ms`
and a reason on every blocked bar. `RuleConfig` refuses to construct if the time stop is not
96, if a partial or trail is enabled, or if leg 5 is on — the three §8.4 omissions, asserted
rather than merely configured.



---

## 10. Trade frequency and funding - measured 2026-08-24

The live logger reports about **2 entries a week**, against the owner's stated preference for
many more. That number is real, and its cause is not the rule.

### 10.1 The rule fires 15.2 times a week. The queue drops 82% of them.

| | per week |
|---|---|
| long signals that pass all 8 legs | **15.2** |
| long signals that become trades at `max_concurrent = 1` | **2.7** |

The simulation skips a candidate while a position is open, and at TP 5.0 positions are held
long, so the one-position cap - not selectivity - is what sets the trade count. **Frequency is
a funding decision, not a rule change.** Nothing about the entry needs loosening, and loosening
it is the one move that would spend the +3.3 skill measured in §8.1.

### 10.2 What each slot delivers, and what it costs

Same rule, same bracket, long-only, fixed 0.01 lot; only the number of simultaneous positions
changes. Equity paths walked in true calendar order on the real sequence, bootstrap resampled
in 20-trade calendar blocks (i.i.d. resampling would destroy exactly the clustering that
concurrency creates):

| slots | trades/wk | win% | exp/trade | $/yr | worst streak | equity for 15% ruin | for 5% ruin | ruin @$200 |
|---|---|---|---|---|---|---|---|---|
| **1** | **2.7** | 42.5 | $4.53 | **+$629** | 9 | **$200** | $325 | **15.2%** |
| **2** | **5.0** | 43.4 | $4.95 | **+$1,282** | 15 | **$350** | $525 | 34.4% |
| 3 | 6.9 | 43.1 | $4.52 | +$1,615 | 20 | $500 | $750 | 45.6% |
| 4 | 8.5 | 42.7 | $4.27 | +$1,886 | 24 | $650 | $1,000 | 55.0% |
| 6 | 11.0 | 42.7 | $4.46 | +$2,546 | 28 | $850 | $1,325 | 60.3% |

**Win rate and per-trade expectancy are flat across the whole range (42.5 - 43.4).** The
signals the queue was discarding are exactly as good as the ones it kept, so this is not a
quality-for-quantity trade. It is purely capital: **one slot costs about $150 of equity.**

Return on equity at a constant 15% ruin budget peaks at two slots:

```
1 slot  $200 -> $629/yr    315%
2 slots $350 -> $1,282/yr  366%   <- best
3 slots $500 -> $1,615/yr  323%
4 slots $650 -> $1,886/yr  290%
```

**Recommendation: $350 and `max_concurrent = 2`.** $150 more equity doubles both the trade
count and the annual dollars at the same survival odds. If the $200 is fixed, stay at one
slot - at $200 two slots is a 34% chance of ruin and the real 2022-23 sequence takes the
account to **-$35**, i.e. it does not survive the history at all.

⚠️ Concurrent longs in one instrument are **not** diversification. Two open longs in gold is a
0.03 lot position with two stops, and the correlation shows up as longer losing runs: worst
streak goes 9 -> 15 -> 20 as slots go 1 -> 2 -> 3.

### 10.3 CORRECTION: `consecutive_loss_halt 5` is a nuisance trigger, not a guard

The halt was specified against a 53.5% planning win rate. The shipping bracket wins **42.5%**,
and a halt threshold is a statement about the *loss* rate, so it does not carry over. Measured
on the real sequence:

| k consecutive losses | 1 slot: happens | 2 slots: happens | drawdown at 0.01 lot |
|---|---|---|---|
| 5 | **3.8x per year** | **12.3x per year** | -$129 |
| 6 | 2.1x per year | 8.8x per year | -$155 |
| 7 | once per 2.1 yr | 5.2x per year | -$181 |
| **8** | **once per 4.2 yr** | 4.3x per year | -$206 |
| 10 | never in 4.2 yr | 2.1x per year | -$258 |
| 12 | never in 4.2 yr | once per 2.1 yr | -$310 |

**As specified the bot would halt itself about four times in its first year on ordinary
variance.** A guard that cries wolf gets switched off, and then it is not there for the one
time it mattered.

```
consecutive_loss_halt   8   at max_concurrent 1
                       12   at max_concurrent 2
```

And note what the table says about the halt's role at this account size: 8 straight losses is
**-$206**, more than a $200 account. **The halt cannot save a $200 account - the equity floor
is the only guard that acts in time.** Keep `equity_floor $60` as the real stop; treat the
consecutive-loss halt as a "something is broken" detector, which is all it can be here.

The daily-loss limit was set the same way and is fixed in §10.5 from the measured daily P/L
distribution rather than from a round number.

### 10.4 CORRECTION to the build order: the 30-signal paper run costs 11 weeks

`AUTOMATION.md` step 7 asks for a paper run of >= 30 signals before going live. At 2.7 fired
signals a week that is **11 weeks of waiting**, and it tests only the plumbing.

Replace it with the stronger and faster check: **reconcile `engine/live_signal.py` against
`tools/validate_rule.py` bar-for-bar over the last 90 days of stored history** - every 15m bar,
every leg, both must agree on `fired`, on each leg's boolean, and on entry/SL/TP/time-stop to
the tick. That is thousands of comparisons, it runs in minutes, and it catches the class of bug
a paper run would miss (a leg computed on a different bar, an off-by-one in the anchor, a stale
ATR). Then a **two-week live paper run** - about 5 signals - only to prove order plumbing,
spread gating and the notifier.


### 10.5 THE FUNDED CONFIGURATION - owner's decision, 2026-08-24

**$350, `max_concurrent = 2`.** The owner chose the best return-on-equity cell in §10.2: $150
more capital than the original plan buys double the trades and double the annual dollars at
the same 15% ruin budget.

```
account         $350          size  fixed 0.01 lot        symbol  MT5 GOLD 15m, long only
max_concurrent  2             bracket TP 5.0 / SL 2.5 x ATR14, fixed at entry
time stop       96 bars (24h) session gold_session, all hours

consecutive_loss_halt   12        (§10.3 - 8 would fire 4.3x/yr at two slots)
daily_loss_limit        $150      (§10.5 below)
equity_floor            $80
max_spread_points       8
kill switch             reports/HALT
review trigger          pooled win rate over the last 50 closed trades < 36%
```

Note the review trigger moved with the bracket: the old "< 52%" was written for TP2.5/SL2.5.
At this bracket break-even is 34.2% and the expected rate is 42.5%, so **52% would fire
permanently**. 36% is the number that means something has actually broken.

**Daily loss limit, measured.** P/L attributed to the day the position closed:

| limit | 1 slot: fires | 2 slots: fires |
|---|---|---|
| -$60 | 1.2x/yr | **7.8x/yr** |
| -$100 | never in 4.2y | 3.3x/yr |
| -$130 | never | once per 1.4 yr |
| **-$150** | **never** | **once per 2.1 yr** |
| -$180 | never | never in 4.2y |

Worst day in the whole panel: **-$78 at one slot, -$155 at two.** So `$150` sits just inside
the worst day on record and outside everything else - it fires on the genuinely bad day and
never on an ordinary one. At one slot the daily limit is nearly redundant (the worst day is
-$78 and only 92 days a year even have a close); at two slots it is doing real work.

**Equity floor $80, and how close that is.** Fixed lot means the equity path is start-invariant,
so the real 2022-23 sequence that bottomed at -$35 from $200 bottoms at **$115 from $350**.

* A floor at $80 would **not** have stopped the run. Headroom: $35.
* A floor at $100 would have been $15 away from stopping it at the worst possible moment.
* A floor at $120 would have killed the account right before the 2024-25 recovery that made
  all the money.

**$350 at two slots survives the real history, but by $115, not comfortably.** That is the
honest statement of what was bought. Bootstrap ruin at $350 / 2 slots is ~15%, the same budget
as $200 / 1 slot - the risk was held constant and the throughput doubled, which was the point.

### 10.6 Live status vs this plan

| | value |
|---|---|
| built | rates + DST fix, `validate_rule.py`, `engine/live_signal.py`, `signals.jsonl` (schema 2) |
| not built | notifier, `exec/mt5_exec.py`, `live_vs_backtest.py` |
| `execute` | **false** - and `terminal.trade_allowed` is False |
| expected firing rate to check against | **5.0 entries/week**, from ~15.2 signals/week |

If the live logger reports far from 15.2 raw signals a week, the live evaluator and the harness
disagree and §10.4's reconciliation must find out why before anything is funded.
