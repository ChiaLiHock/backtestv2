# docs/ — index

Read in this order.

| # | File | What it is | Read it when |
|---|---|---|---|
| 1 | **OPERATING_PLAN.md** | What was decided and what it is worth — the fixed-0.01-lot ruin arithmetic, the owner's own 367-trade baseline, the four findings usable without a bot, and current build status | **Picking this up fresh — start here** |
| 2 | **ANALYSIS_BRIEF.md** | The briefing: what the system is, what every number means, what has been tested, **what has already been disproved** | You are being asked "where should we optimise?" |
| 3 | **ENTRY_RULES.md** | The decided trigger specification, measured on PAXG + XAU + BTC + ETH, and what the engine still needs to run it | You are about to change the entry or exit |
| 4 | **AUTOMATION.md** | The build spec for running the rule unattended on MT5 — feed decision, guards, gates, sizing. **§4b holds the gate §5.1 result; the tail holds its adversarial verdict and three corrections** | You are building or reviewing the execution path |
| 4b | **LIVE_MODE.md** | The always-on page: no backtest in the loop, the RiskExecutiveEngine port, the gate that can only veto, and the three buttons | You are running `watch.bat` and want to know what it is doing |
| 5 | **FEATURE_MINE.md** | Six mining passes, 4,000+ conditions, 24+ adversarial checks, **six candidates rejected** — plus the five ways a finding dies here and the ~56% standing null | You are about to propose a new entry filter |
| 6 | **HANDOVER.md** | Session state, environment gotchas, every bug found and why it mattered, open questions | Something breaks |
| 7 | **../PROGRESS.md** | Chronological record of each phase | You want the history |
| 8 | **INDICATORS.md** | Every indicator's formula with `file:line` back to the Kotlin, warm-up index, seed value, repaint verdict | You need to know exactly how something is computed |
| 9 | **SIGNAL_MAP.md** | The signal vocabulary — implemented vs documented-but-not-built | You are writing a strategy config |
| 10 | **OPEN_QUESTIONS.md** | Every ambiguity found in the app, the decision taken, and why | You want to know why something was done a certain way |
| — | **../README.md** | How to run everything — the live chart, the measure tool, the symbol comparison | You just want the commands |
| — | **../tools/mine/README.md** | The measurement toolkit — rebuilding the panels, and the five ways a finding dies | You are about to test a new idea |
| — | **../tools/export_golden.md** | Golden-CSV parity procedure | Superseded — parity was confirmed directly against the app |

## The four things worth knowing before anything else

1. **Cost floor.** Round-trip taker fee is **$5.06** at gold ≈ 4600 — **20% of a $25
   target**. Most "improvements" shorten the hold or tighten the target, which makes this
   worse. Check this first on any proposal.

2. **Sample ceiling — now partly lifted.** Every XAUUSDT run produces 95–190 trades over
   166 days; at n=100 the 95% CI on a win rate is ±10 points, and ~15 configurations have
   already been tested against that one sample, so tuning against XAUUSDT alone still fits
   noise. **`PAXGUSDT` is now synced** — 1m/5m/15m/30m/1h/4h from 2022-03-15, 4.4 years,
   2.3M 1-minute bars. It tracks XAUUSDT to 0.237% and is the app's own gold fallback. Use
   it for anything that needs a sample, and keep XAUUSDT as the out-of-sample check.

3. **Two conditions have already failed for the same structural reason** — they were true
   at the moment the entry fired. Before proposing a filter, ask whether it can be true
   simultaneously with the entry. See ANALYSIS_BRIEF §7.

4. **The entry is gross-positive on gold and gross-NEGATIVE on BTC and ETH** over the same
   window with size normalised for volatility. So this is not "a good signal eaten by fees";
   it is "a signal that only looks good on gold, eaten by fees". BTC/ETH are synced and one
   command away — use them as a free out-of-sample check on anything you propose.
   See ANALYSIS_BRIEF §6.1.

## The feed changed, and it mattered more than any filter

Everything through `ANALYSIS_BRIEF.md` was measured on **Bybit** candles, where a round trip
costs 11 bps and the break-even win rate at a 2.5/2.5 ATR bracket is **62–64%**. The account
actually trades **XM MT5 `GOLD`**, whose spread is **0.38 median** measured over 20,000 M15
bars — a break-even of **51.1%**.

Re-validated on the broker's own bars over **4.2 years / 1,348 trades**, the ENTRY_RULES
trigger returns **53.5%** against that 51.3% break-even. It is the first time the rule has
cleared break-even on a large sample, and the margin is **+2.2 points with a confidence
interval that includes failure**. Full result and caveats: `AUTOMATION.md` §4b.

Practical consequence: **a Bybit-measured number is not a statement about what this account
would have earned.** Check which feed a result came from before quoting it.

## What is NOT from the app

Marked everywhere, but worth repeating: **MACD** and the **asia/london/ny session labels**
do not exist in the Android app. They are backtest-only additions and are **outside the
indicator parity that was confirmed against the live app**. Anything built on them is
unvalidated against anything real.
