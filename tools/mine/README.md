# tools/mine — the measurement toolkit

Rescued from the session scratchpad, which has been wiped once already. The
`panel_*.pkl` files are **not** here — they are ~1.3 GB and are rebuilt by
`build.py` in minutes.

Six mining passes (4,000+ conditions, 24+ adversarial verifications) ran on this
toolkit and rejected six candidates. `../../docs/FEATURE_MINE.md` is the record.
**Read `BRIEF.md` before using any of it** — it carries the measurement standard,
and the standard is the reason the results are trustworthy.

## Rebuild the panels

```
PY=..\..\.venv\Scripts\python.exe

# Bybit panels (from the candles already in market.db)
$PY build.py XAUUSDT  15m 1m 2880
$PY build.py PAXGUSDT 15m 5m 576
$PY build.py BTCUSDT  15m 1m 2880

# MT5 broker panel — ingest first. NOTE the DST bug in the repo's own
# data/mt5_rates.py (docs/AUTOMATION.md G3); mt5_ingest.py here is DST-aware.
$PY mt5_ingest.py --prove          # re-runs the clock proofs, writes nothing
$PY mt5_ingest.py
$PY build.py MT5GOLD 15m 5m 576

# causality is machine-checked, not asserted
$PY build.py --check-causal MT5GOLD 15m
```

## The pieces

| file | what |
|---|---|
| `BRIEF.md` | the measurement standard — the 49.8% pooled null, non-overlapping trades, cross-panel replication, the rules of evidence |
| `BRIEF_WINDOWS.md` | addendum for the two MYT trading windows |
| `build.py` | panel builder: 496 columns, first-touch outcome labels, `--check-causal` |
| `feats.py` | the derived features (UT flip timing, divergence, geometry, slopes) |
| `screen.py` | `Panel` / `simulate` / `screen` / `bins` — everything measures through this |
| `exits.py` | two-stage exit evaluator (stop / partial / chandelier) |
| `mt5_ingest.py` | DST-aware MT5 ingestion with four independent clock proofs |
| `streak.py`, `target.py` | Monte Carlo: ruin and target-reaching at a given win rate and size |
| the rest | one-off studies kept for their method, not their numbers |

## The four ways a finding dies here

Every one of these killed a real candidate. Check all four before believing anything.

1. **Wrong comparator.** An additive filter is scored against the rule's *own other
   trades*, never the 49.8% null. Cost one finding 1.6 z; made another inert.
2. **A selected corner.** Print the full 2×2. Both legs below baseline while the
   pair is far above it means a lucky quadrant, not a mechanism.
3. **Best-of-K.** Generate hundreds of random rules of the same construction and
   ask where the finding lands. One candidate sat at P(best-of-181) = 0.913.
4. **Two-simulation artifact.** `simulate()` takes non-overlapping trades, so a
   filter and its complement do **not** partition the parent's trade list —
   splitting the stream releases trades the parent never takes. Partition the
   parent's actual trade list at entry. This killed two candidates.

Plus, for anything that will be traded on a small account:

5. **Affordability selection.** A rule that prefers high-ATR bars selects trades a
   small account cannot take. Report the median stop against the parent's.

---

## The three scripts that decided the shipping config (2026-08-23)

Run with the repo venv from `C:\inetpub\Claude\ITSupport`. All three import
`tools/validate_rule.py` so there is exactly one definition of the rule.

| script | question it answers |
|---|---|
| `bracket_ruin.py` | which TP/SL, judged by **P(ruin) at a fixed 0.01 lot**, not by net |
| `null_longonly.py` | how much of a long-only result is the rule and how much is gold's drift |
| `byyear.py` | what the **real trade order** does to $200 - the bootstrap hides 2022-23 |

Two traps they exist to avoid, both of which changed the answer:

* **Raw dollars across the panel are not comparable.** Gold went 1,800 -> 4,300, so a 2022
  trade pays a 2022-sized amount. All three re-express each trade as R (`net/atr`) and reprice
  at 2026's median signal ATR before any dollar figure is computed.
* **A random long clears break-even in a bull market.** Break-even is the right comparator for
  a two-sided rule at a symmetric bracket and the wrong one for a long-only rule. The matched
  random-entry null in `null_longonly.py` is the sixth failure mode to add to the five below.

`--path-tf 15m` must be passed to `validate_rule`. The 1m series only reaches 2026-05-12 and
the harness auto-picks the finest available, silently cutting the panel from 1,349 to 76.
