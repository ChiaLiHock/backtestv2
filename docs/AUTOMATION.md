# AUTOMATION.md — running the rule unattended on MT5

**Status:** specification. Nothing here is built yet. The owner has decided to go to live
execution on a small account; this file is the build order.

**Read first:** `ENTRY_RULES.md` (the rule), `FEATURE_MINE.md` (what was rejected and why).
This file assumes both.

---

## 0. The one architectural rule

> **The deterministic rule decides. The language model narrates and flags exceptions.
> The model never chooses an entry, an exit, a stop, or a size.**

This is not a style preference. The rule's numbers come from 1,462 measured conditions across
six panels and 4.4 years. The "read the snapshot and judge" layer has been measured **zero
times**. Automating the second while the first exists puts the untested component in charge
of the account while the owner is at work.

Concretely: on 2026-08-23 08:11 UTC the app's own export read `NO CLEAR SETUP · long quality
0 · short quality 38`, while four 1-lot ETHUSD shorts opened 33–93 minutes earlier sat open
with **no stop loss**. The two layers already disagree. Build the one with evidence.

---

## 1. Should the feed move from Bybit to MT5? **Yes — and it is the largest single
improvement found in this project.**

Measured against the live XM terminal (account 335445171 @ XMGlobal-MT5 9, build 6140):

| fact | value | why it matters |
|---|---|---|
| `GOLD` spread | **0.53** on 4603.92 = **1.15 bps** | Bybit taker round trip is **11 bps** |
| `GOLD24-7` spread | 1.50 = 3.26 bps | still 3.4× cheaper than Bybit |
| `GOLD` contract | 100 oz / lot, min **0.01 lot** = 1 oz | |
| `GOLD` H4 history | **20,000 bars back to 2013-08-30** | 13 years of the *actual traded instrument* |
| `GOLD` M15 history | 60,000 bars back to 2024-02-06 | 2.5 years |
| `real_volume` | **0** — tick count only | see §2.2 |
| server clock | **UTC+3** | see §2.1 |
| account | equity $155.45, balance $209.87, leverage 1:1000 | see §3 |
| `terminal.trade_allowed` | **False** | AutoTrading is off; `order_send` will fail until enabled |

### ⚠️ Correction to the spread figures above (verified 2026-08-23 16:40 MYT)

The `GOLD` spread of **0.53 in the table above was read off a stale tick.** It was taken on a
Sunday with the gold book shut — `symbol_info_tick('GOLD')` was returning Friday's last quote,
**35.5 hours old**. A frozen spread from a closed market is not a tradeable number, and the
whole cost argument rests on it.

Settled properly from the broker's own per-bar `spread` field, which MT5 records on every
rate: **20,000 M15 bars, 2025-10-15 → 2026-08-21.**

| | median | p90 | p99 | max |
|---|---|---|---|---|
| `GOLD` all hours | **0.38** (0.85 bps) | 0.51 | 0.54 | 1.37 |
| `GOLD` 06:00–09:00 MYT | **0.40** (0.93 bps) | 0.52 | 0.67 | — |
| `GOLD` 19:00–22:00 MYT | **0.31** (0.74 bps) | 0.45 | 0.51 | — |
| `GOLD24-7` all hours | 1.50 (3.28 bps) | 1.50 | 2.02 | 2.50 |

**The conclusion survives and improves.** The real spread is ~28% tighter than the stale
reading, and tighter still in the evening window. `GOLD24-7` at 1.50 was correct.

Corrected break-even at a 2.5/2.5 ATR bracket, ATR(15m)≈7:

| venue | round-trip cost | break-even WR |
|---|---|---|
| Bybit XAUUSDT | $5.06 | 64.5% |
| MT5 `GOLD`, spec's stale 0.53 | $0.53 | 51.5% |
| **MT5 `GOLD`, measured median 0.38** | **$0.38** | **51.1%** |
| MT5 `GOLD`, 19:00–22:00 MYT 0.31 | $0.31 | 50.9% |
| MT5 `GOLD24-7` 1.50 | $1.50 | 54.3% |

**This discharges gate §5.2.** That gate asks for "one week of per-minute spread"; the
broker's own record supplies ten months of per-bar spread, already split by window. No
waiting required. What it does *not* supply is realised slippage — the spread is the quoted
cost, not necessarily the filled one — so the paper run (§5.3) should log intended vs actual
fill and that residual should be reported before `execute: true`.

### ⚠️ A cost the table omits: overnight swap

`GOLD` swap is **−94.64 points long / +12.17 short** per lot per night. Per 0.01 lot that is
**−$0.89 for a long**, verified against the owner's own history (30 of 348 real trades crossed
a rollover, −108.12 total, implying −$0.89 per 0.01 lot per night).

**A single overnight hold costs 2.3× the entire round-trip spread**, and it is charged only to
longs; shorts are paid. The rule's holds are measured in hours and the owner's median is 86
minutes, so most trades never touch it — but the cost model should carry it as a conditional
rather than omit it, and a guard that declines to open a **long** shortly before 00:00 server
time (21:00 MYT — inside the evening window) is worth considering. Note this cuts against the
evening window, which is otherwise the cheapest by spread.

### The cost finding

`ENTRY_RULES.md` §2 established that break-even win rate is
`(SL·ATR + cost) / ((TP+SL)·ATR)` and that on Bybit this is **62–64%** at a 2.5/2.5 ATR
bracket — the constraint that has dominated every conclusion in the project. On MT5 GOLD:

| venue | round-trip cost | break-even WR at 2.5/2.5, ATR(15m)≈7 |
|---|---|---|
| Bybit XAUUSDT | 11 bps ≈ $5.06 | **62–64%** |
| MT5 `GOLD` | 1.15 bps ≈ $0.53 | **≈ 51.7%** |
| MT5 `GOLD24-7` | 3.26 bps ≈ $1.50 | ≈ 54.3% |

The rule measures **56.6%** on the large PAXG sample and **68.2%** on XAUUSDT. On Bybit only
the small sample cleared break-even. **On MT5 both do, for the first time.** This is worth
about ten points of margin — more than every entry filter tested in `FEATURE_MINE.md`
combined, and about three times the maker-fee lever in `ENTRY_RULES.md` §5.1.

⚠️ Two caveats before believing it. The 0.53 reading was taken while `GOLD` was **closed**
(last bar Friday 23:45 server) so it may be the last live quote rather than a typical one;
spreads widen at news and at the daily rollover. **Log the live spread every minute for a
full week, and specifically inside the two trading windows, before sizing anything on it.**
And XM `GOLD` is spread-only on Standard accounts — if this is a Zero account there is a
commission to add.

Swap: `GOLD` swap_long **−94.64** / swap_short **+12.17** per lot per night. At 0.01 lot that
is −$0.95 / +$0.12. Holds average 2.5–5.7 hours so most trades never pay it, but a position
carried across 00:00 server time does. Charge it in the P/L reconciliation, not in the entry
decision.

---

## 2. The four things that are NOT a drop-in swap

### 2.1 Server time is UTC+3, so the H4 bars are offset three hours

Confirmed: server clock reads 11:22 when real UTC is 08:22. MT5 H4 buckets therefore start at

```
server 00:00 04:00 08:00 12:00 16:00 20:00
= UTC  21:00 01:00 05:00 09:00 13:00 17:00
```

Bybit's H4 buckets start at 00:00/04:00/08:00/12:00/16:00/20:00 **UTC**. They do not line up.

This is not cosmetic. The two most load-bearing legs of the rule are the **4H UT bias** and
the **4H EMA stack** (`ENTRY_RULES.md` §11.5), and the UT level is a *path-dependent latching*
trailing stop — a different bar partition produces a different flip history, not a slightly
different number.

**Required:** store MT5 bars with `open_time` converted to true UTC epoch ms, keep the H4
partition exactly as the broker publishes it (do **not** re-bucket), and treat MT5-derived
results as a separate instrument from the Bybit ones. Never mix them in one panel.

### 2.2 Volume is tick count, not traded size

`real_volume` is 0 on every bar; only `tick_volume` is populated. The port already handles
this shape (`indicators/mathseries.py: to_tick_volume`, and `SIGNAL_MAP.md` §K notes the
Bybit-vs-tick distinction). Ratios such as `relative_volume` remain meaningful because they
are unitless; absolute turnover gates do **not** transfer and must not be ported.

### 2.3 `GOLD` has real sessions; the Bybit perpetual does not

`GOLD` stops on weekends and has a daily rollover break. The engine's `gold_session` filter
(Sun 21:00 UTC → Fri 22:00 UTC) was written for a 24/7 perpetual that merely *prints* thin
weekend bars. On MT5 those bars do not exist, so the sync must be genuinely gap-tolerant and
`coverage --show-gaps` will report real gaps that are not errors.

`GOLD24-7` does trade continuously but is thin (tick volume 22–43 per 15m bar against 1,343–
2,183 on `GOLD`) and carries a 3× wider spread. **Trade `GOLD`. Use `GOLD24-7` only if you
later want weekend coverage, and re-validate separately if you do.**

### 2.4 The measured edge was measured on Bybit bars

Every number in `ENTRY_RULES.md` comes from Bybit candles. Moving the feed invalidates none
of the *reasoning* but all of the *calibration*. §5 below makes re-validation a gate.

The upside: `GOLD` H4 goes back to **2013**, which is three times PAXG's history and is the
instrument actually being traded. That is a materially better validation set than anything
used so far.

---

## 3. Position sizing — the binding constraint, stated plainly

```
GOLD: 1 lot = 100 oz, minimum 0.01 lot = 1 oz
stop = 2.5 × ATR-14(15m);  ATR ≈ 7  →  stop ≈ $17.50 per oz
minimum position risk  = $17.50
current equity         = $155.45
                       = 11.3% of equity per trade
```

At a 56% win rate an eight-loss run is an ordinary occurrence, not a tail. Eight losses at
11.3% compounds to about **38% of starting equity**. The rule fires ~2 trades/week inside the
two windows, so a run like that is a normal month.

The minimum lot is a hard floor — it cannot be sized down. So there are three options and the
implementer should ask which is wanted rather than assume:

| option | equity needed | risk per trade |
|---|---|---|
| fund to 1% risk | ~$1,750 | $17.50 |
| fund to 2% risk | ~$875 | $17.50 |
| trade at current equity | $155 | **11.3%** — accepted as tuition, not as a strategy |

**Whatever is chosen, `risk_pct` goes in the config and the executor refuses to send an order
that exceeds it.** If equity is too small for the minimum lot at the configured risk, the
correct behaviour is to **log and skip the signal**, not to send a smaller-than-minimum order
(which the broker rejects) and not to widen the stop (which breaks the rule).

---

## 4. Components to build

Nothing below needs a language model.

### A. `data/mt5_rates.py` — bar ingestion (new)

* `copy_rates_range` in chunks; `copy_rates_from_pos` returns empty above ~60k bars.
* Convert `time` (server epoch, UTC+3) → true UTC epoch ms **once, at the boundary**.
* Write into `candles` under a distinct symbol key — `MT5:GOLD` — so Bybit `XAUUSDT` rows are
  never mixed with broker rows in the same panel.
* Map `tick_volume` → `volume`, and record `real_volume` is unavailable.
* Idempotent and resumable, same contract as `data/sync.py`.
* CLI: `cli.py sync-mt5-rates --symbol GOLD --tf 1m,5m,15m,30m,1h,4h --from ...`

### B. `engine/live_signal.py` — the decision function (new)

* On each closed anchor bar, evaluate `ENTRY_RULES.md` §4 legs 1–9 plus §11 window gate.
* Reuse `engine/signals.py`; do not re-implement the rule.
* Emit one JSON line per evaluation to `reports/signals.jsonl`:
  `{ts_utc, symbol, anchor, fired, side, legs:{...}, entry_ref, atr_entry, sl, tp1, trail,
    window, reason_if_skipped}`
* **Closed bars only.** The forming bar never produces a signal (`watch.py` docstring).
* This file must run and log identically whether or not execution is enabled.

### C. `exec/mt5_exec.py` — the executor (new, the only file that can send orders)

`data/mt5_live.py` is **read-only by construction** (its line 14 says so). Do not add
`order_send` there — keep the read path unable to trade.

* Preflight, every cycle, refuse to trade if any fails: `terminal_info().trade_allowed`,
  `account_info().trade_expert`, `symbol_info(sym).trade_mode` allows trading, spread ≤
  `max_spread_bps`, free margin sufficient, DB not stale.
* Send entry and stop **in one `order_send`** with `sl=` populated. Never send an order and
  attach the stop afterwards — that window is exactly how the four ETH shorts ended up naked.
* `tp` left empty; the partial and trail are managed by the executor (§D), not the broker.
* Use the symbol's `filling_mode`; retry once on requote; log every request and result.

### D. Position management (in the executor)

Implements `ENTRY_RULES.md` §5 option B, mechanically:

1. stop = `entry ∓ 2.5 × ATR_at_entry`, **frozen** — never widened, for any reason
2. at `entry ± 2.5 × ATR_at_entry`: close 50%, move the broker stop to breakeven
3. thereafter: chandelier at `peak ∓ 2.5 × ATR_at_entry`, ratchet only, modified on the broker
4. no take-profit ceiling — this is what "let a one-way move run" means in code

`tools/advise.py` already computes every one of these levels from `broker_trades` and live
indicators; reuse its functions rather than duplicating the arithmetic.

### E. Reconciler

Every cycle compare intended state to broker state: any open position with **no stop**, or a
stop that disagrees with the rule by more than 0.25 ATR, is a **P1 alert** and — if
`auto_repair: true` — the stop is set to the rule's level immediately.

### F. Notifier

Push on: entry filled, partial filled, stop moved to breakeven, exit, any P1 alert, executor
halted. Telegram bot is the least friction; email is fine.

### G. LLM brief — optional, and last

Twice a day at 05:50 and 18:50 MYT: read the app export + `advise.py --json` +
`signals.jsonl` tail → one short brief. **Read-only. It has no order path.** Its job is what
the rule cannot see: news, the weekend gap, unusual spread, position concentration.

---

## 4a. DST BUG — found and fixed 2026-08-23, §4b re-run below

`data/mt5_rates.py` converted server time with **a fixed +3**, measured once in August. XM
runs **EET/EEST** on the EU rule, so every bar from late October to late March was stamped
**one hour early**. `data/mt5_live.py` had the identical defect on the trade history.

Three independent confirmations before anything was changed:

1. **Bybit cross-check.** `MT5:GOLD` against `XAUUSDT` at matching UTC timestamps. Bybit's
   history starts 2026-03-09 and EU DST began 2026-03-29, giving a three-week winter window:
   bar-to-bar scatter (MAD) was **10.72 before the transition and 1.14 after**, and shifting
   the winter bars forward one hour collapsed it to 2.13.
2. **Known H4 bars.** Server `2026-08-21 20:00` must be 17:00 UTC (+3) and
   `2026-01-15 20:00` must be 18:00 UTC (+2). The stored data had both at +3.
3. **Season signature.** Stored H4 opens were `[1,5,9,13,17,21]` UTC in **both** July and
   January. A UTC+2 server produces `[2,6,10,14,18,22]`. Identical hours across the year is
   the fingerprint of a fixed-offset conversion.

**Fix:** `data/broker_clock.py` localises to `Europe/Athens` with
`ambiguous="raise", nonexistent="raise"` rather than adding an integer. The autumn fold and
spring gap both fall on a Sunday when `GOLD` is shut, so no bar should land in either — the
raise turns that expectation into a check. Across 25 years and six timeframes it never fired.

⚠️ One bug found while fixing it: `pd.to_datetime(..., unit="s")` yields `datetime64[s]` in
this pandas version, so `.view("int64")` is already **seconds**, not nanoseconds. Dividing by
a hardcoded 1e6 silently produced 1970 timestamps. The conversion now casts to an explicit
`datetime64[ms]` instead of assuming a resolution.

**Verification after the fix:** H4 hours now `[1,5,9,13,17,21]` in summer and
`[2,6,10,14,18,22]` in winter; winter MAD 10.72 → **2.13**; and against the parallel
session's independently written DST-corrected `MT5GOLD` table, **99,844 overlapping bars with
max |close difference| 0.0000** — two separate implementations agreeing on every timestamp.

### §4b re-run — old vs new

| | fixed +3 (wrong) | Europe/Athens (correct) |
|---|---|---|
| pooled n | 1,348 | **1,349** |
| pooled win% | 53.5% | **53.4%** |
| 95% CI | 50.8–56.1 | 50.8–56.1 |
| break-even | 51.3% | 51.3% |
| **edge** | **+2.2** | **+2.1** |
| net / unit | +1,293.88 | +1,286.44 |
| expectancy | +0.960 | +0.954 |
| profit factor | 1.20 | 1.19 |
| long | 796 / 55.3% | 797 / 55.2% |
| short | 552 / 50.9% | 552 / 50.9% |
| bracket grid | 9/9 net positive | 9/9 net positive |

By year — 2022, 2023 and 2025 are **identical**; 2024 and 2026 move by one trade:

| year | old | new |
|---|---|---|
| 2022 | 183 / 53.6% / +55.98 | 183 / 53.6% / +55.98 |
| 2023 | 325 / 49.2% / −64.19 | 325 / 49.2% / −64.19 |
| 2024 | 318 / 52.8% / +138.68 | 319 / 52.7% / +130.06 |
| 2025 | 348 / 57.5% / +650.75 | 348 / 57.5% / +650.75 |
| 2026 | 174 / 54.6% / +512.67 | 174 / 54.6% / +513.84 |

**Why the headline barely moved, and why that is the correct outcome.** The bug relabelled
bars; it did not reorder or alter them. Every MT5 timeframe shifted by the same hour together,
so a 4H bar still covers exactly the same sixteen 15m bars and the multi-timeframe alignment
the rule depends on is untouched. Only things reading an **absolute wall clock** could change:
the `gold_session` boundary (Fri 22:00 / Sun 21:00 UTC) caught a slightly different set of
bars, which is the entire difference.

### The one result that did move: the §11 window gate

| | old | new |
|---|---|---|
| n | 738 | **708** |
| win% | 54.2% | **53.1%** |
| break-even | 51.4% | 51.4% |
| **edge** | **+2.8** | **+1.7** |

The window gate is defined in **MYT hours**, so it is exactly the measurement a one-hour
label error corrupts — and it moved four times as much as the all-day panel.

**With the clock corrected, the windows are now WORSE than trading all day: +1.7 against
+2.1.** Before the fix they looked better. §11's case for restricting hours was measured on
n=110/55 and is not supported here on n=1,349/708, on either side of the fix. The owner's
instruction to trade all day (§C4) is the right call, and this is now a third independent
measurement agreeing with it.

---

## 4b. GATE §5.1 RESULT — measured 2026-08-23, re-run after the DST fix

**Superseded numbers:** §4b originally reported the pre-fix figures. The table in §4a above
is authoritative; the conclusion below is unchanged.



Built: `data/mt5_rates.py` (§A), the rule's missing legs in `engine/signals.py`
(`htf_{5m,15m,30m}_ut_*`, `htf_4h_ema_stack_*`, `mtf_dip_*`, `mtf_reclaim_*`), and
`tools/validate_rule.py` — a permanent harness implementing `ENTRY_RULES.md` §6 exactly.

**The harness was validated before it was trusted.** Run against Bybit `XAUUSDT` 15m it
reproduces `ENTRY_RULES.md` to the decimal: **n=112, pooled 67.0%, long 70.5 / short 64.7**.
It did not at first — 53.0% on n=149 — because the `gold_session` filter was missing and
timeouts were priced at the bar extreme instead of its close. A validation harness that
disagrees with the study it is validating is worthless until that is explained.

### The panel

MT5 history depth is per timeframe and set by the broker (terminal `maxbars` 100,000):

| TF | bars | back to |
|---|---|---|
| 1m | 100,000 | 2026-05-12 |
| 5m | 99,468 | 2025-03-25 |
| **15m** | **99,816** | **2022-05-30** |
| 30m | 99,920 | 2018-03-01 |
| 1h | 81,907 | **2001-06-03** |
| 4h | 23,613 | 2001-06-03 |

⚠️ **Leg 5 (the 5m UT bias) caps the panel at 5m depth.** An HTF column with no data cannot
equal `BIAS_BULLISH`, so every signal before 2025-03-25 dies silently and the run reports a
shorter panel with no error. §4.3 already calls leg 5 optional; dropping it buys 2.8 more
years. The harness now warns by name when a leg truncates a panel.

### Result — `MT5:GOLD` 15m, 2022-05-30 → 2026-08-21, 5m leg off, cost 0.85 bps

| | n | win% | 95% CI | net/unit | expectancy | PF |
|---|---|---|---|---|---|---|
| **pooled** | **1,349** | **53.4%** | **50.8–56.1** | +1,286.44 | +0.954 | 1.19 |
| long | 797 | 55.2% | 51.7–58.6 | +945.11 | +1.186 | 1.25 |
| short | 552 | 50.9% | 46.7–55.1 | +341.33 | +0.618 | 1.12 |

Break-even **51.3%**. Null 49.8%. (Timestamps DST-corrected — see §4a.)

**Read this carefully.**

* It **clears break-even by +2.1 points on 1,349 trades** — the first time this rule has
  cleared on a large sample. On Bybit the same rule needed 61–64% and the big PAXG panel
  returned 56.6% against a 65.7% break-even, i.e. clearly negative. **The cost finding in §1
  was the whole story, and it was right.**
* **But the 95% CI lower bound (50.8) sits below break-even (51.3).** This does not clear at
  p<0.05. It is a positive point estimate with an interval that includes failure.
* **The short side is at break-even** — 50.9% against 51.3%. The rule's edge is a long-side
  edge on this instrument, which is what the owner's own record independently says
  (manual: long +4,210, short −636).

### By year — and it settles an open question

| year | n | win% | net |
|---|---|---|---|
| 2022 | 183 | 53.6% | +55.98 |
| 2023 | 325 | **49.2%** | **−64.19** |
| 2024 | 319 | 52.7% | +130.06 |
| 2025 | 348 | 57.5% | +650.75 |
| 2026 | 174 | 54.6% | +513.84 |

`ENTRY_RULES.md` §6 offered two explanations for PAXG's era ramp (2022 39.9% → 2026 58.6%) —
**liquidity** or **regime** — and said the data could not separate them. This panel can.
`GOLD` was a deep, liquid book throughout, and its ramp is far flatter: 49–58% rather than
40–59%. **The liquidity explanation carries most of it.** What remains is a real but mild
regime effect: 2023 is the one negative year, and 2023 gold was range-bound — a trend
rule losing money in a range, exactly as §6 predicted.

### The bracket plateau holds

Pooled win% / net per unit, ~1,300 trades per cell:

| | TP2.0 | TP2.5 | TP3.0 |
|---|---|---|---|
| **SL2.0** | 52.6% / +841 | 47.5% / +1037 | 42.9% / +1034 |
| **SL2.5** | 58.3% / +1109 | 53.5% / +1294 | 49.4% / +1418 |
| **SL3.0** | 61.8% / +1064 | 57.0% / +1207 | 53.0% / +1257 |

**Nine of nine cells net positive** on a four-year sample. Compare `ANALYSIS_BRIEF.md` §7.3,
where all twelve trail-sweep cells lost — that is what a dead surface looks like, and this is
not one. The configured 2.5/2.5 is not the peak; TP3.0/SL2.5 is.

### Windows (§11) — the direction holds, the magnitude does not

| | n | win% | break-even | edge |
|---|---|---|---|---|
| all day | 1,349 | 53.4% | 51.3% | **+2.1** |
| 06–09 + 19–22 MYT | 708 | 53.1% | 51.4% | **+1.7** |

§11 measured +6.5 → +8.4 on n=110/55. On the corrected panel it is **+2.1 → +1.7 — the
windows are worse than trading all day.** Before the DST fix they looked better (+2.2 → +2.8),
which is what a one-hour label error does to a gate defined in MYT hours. See §4a.
Do not implement the window restriction.

### Verdict on the gate

**Passed, conditionally.** The edge survives the feed change and clears break-even on the
largest and most relevant sample the project has assembled. It does not clear at p<0.05, the
short side does not clear at all, and one year in five is negative.

**What that means for sizing is unchanged and is now the binding issue** — see §3. At
+$0.96 per trade per ounce and ~320 trades a year, minimum size earns roughly **$307/year**
while risking **11.3% of a $155 account per trade**. At 53.5% an eight-loss run is ordinary;
eight losses at 11.3% is a **61% drawdown**. The edge is real enough to be worth trading and
far too thin to survive that sizing. **Fund the account or do not run it.**

---

## 5. The gate: what must pass before `execute: true`

1. **Re-validate on MT5 bars.** Rebuild the panels from `MT5:GOLD` (§A) and re-run the
   `ENTRY_RULES.md` §1 and §11 measurements. Report pooled long+short win rate against the
   49.8% null and against the **MT5** break-even from §1. If the edge does not survive the
   feed change, stop here and say so.
2. **Spread study.** One week of per-minute spread, reported as a distribution and separately
   for 06:00–09:00 and 19:00–22:00 MYT. The break-even in §1 is only as good as this number.
3. **Paper run.** `execute: false` with full logging, ≥ 30 signals, and the live signal list
   reconciled bar-for-bar against a backtest over the same period. **They must match
   exactly.** Any mismatch is a bug in the live path, not noise.
4. **Guard tests** (§6) all pass in a dry-run harness.

Only then flip `execute: true`, and start at the smallest size the account allows.

---

## 6. Guards — non-negotiable, each one a test

| guard | behaviour |
|---|---|
| **stop with entry** | order rejected by our own code if `sl` is empty |
| **max concurrent** | default **1** per symbol. The current account holds four same-direction ETH shorts = 4× intended risk |
| **one symbol** | `GOLD` only. Owner's own record: XAU **+3,682.74** over 348 trades, ETH −123.41 over 14, BTC −215.90 over 5 |
| **daily loss limit** | halt until next session on breach |
| **consecutive-loss halt** | halt after N losses (default 5) pending manual resume |
| **window gate** | new positions only 06:00–09:00 and 19:00–22:00 MYT, and the **fill** must land inside the window (`ENTRY_RULES.md` §11.6) |
| **max spread** | skip the signal if spread > `max_spread_bps` at send time |
| **stale data** | halt if the newest closed bar is older than 2 × the anchor interval |
| **kill switch** | a file `reports/HALT` — present means flatten nothing, open nothing. Checked every cycle, before anything else |
| **never widen a stop** | stops ratchet toward profit only |
| **restart safety** | on start, adopt existing positions from `broker_trades` and recompute their levels; never open a duplicate |

---

## 7. Config sketch

```yaml
automation:
  symbol: GOLD                  # MT5 symbol; data key MT5:GOLD
  anchor: 15m
  execute: false                # the gate in §5 flips this
  risk_pct: 1.0                 # of equity; skip the signal if min lot exceeds it
  max_concurrent: 1
  daily_loss_limit_pct: 3.0
  consecutive_loss_halt: 5
  max_spread_bps: 4.0
  windows_myt: ["06:00-09:00", "19:00-22:00"]
  fill_must_be_in_window: true
  exit:
    stop_atr: 2.5
    partial_at_atr: 2.5
    partial_fraction: 0.5
    then_stop: breakeven
    trail_atr: 2.5
  notify: { telegram_chat_id: "...", on: [entry, partial, breakeven, exit, alert, halt] }
```

---

## 8. Runbook

* **Enable AutoTrading in the terminal** — `trade_allowed` is currently `False` and
  `order_send` fails silently-ish without it.
* Terminal must stay logged in; Windows power settings must not sleep the machine.
* Run the loop as a Windows scheduled task at boot with restart-on-failure.
* `reports/HALT` is the panic button — create the file from any device with file sync.
* Weekly: reconcile `broker_trades` against `signals.jsonl` and confirm every fill traces to
  a logged signal. **A fill with no signal means something other than the rule is trading.**

---

## 9. Acceptance tests

1. Live signal list == backtest signal list over the same window, bar for bar.
2. Every `order_send` in the log carries a non-empty `sl`.
3. Killing the process mid-trade and restarting adopts the position and recomputes the same
   levels; no duplicate order.
4. `reports/HALT` blocks a signal that would otherwise have fired.
5. A synthetic spread spike above `max_spread_bps` skips the signal.
6. A signal whose fill would land outside the window is skipped.
7. Server-time conversion: a known H4 bar's stored `open_time` equals the true UTC instant.
8. Daily-loss and consecutive-loss halts trigger at the configured thresholds.

---

## 10. Build order

1. §A MT5 rates + §2.1 time conversion + test 7
2. §5.1 re-validation — **decision point; if the edge dies here, stop**
3. §B signal engine + `signals.jsonl` + test 1
4. §5.2 spread study (can run in parallel from step 1)
5. §F notifier — value before any execution exists
6. §C/§D/§E executor, `execute: false`, tests 2–8
7. §5.3 paper run, ≥ 30 signals
8. flip `execute: true` at minimum size
9. §G LLM brief, if still wanted

---

# GATE §5.1 RESULT — run 2026-08-23. **The gate does not pass as specified.**

Seven agents: MT5 ingestion, three measurement jobs, three adversarial lenses.
**Data lens passed. Edge lens refuted (high). Economics lens refuted (high).**

Everything below supersedes the corresponding section above. Three of the errors are mine.

## G1. The edge does survive the feed change — but only on the long side

Rebuilt from MT5 GOLD bars (`MT5GOLD`, DST-corrected), priced at the broker's own per-bar
spread rather than an exchange fee:

| panel | n | pooled | long | short | break-even | edge |
|---|---|---|---|---|---|---|
| 15m, all-day, 2025-03→2026-08 | 393 | 56.7% | **59.4** | 52.1 | 50.9 | +5.8 |
| 1h, all-day | 166 | 58.4% | **63.3** | 51.5 | 50.4 | +8.0 |
| 15m, 4.2 y, 5m leg off | 1,410 | 55.6% | — | 52.9 | 51.7 | +3.9 |
| 13 y skeleton | 2,237 | 53.0% | — | 50.8 | 51.6 | +1.4 |

Break-even moves **64.3% → 50.9%** (15m) and **56.6% → 50.4%** (1h). Cost/ATR falls 16×.
That part of §1 is confirmed and is the largest effect in the project.

**But the short side clears break-even nowhere, at any depth.** 15m short 52.1 vs 50.9
(p=0.387); 1h 51.5 vs 50.4 (p=0.428); 4.2 y 52.9 vs 51.7 (p=0.282). On the two deepest
samples it is *below* break-even. The long side is significant everywhere (15m p=0.0037).

`ENTRY_RULES.md` §3 makes "a one-sided lift is not an edge" non-negotiable, so by the
project's own standard this is **a long-only system or it is nothing**. Do not ship the
mirrored short leg on the strength of these numbers.

## G2. ⛔ At $150–200 the executor takes **zero** trades — this is the blocking finding

GOLD minimum is 0.01 lot = 1 oz, so a position risks exactly `2.5 × ATR` dollars and cannot
be sized down. Median stop over the trades actually taken: **$17.25** (15m), **$37.08** (1h).
On 2026 trades only the 15m median is **$24.68**.

At $150 or $200, with a 1% or 2% cap, on both anchors, all-day or windowed:
**skip rate 100.0%.** The edge is not degraded at this size — it is unreachable.

**§3's funding table above is wrong and I am correcting it.** "$875 → 2% risk" funds only the
*median* trade; 49.4% of 15m signals still exceed a $17.50 cap.

| goal | equity needed |
|---|---|
| cover the median signal at 2% | $875 (≈49% of signals still skipped) |
| **cover the full 15m signal list at 2%** | **$7,959** |
| cover the full list at 1% | $15,918 |
| trade at $200 with ~12–16% risk/trade | possible, and is a tuition bet, not risk control |

## G3. Three corrections to this document

**§2.1 was wrong.** The XM server is **EET/EEST — UTC+2 in winter, UTC+3 in summer**, on the
EU rule. My constant "+3" came from a reading taken in August. Proven four independent ways,
including a week-close test needing no reference instrument (213/213 clean weeks) and the 15
weeks where US DST is on but EU DST is not — all imply +2, ruling out both a US-rule server
and a fixed +3. So the H4 UTC grid is `21/01/05/09/13/17` in summer and `22/02/06/10/14/18`
in winter; §2.1 documented only the first.

⚠️ **`data/mt5_rates.py` therefore writes corrupted data.** It applies one live-measured
constant offset, so every bar from late October to late March is stamped an hour early. On
the existing `MT5:GOLD` table the 15m/1h rows share ~97% of timestamps with the corrected
series but the close **differs on ~38% of them** — winter bars carry another bar's OHLC.
Any result computed on `MT5:GOLD` needs re-running.

**§11.7 of `ENTRY_RULES.md` gave the wrong reason for preferring big targets.** A round-trip
cost enters mean R as `−c/(SL·ATR)`, a constant for every TP, so a cost change cannot reorder
the TP ranking — only shift the column. Demonstrated on identical trades: mean R is monotone
increasing in TP under both cost models, best-by-PF is TP5.0/SL2.5 under both. The
preference is a property of **gold's continuation**, not of the fee. The advice ("stop
cutting winners at 1.5–2.0 ATR") stands; the explanation was wrong.

**§2.2 overstated.** `real_volume` is 0 on 1m–30m but non-zero on 3.6% of 1h and 3.3% of 4h
bars. Volume is still tick count everywhere in practice.

## G4. What the MT5 data says the configuration should be

Two changes are large, replicate across both anchors, and point the opposite way to the
Bybit-era conclusions. **Both need the adversarial treatment of `FEATURE_MINE.md` §4 before
being trusted** — three findings died at exactly this stage before.

| change | Bybit said | MT5 says |
|---|---|---|
| **drop the dip/reclaim event (legs 7–9)** | −1.2 to −9.1, keep it | ⛔ **RETRACTED — see `FEATURE_MINE.md` §11.1.** The +10.1-vs-+5.8 comparison ran two separate `simulate()` streams sharing only 199 of 393 trades. Partitioning one trade list at entry gives dip-on 60.4% vs dip-off 61.2%, **z = −0.20, p = 0.84** — the legs are inert. **Keep them.** |
| **the §11 window gate** | +8.4 edge | **does not replicate** — windows are slightly *worse* than all-day (56.3 vs 56.7); window A is the weak half |
| best fixed bracket | TP 2.5/SL 2.5 | **TP 5.0 / SL 2.5** — edge +11.4, PF 1.79 |

More trades *and* more edge from dropping a leg is the opposite of everything the Bybit work
found. Note §11's window advantage was partly computed on the DST-corrupted timestamps, which
is the likeliest explanation for it evaporating.

## G5. Panel limits that bound all of the above

* The **5m series caps both panels** — the broker serves ~100k bars per timeframe, so 15m
  reaches 2022-05 but 5m only 2025-03. Labels exist on 33% of 15m rows. n=393, not n=1,348.
* **Pre-2013 H1/H4 are daily bars mislabelled** — 3,036 rows sit 24h apart. §1's "H1 back to
  2001" is a sparse daily backfill; any indicator warmed across it is garbage.
* MT5 GOLD is **12–24% more volatile per bar** than XAUUSDT/PAXG in a matched window — ATR
  thresholds transfer approximately, ratios of ATRs shift ~12%.
* Spread in bps **falls** over time (1.43 → 0.88) as gold rose; the panel uses each bar's own
  spread, not a constant.

## G6. Revised build order

1. Fix `data/mt5_rates.py` to use a DST-aware conversion, re-ingest, discard `MT5:GOLD`.
2. Decide the funding question in G2. **Nothing downstream matters until this is answered** —
   at $200 the specified executor is a no-op.
3. Adversarially verify the two G4 changes before adopting them.
4. Re-run the gate long-only, and decide explicitly whether a long-only gold bot is the
   intended product.
5. Then, and only then, §4's components.
