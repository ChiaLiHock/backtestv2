# Mining brief — read fully before running anything

You are mining for entry-condition combinations that raise the probability a trade
reaches its target before its stop, on gold. The substrate is already built. **Do not
rebuild it, do not re-sync data, do not modify `screen.py`, `build.py` or `feats.py`.**

## Environment

```
PY   C:\inetpub\Claude\ITSupport\backtest\.venv\Scripts\python.exe
MINE C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine
```

Run everything with that interpreter. Write your own scratch scripts into
`$MINE\work\<your-family>\` — never anywhere else.

## The panels

| spec | what it is | why it matters |
|---|---|---|
| `XAUUSDT/15m` | the live instrument, 166 days | small (n≈100 trades), the target market |
| `PAXGUSDT/15m/2025,2026` | gold token, liquid era | independent, n≈480 |
| `PAXGUSDT/1h/2025,2026` | same, slower anchor | independent, n≈240 |
| `PAXGUSDT/15m/2022,2023,2024` | **hostile panel** — illiquid + range-bound | nothing should look good here; if it does, suspect a bug |
| `BTCUSDT/15m`, `ETHUSDT/15m` | transfer check, same 166 days | a gold-specific edge should NOT appear here; a microstructure edge might |

## The null, and why

Every measurement is **pooled long + short at a SYMMETRIC bracket** (TP = SL in ATR).
Because `P(long hits +k·ATR first) + P(short hits −k·ATR first) ≈ 1` on any bar, the
pooled win rate of a *random* rule is **49.8%** regardless of what the market did. Gold
fell 10% over the XAUUSDT window, so a long-only win rate is uninterpretable. **49.8% is
your zero. Report `wr` against it, and always report `wrL` and `wrS` separately** — a rule
whose lift lives on one side only is a bet on the window, not an edge.

`be` in the output is the break-even win rate *after costs* for that panel and bracket.
`wr` above 49.8 means the signal is real; `wr` above `be` means it is tradable. **They are
different questions and you must not conflate them.** PAXG 15m has `be` ≈ 70% and nothing
clears it — that panel is for measuring the signal, not for judging profitability.

## The tool

```python
import sys; sys.path.insert(0, MINE)
from screen import Panel, simulate, screen, bins

p = Panel("XAUUSDT", "15m")                       # or Panel("PAXGUSDT","15m",(2025,2026))
base = p.base()                                   # the docs/ENTRY_RULES.md rule, (long,short)
print(bins(p, "age_5m", base).to_string())        # quantile response of one feature
print(bins(p, "div_macd_hid_bull", base, mirror="div_macd_hid_bear").to_string())

cands = [("name", "d.age_5m < 30", "d.age_5m < 30")]   # (name, long_expr, short_expr)
print(screen(p, cands, base).to_string())
```

CLI equivalents:

```
$PY $MINE\screen.py bins XAUUSDT/15m age_5m --base
$PY $MINE\screen.py bins XAUUSDT/15m div_macd_hid_bull --base --mirror div_macd_hid_bear
$PY $MINE\screen.py cands PAXGUSDT/15m/2025,2026 mycands.json --base
```

`--base` applies the reference rule underneath (which supplies DIRECTION). Without it the
candidate must be directional by itself — the long and short expressions must differ, or
every bar will be ambiguous and skipped.

Brackets: `--tp/--sl` from {1.5, 2.0, 2.5, 3.0} for SL and {1.5 … 6.0} for TP.
**Default to the symmetric 2.5/2.5 for mining.** Only look at asymmetric brackets once a
condition has already survived the symmetric test.

## The columns you have

496 per panel. `$PY -c "import pandas as pd; print(list(pd.read_pickle(r'...panel_XAUUSDT_15m.pkl').columns))"`

Naming: an unprefixed column is the anchor timeframe (15m for a 15m panel). Prefixed
`5m_`, `30m_`, `1h_`, `4h_` are that timeframe's value **as of its last CLOSED bar** — no
look-ahead. Families:

* **from the app's indicator port** — `atr_14 atr_percent ema_7 ema_14 ema_28
  ema_alignment ema_separation_atr ema_spread_trend extension_atr rsi_14 macd macd_signal
  macd_hist bb_upper bb_middle bb_lower bb_width bb_percent_b bb_width_percentile adx_14
  plus_di minus_di adx_direction vwap relative_volume ut_level ut_position ut_buy ut_sell
  ut_bias structure_state bos choch last_swing_high last_swing_low prev_swing_high
  prev_swing_low volatility_band volatility_percentile directional_score confirmed_bias`
* **flip timing (new)** — `ut_flip_age_min` `stack_flip_age_min` per timeframe, and on the
  anchor grid `age_5m age_15m age_30m age_1h age_4h`, `lag_5m_15m` `lag_15m_1h` `lag_5m_4h`
  (differences in minutes), `age_min_all` `age_max_all`, `n_bull` `n_bear` (how many of the
  five timeframes agree), `cascade_fast_first`
* **divergence (new)** — `div_{macd,rsi}_{reg_bear,reg_bull,hid_bear,hid_bull}` per
  timeframe. `reg` = regular (reversal), `hid` = hidden (continuation). True for 20 bars
  after the second pivot is **confirmed**, so there is no look-ahead.
  ⚠️ MACD is **not in the Android app** — it is a backtest-only addition, outside the
  confirmed indicator parity. Say so if you build on it.
* **volume (new)** — `vol_pctile_100`, `turnover`, plus the port's `relative_volume`
* **geometry (new)** — `body_ratio` `close_pos` `dip_depth_atr_5` `pop_height_atr_5`
  `low_vs_ema14_atr` `high_vs_ema14_atr` `close_vs_ut_atr` `close_vs_vwap_atr`
  `rsi_min_5` `rsi_max_5`
* **slopes (new)** — `adx_slope_5` `rsi_slope_5` `macd_hist_slope_3` `sep_slope_10`
* **time** — `hour_myt` `hour_utc` `dow` `year` `in_session`

## Rules of evidence — a finding that breaks any of these does not count

1. **Replication beats significance.** A condition must hold on **XAUUSDT/15m AND at least
   one PAXG liquid panel**. A result on one panel is a hypothesis, not a finding.
2. **Shape beats a p-value.** Use `bins()` first. A *monotone* response across quantiles is
   credible. One hot bin between two cold ones is noise however good its z looks. Report the
   whole bin table, not the best bin.
3. **Symmetry.** `wrL` and `wrS` must both move. If only one side lifts, say so and mark the
   finding as failed.
4. **Count your tests.** State how many conditions you evaluated. With ~50 tests a z of 2 is
   expected by chance roughly twice. `screen()` returns `p` and `fdr_pass`.
5. **n ≥ 40 trades** on at least one panel, and never quote a bin with n < 25.
6. **The hostile panel is a tripwire.** If your condition looks good on PAXG 2022–2024 too,
   you have probably found a look-ahead bug rather than an edge. Check it.
7. **No new indicators.** Everything you need is already in the panel. If you truly need a
   derived column, compute it inline in your own script from existing columns — do not edit
   the shared builders.

## What has already been established — do not re-derive

`C:\inetpub\Claude\ITSupport\backtest\docs\ENTRY_RULES.md` is the current state. Summary:

* Base rule = all five timeframes' UT bias agree + 4H EMA stack agrees + price dipped below
  15m EMA-14 within 3 bars and closed back above it. Pooled WR 67.0% (XAU, n=112),
  56.6% (PAXG 15m liquid, n=482), 57.8% (PAXG 1h liquid, n=237).
* **Already screened and found worthless** (lift ≤ ~2 points, no replication): ADX ≥20/≥25,
  RSI>50, RSI 45–60, relative_volume ≥1.2, volatility band, structure state, hour-of-day
  buckets, price extended >1.5 ATR from EMA-28, Kaufman efficiency ratio, macro trend
  (close vs close 1/3/5/10/20/30 days back), 4H EMA separation thresholds.
  **Re-testing these in exactly the same form is wasted work.** Testing them in a *new
  form* (as a slope, an interaction, a percentile rather than a level) is fair game — say
  explicitly what is new about your form.
* The pullback pattern **alone** is worthless: 49.8% on n=2107.
* No regime gate has been found that separates PAXG's good years from its bad ones. That is
  the open problem and anything that cracks it is the highest-value result available.

## What to report back

Return a compact structured summary. For each condition you tested that survived rule 1:

* the exact expression (long and short)
* the bin table or the screen row, with n, wr, wrL, wrS, z, be, pf — for **every** panel
* whether it is additive to the base rule or a standalone direction signal
* a one-line mechanism: *why* would this work? "it correlates" is not a mechanism
* your own best argument against it

Also report, in one line each, the conditions you tested that **failed** — that list is as
valuable as the survivors and stops the next session repeating you.

Be blunt about null results. Most of what you test will be nothing. Saying so clearly is
the deliverable, not a failure.
