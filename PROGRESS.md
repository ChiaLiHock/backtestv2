# PROGRESS.md

Chronological record. For current state and everything learned the hard way, read
`docs/HANDOVER.md` first, then `docs/ANALYSIS_BRIEF.md`.

**Tests: 248 passed, 1 skipped.** Nothing under `app/` was ever modified — read-only throughout.

---

## PHASE 0 — Indicator archaeology · ✅ COMPLETE

- Read all 4,107 lines of `analysis/` + `core/` plus the Bybit data layer and UI layer.
- 11 parallel extraction agents, each audited by a hostile citation checker, plus a
  completeness critic (23 agents, 823 tool calls). Every citation re-verified by hand —
  several agent-supplied line numbers were wrong and were corrected.
- Produced `docs/INDICATORS.md`, `docs/SIGNAL_MAP.md`, `docs/OPEN_QUESTIONS.md`.

**Findings that changed the plan:** the symbol is `XAUUSDT` not `XAUTUSDT`; UT Bot has two
conflicting defaults (3.0 in `UtBot.kt`, 2.0 in `SettingsStore.kt`); swing pivots are
mutated in place so structure must be recomputed per bar; six inputs read the forming bar;
no session logic, no EMA 200 and no "Opportunity Score" exist in the app.

---

## PHASE 1 — Market data · ✅ COMPLETE

`data/bybit_client.py` (bytick with bybit fallback, seeded backoff jitter for determinism,
~8 req/s, `retCode`-inside-200 handling), `data/db.py` (repository layer; `sqlite3` never
leaves `backtest.data`), `data/sync.py` (idempotent, resumable, gap-refilling).

**Verified live.** `XAUUSDT` resolved: `baseCoin XAU`, `LinearPerpetual`, tick 0.01.
Synced **zero gaps**: 1m 239,378 · 5m 47,876 · 15m 15,959 · 30m 7,992 · 1h 4,006 · 4h 998.

⚠️ **`XAUUSDT` was listed 2026-03-09 — only 166 days exist.** `PAXGUSDT` has 4.4 years and
tracks it to 0.237%; it has since been synced in full (1m–4h from 2022-03-15) by a parallel
session, along with `BTCUSDT` and `ETHUSDT` over XAUUSDT's window. See `OPEN_QUESTIONS.md`
Q0 and `docs/ENTRY_RULES.md`.

---

## PHASE 2 — Indicator port + parity · ✅ COMPLETE, parity CONFIRMED

`indicators/` — `mathseries`, `trend` (ATR/EMA/UT Bot), `context` (RSI/BB/ADX/VWAP/RelVol/
MACD), `structure` (swings/BOS/CHoCH/zones), `regime`, `registry`, `base`.

**Parity confirmed against the live app** by the user comparing the Chart page legend with
`tools/plot_chart.py` output on 1H and 30m, UT inputs 2.0/10. The golden-CSV export is
therefore no longer required.

Adversarial port audit (8 modules, hostile auditor with a live interpreter) found five real
numerical divergences — see `HANDOVER.md` §5.3. All fixed and pinned by bit-identical tests
against literal Kotlin transcriptions over 20,000 bars.

**Performance:** a 240k-bar 1m series went from >2 min to 4.0 s (removed an O(n × pivots)
per-bar chain copy; vectorised two rolling-percentile loops). Results unchanged.

**Look-ahead safety:** `test_no_lookahead.py` truncates the dataset at several cut points
and requires every earlier value to be bit-identical, then mutates all *future* bars and
requires the past not to move.

---

## PHASE 3 — Backtest engine · ✅ COMPLETE

`engine/config.py` (pydantic schema + stable `config_hash`), `signals.py` (**87**
signal_ids), `fills.py`, `features.py`, `backtester.py`.

Acceptance criteria met: same config → same hash → identical trade list; every trade has a
complete `trade_features` snapshot (~150 features across 5m/15m/30m/1h/4h); the engine
refuses to run over data gaps without `--allow-gaps`.

**New capability:** `ltf_ema_break` trailing exit — walks the lower-timeframe sub-bars
inside each strategy bar, checking the stop against each sub-bar's high/low **before**
comparing its close to the EMA (a stop is intrabar; an EMA break is only knowable at the
close). Plus `arm_on_recross`, without which a pullback entry exits immediately.

---

## PHASE 4 — Analytics · ⏳ PARTLY DONE

Done: `analysis/metrics.py` (Wilson CIs, drawdown, profit factor, streaks, ambiguous
count), `analysis/report.py` + `templates/report.html`, `tools/sweep.py`.

**Interactive report** (`report.bat`) — self-contained ~8 MB HTML, no CDN: timeframe
switcher holding the same window, entry/exit markers, per-signal filter, clickable trade
list, **multi-timeframe UT rail** (one lane per TF — the confluence view), Volume / RSI /
MACD panels, VWAP overlay.

**Live watch** (`watch.bat`) — syncs, re-runs, rebuilds and serves on a timer (~7 s/cycle).
The page polls `status.json` and shows a banner rather than force-reloading. Live signals
are read from the **last closed bar**, and it shows **which entry condition is blocking**.

Not done: `by_feature`, `by_time`, `compare()`, in-sample/out-of-sample split.
**Deliberately deferred** — 95–190 trades is too small to mine honestly.

---

## PHASE 4b — live chart, measuring, multi-instrument · ✅ COMPLETE

Three things, all driven by using the tool rather than by the original spec.

**1. The chart updates itself every 5 seconds.** `watch.py` now runs two loops. The fast one
makes a single 1-minute request per symbol, folds the forming bar for every timeframe from
it, recomputes indicators over a 1,500-bar tail and writes `live.json`. The browser patches
those bars into the arrays it already holds. The slow loop still does the real work — sync,
backtest, rebuild — and when it finishes, the page fetches the new payload and swaps it in.
**Nothing reloads.** Zoom, selected trade, timeframe and any measurement all survive.
The old "New data · click to refresh" button is gone.

The forming bar is drawn hollow and is display-only. UT markers, the Live panel and every
entry condition still read the last CLOSED bar.

A real bug surfaced here and is written up in `HANDOVER.md` §5.0: the first version appended
only the forming bar onto a stale tail, so a bar that had closed since the last sync was
**skipped**, and the indicators were computed over a series with a hole in it.

**2. A measuring tool.** Shift+drag, or click Measure. Reads Δ price, %, **×ATR-14 at the
left anchor**, bar count, elapsed time, and the dollar value at that run's position size.
Anchors are stored as (bar index, price), so the measurement survives panning, zooming, a
live tick and a payload swap.

**3. The hover card.** Pointing at any bar reproduces the app's TECHNICAL STATE and
KEY LEVELS panels as of that bar. The state fields were already per-bar in the indicator
frame; the zones were not, and are the real work — the app rebuilds them from a trailing
500-bar buffer of M30+H1+H4 swings plus H1 session levels, and swing chains accumulate, so
they cannot be derived by indexing into one full-history pass. `indicators/zones.py` builds
one snapshot per H1 bar (~3 ms each) and caches them in `zone_snapshots`, keyed by the
indicator config so a window change invalidates rather than serving stale rows. A closed
bar's snapshot depends only on closed bars, so caching is exact.

Because zones are market-wide and split by the H1 confirmed close, one H1 series serves
every chart timeframe — and a 5m bar reads the last *closed* H1 snapshot, labelled with how
stale it is. `tests/test_live.py` pins agreement with `plot_chart.build_zones`, immunity to
future bars, and that no UT-derived source ever reaches a zone.

**4. Malaysia and New York time.** The axis, trade list and header switch between MYT, NY
and UTC; the tooltip shows all three labelled; the header carries a live clock in both. New
York uses the IANA zone rather than a fixed offset, so EDT/EST is correct — the dataset
spans a DST change and a hardcoded −5 would be an hour wrong from March to November.
Storage stays epoch-ms UTC; this is display only.

**5. Real fills on the chart.** `data/mt5.py` imports a MetaTrader 5 trade-history
export. The hard part is not parsing: MT5 writes the broker server's wall clock with **no
timezone marker anywhere in the file**, and a wrong guess puts every fill on the wrong bar
while still looking plausible. So the offset is measured — every whole-hour candidate is
scored by the distance between each fill price and the 1-minute candle it would land on.
This account resolves to **UTC+3** (EEST) with a 2.6x margin, and 87% of fills then land
inside the 1h bar they were taken on.

What remains after aligning the clock is a genuine contract basis: XM's spot `GOLD` trades
**3.38 under** Bybit's `XAUUSDT` perpetual (−0.076%, IQR 2.2–4.7). Fills are stored and
drawn at the price actually paid, never adjusted toward the candles, and the basis is
reported so an off-wick marker has an explanation.

Imported: 348 GOLD, 5 BTCUSD, 7 open ETHUSD. **Net +3,574.62 on gold** over the same window
in which every backtested variant lost money — worth noting before optimising the strategy
further.

**6. Live MT5 connection.** `data/mt5_live.py` reads history straight out of the running
terminal, so there is nothing to export or import. `watch.bat` syncs at the start of every
cycle (0.03 s incremental, re-reading 7 days so a position closed today but opened last week
picks up its exit); `--no-mt5` disables it.

**No credential is involved and none is accepted.** `initialize()` is called with no
arguments, which attaches to the terminal already logged in on this machine. The package's
`initialize(login=, password=, server=)` form is deliberately unused, and
`tests/test_mt5_live.py` parses the module: it fails if `initialize` ever receives an
argument, if any identifier mentions a password, or if `order_send`/`order_check` appears.
Read-only by construction, asserted statically so it holds without a terminal present.

The clock is measured here too, by a second independent route: the last tick of a symbol
whose market is open *is* the server's current time. The freshest quote across candidates is
used — on a weekend gold's last tick is Friday's and would imply UTC−32. It reports **UTC+3**,
matching the .xlsx price-matching heuristic exactly, and a test asserts the agreement.

MT5 stores deals, not positions, so deals sharing a `position_id` are recombined with their
commission, swap and fee summed. A test compares the rebuilt positions against the .xlsx
export field for field on every position both contain — 348 gold and 5 BTC agree exactly,
and the live view is ahead by the ETH trades taken after the export.

**7. Trades panel and rail.** The list is newest-first, filters to winners or losers only
(driving the chart overlay with it), and switches between backtest and real fills. The rail
gained a `signal` lane and a `mine` lane alongside the per-timeframe UT lanes.

**8. Any instrument, comparably.** `--symbols XAUUSDT,BTCUSDT,ETHUSDT` on `backtest`,
`report` and `watch`. `engine/symbols.py` scales `qty` by the ATR ratio so every symbol
risks the same dollars per trade, pins them all to the window they all cover, and takes
`tickSize` from the instrument. Without that, `$25` is 1.7×ATR on gold and 0.06×ATR on BTC
and the comparison measures tick size. The report gained a symbol switcher and a
cross-symbol table.

**Result — the one genuinely new finding.** Same config, same days, normalised size:

| symbol | n | win% | **gross** | fees | net |
|---|---|---|---|---|---|
| XAUUSDT | 99 | 51.5% | **+137.59** | 482.28 | −347.76 |
| BTCUSDT | 97 | 44.3% | **−290.81** | 279.11 | −575.01 |
| ETHUSDT | 90 | 44.4% | **−237.12** | 195.02 | −439.32 |

Gold is the only one where the entry is gross-positive. On BTC and ETH the signal loses
before fees, so no cost reduction could rescue it. Confidence intervals overlap, so this is
not proof — but it reframes the problem from "a good signal eaten by costs" to "a signal
that only looks good on gold". BTC/ETH are now the cheap out-of-sample check the project
did not have. See `docs/ANALYSIS_BRIEF.md` §6.1.

---

## PHASE 6 — MT5 execution path · ⏳ steps 1–2 of 9 done

`docs/AUTOMATION.md` is the build spec (written by a parallel session; its §1 spread figures
were corrected here after being found to come from a stale weekend tick). Build order §10:

| step | state |
|---|---|
| 1. §A MT5 rates + time conversion + test 7 | ✅ `data/mt5_rates.py`, `cli sync-mt5-rates` |
| 2. **§5.1 re-validation — decision point** | ✅ **passed conditionally** — see below |
| 3. §B signal engine + `signals.jsonl` | ⏸ |
| 4. §5.2 spread study | ✅ done from the broker's own per-bar record |
| 5. §F notifier | ⏸ |
| 6. §C/§D/§E executor, `execute: false` | ⏸ |
| 7. §5.3 paper run ≥30 signals | ⏸ |
| 8. flip `execute: true` | ⏸ — **owner's decision, not the tool's** |
| 9. §G LLM brief | ⏸ |

**Feed.** `MT5:GOLD` stored under its own symbol key, never merged with Bybit `XAUUSDT`.
Server time (UTC+3) converted to true UTC once at the boundary; the H4 partition is kept on
the broker's UTC+3 day rather than re-bucketed, because the 4H UT level is a path-dependent
latching stop and a different partition is a different flip history. Depth is per timeframe
and set by the broker: 15m back to 2022-05, 30m to 2018, 1h and 4h to **2001**.

**Rule vocabulary.** `htf_{5m,15m,30m}_ut_*`, `htf_4h_ema_stack_*`, `mtf_dip_*`,
`mtf_reclaim_*` added to `engine/signals.py` — 99 signals now — so the rule has exactly one
definition shared by the harness and the future live path.

**Gate §5.1 result.** `tools/validate_rule.py` implements ENTRY_RULES §6 and reproduces the
study exactly on Bybit (n=112, 67.0%, 70.5/64.7) before being trusted. On `MT5:GOLD` 15m over
**4.2 years / 1,348 trades**: pooled **53.5%** against a **51.3%** break-even, nine of nine
bracket cells net positive, one negative year (2023, a range). **First time the rule has
cleared break-even on a large sample** — but the CI lower bound is below break-even, the
short side is at break-even, and at minimum lot the sizing is 11.3% of equity per trade.
Full result and caveats in `AUTOMATION.md` §4b.

---

## PHASE 5 — FastAPI · ⏸ NOT STARTED

`api/` is an empty package. `watch.py` covers the live use case on localhost.

---

## Results — nothing has an edge

| config | n | win% | net | exposure |
|---|---|---|---|---|
| `ut_1h_long_v1` ($25/$25) | 99 | 51.5% | −347.76 | 39.5% |
| `ut_1h_long_atr` (1.75×ATR) | 95 | 50.5% | −361.02 | 24.8% |
| `ut_mtf_ride_long` (best of 12 sweep) | 169 | 26.6% | −374 | 8.8% |
| `ut_mtf_ride_short` | 190 | 27.9% | −882.38 | 6.4% |
| **buy & hold 1 unit** | — | — | **−524.90** | 100% |

Gold **fell 10.1%** over the window (5127 → 4608). The long variants losing less than
buy-and-hold is **not** an edge — they are flat ~91% of the time, so they did not
participate in the decline. All 12 trail-sweep combinations lose (−374 to −717, win
24–29%), so it is not a tuning problem.

Round-trip cost is **$5.06 = 20% of a $25 target**. Every config is gross-positive and
net-negative: **costs are the entire deficit.**
