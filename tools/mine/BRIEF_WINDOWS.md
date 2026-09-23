# BRIEF_WINDOWS.md — addendum. Read BRIEF.md first, then this.

Everything in `BRIEF.md` still holds: the panels, the tool API, the 49.8% pooled null, the
rules of evidence. This file changes **what is being asked** and adds three rules that the
previous pass learned the hard way.

## The constraint

The trader can only **open new positions** inside two windows, in MYT (= UTC+8):

* **Window A — 06:00-09:00 MYT = 22:00-01:00 UTC.** NY close rolling into the Asia open.
* **Window B — 19:00-22:00 MYT = 11:00-14:00 UTC.** London PM rolling into the NY open
  (NY opens 13:30 UTC = 21:30 MYT, i.e. *inside* window B).

Exits are unconstrained — a position opened in a window may close whenever the exit says.

`p.window("A")`, `p.window("B")`, `p.window("AB")`, `p.window("OUT")` return the masks.
Use them; do not re-derive from `hour_myt`.

**This window is exogenous.** It is the trader's availability, not something the search
discovered, so it carries **no multiple-testing cost** and does not need to justify itself.
Do not spend effort testing whether these are the *best* hours — that question is not being
asked and the answer would not change the constraint.

## What the two windows actually are — measured, XAUUSDT 15m, in-session

| window | bars | % of session | ATR% (median) | turnover/bar | relative volume |
|---|---|---|---|---|---|
| A 06-09 MYT | 1,428 | 12.3% | **0.1430** | $370,266 | 0.93 |
| B 19-22 MYT | 1,440 | 12.4% | **0.1922** | **$807,581** | **1.19** |
| rest of day | 8,725 | 75.3% | 0.1883 | $410,819 | 0.70 |

They are **not the same kind of market** and should not be pooled by default:

* **B is the prime window** — the highest-participation three hours of the gold day, 2.2x
  window A's turnover, the highest ATR%, and where the daily range is typically established.
* **A is the quiet window** — the lowest ATR% of the three groups, participation at 0.93.

The volatility gap has a direct economic consequence, and it is the single most important
number in this brief. Cost is a fixed **0.11% of notional**, so a lower ATR% means a higher
break-even win rate. Measured on XAUUSDT at a 2.5/2.5 ATR bracket:

| window | break-even WR | base rule WR | edge |
|---|---|---|---|
| A | **65.7%** | 69.7 (n=33) | +4.0 |
| B | **60.8%** | 78.3 (n=23) | **+17.5** |
| A+B | 64.3% | 72.7 (n=55) | +8.4 |

**Window B starts ~5 points ahead of window A before any rule is applied.** A rule that
looks equally good in both windows is therefore *better* in B. Report the two windows
separately, always, and only pool them when you have shown they behave the same.

## Sample — read this before designing anything

Base-rule trades available inside A+B:

| panel | all day | A only | B only | A+B | usable for |
|---|---|---|---|---|---|
| XAUUSDT 15m (166d) | 110 | 33 | 23 | **55** | confirmation only — never mine here |
| PAXGUSDT 15m 2025-26 | 482 | 128 | 118 | **244** | mining |
| PAXGUSDT 15m 2022-24 | 867 | 187 | 202 | **379** | hostile tripwire |
| PAXGUSDT 1h 2025-26 | 237 | 56 | 52 | **104** | mining, thin |

**Mine on PAXG, confirm on XAU.** With n=23 in window B on the live instrument, any
XAU-only result is uninterpretable. If a finding needs XAU to be true, it is not a finding.

Also note what the constraint already does on its own, before any new rule: on PAXG 1h it
moves the base rule from **-0.3 edge (all day) to +4.2 (A+B)**. On PAXG 15m it does nothing
(-9.1 to -9.3). On the hostile panel it makes things *worse* (-23.9 to -28.7), which is the
correct tripwire direction.

## What to look for — the archetypes, and why

The previous pass mined features while pooling every hour of the day. That pass is
`FEATURE_MINE.md` and it found nothing. **Do not repeat it inside the windows.** What is
genuinely new here is that A and B are different market states, so a *different archetype*
may be right in each. That is the hypothesis space worth spending on:

* **Window B is where trends are born.** `ut_cross_up` / `ut_cross_down` were dead when
  measured across all hours (39.0% pooled, below the null) — but a fresh UT cross at the NY
  open is a different event from one at 03:00 UTC. Re-test the cross *inside window B only*.
  Same for breakout archetypes: an opening-range break of the 11:00-13:30 UTC range, taken
  at the NY open, has a mechanism that does not exist at other hours.
* **Window A is quiet and may not be a trend window at all.** Test whether continuation
  degrades there and whether the opposite archetype — fading an extreme, mean reversion to
  VWAP or EMA-28, range behaviour — does better. If A only works with a different rule than
  B, say so plainly; two rules is an acceptable answer.
* **Relaxation for frequency.** The base rule inside A+B fires 0.33/day on XAU — one trade
  every three days. If window B is itself a quality filter, 4-of-5 timeframe agreement may
  be enough there, buying back trade count at little cost. `n_bull`/`n_bear` give this
  directly. **This is the most commercially useful question in the brief** — the trader
  asked for frequency and the constraint has just cut it by 60%.
* **Hold-time interaction.** A trade opened at 21:00 MYT runs straight into the NY session;
  one opened at 08:00 MYT runs into the Asia lull. Average hold is 2.5-5.7 hours, so the
  window determines what the trade is held *through*. Test whether the exit should differ by
  entry window.
* **Direction asymmetry per window.** Report `wrL` and `wrS` per window. A window-specific
  directional bias is plausible (fixings, NY-open flow) and would be a real finding — but it
  must appear on both PAXG eras, not just the recent one.

## Three rules the previous pass learned the hard way

These killed all three of its finalists. They are not optional.

1. **Score an additive filter against the base rule's own other trades *inside the same
   window*, never against the 49.8% null.** The null answers "does this contain directional
   information". It does not answer "does this improve my rule", because the rule is already
   at 68%. Getting this wrong took a z of 4.46 down to 2.82 once corrected.
2. **Print the full 2x2, not the marginals.** If leg A alone and leg B alone are each *below*
   base while A&B is far above it, you have selected a lucky corner of a 4-cell partition,
   not found a mechanism. Genuinely independent filters produce main effects. Report the
   four exclusive cells for every pair you propose.
3. **Perturb every threshold by ±30%.** If nothing changes — same n, same win rate — the leg
   is *inert*, not robust. One previous finalist's two extra legs were already implied by
   its first leg and deleted three trades between them.

And: **break-even is now computed from the ATR of the trades actually taken**, not the panel
median (`screen.py` `simulate`). This matters here specifically — the old panel-level figure
understated window A's break-even by about 4 points. If you cached numbers from an earlier
session, re-run them.

## Reporting

Same schema as before. In addition, for every survivor state:

* the result **separately for window A and window B**, never only pooled
* `per_day` and trades-per-week inside the windows — the constraint has already cut
  frequency by 60% and a rule that fires monthly is not usable however good it looks
* the 2x2 cells for any pair
* whether it needs a different rule in A than in B
