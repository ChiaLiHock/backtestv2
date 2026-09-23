# OPEN_QUESTIONS.md

Distilled from 96 ambiguities raised during PHASE 0. Ordered by how much they change the result.
**Q1–Q5 block PHASE 1/2 and need answers before any code is written.** The rest can proceed under
the stated default, but the default is recorded so it is never a silent choice.

---

## ⛔ BLOCKING

### Q0 — `XAUUSDT` only has 166 days of history. You asked to backtest from 2025.

**Resolved by measurement against the live API on 2026-08-22, not by assumption.**

`XAUUSDT` was listed on **2026-03-09 02:14 UTC** (`launchTime` from
`/v5/market/instruments-info`). That is the entire history that exists — not a
retention limit, an existence limit. Paging `end` backwards past it returns zero
rows.

| Symbol | Base | Listed | 4h bars available | Median deviation vs XAUUSDT |
|---|---|---|---|---|
| **XAUUSDT** | XAU | 2026-03-09 | ~999 (166 days) | — (reference) |
| **XAUTUSDT** | XAUT | 2025-04-03 | ~3,037 (17 months) | $17.18 / **0.393%** |
| **PAXGUSDT** | PAXG | 2022-03-14 | ~9,727 (**4.4 years**) | $10.08 / **0.237%** |

*(Deviation measured over the 999 overlapping 4h closes, gold ≈ $4,612.)*

What is synced today for `XAUUSDT`, zero gaps on every timeframe:

| TF | Bars | From | To |
|---|---|---|---|
| 1m | 239,378 | 2026-03-09 02:58Z | 2026-08-22 08:35Z |
| 5m | 47,876 | 2026-03-09 02:55Z | 2026-08-22 08:30Z |
| 15m | 15,959 | 2026-03-09 02:45Z | 2026-08-22 08:15Z |
| 30m | 7,980 | 2026-03-09 02:30Z | 2026-08-22 08:00Z |
| 1h | 3,990 | 2026-03-09 02:00Z | 2026-08-22 07:00Z |
| 4h | 998 | 2026-03-09 00:00Z | 2026-08-22 04:00Z |

**Why this matters for your actual goal.** After the 500-bar warm-up, a 30m
backtest has ~7,480 usable bars. That is enough to *run*, but PHASE 4 asks
"which conditions have win rate > 60% with N ≥ 30". Slicing ~150–250 trades
across `atr_percentile_250` × `hour_of_day_myt` × `regime_label` leaves most
buckets under 30 and the whole exercise becomes noise-mining.

**This partially reverses my Q1 recommendation.** `XAUUSDT` is the right
*instrument* — it is what the app prices, `baseCoin: XAU`, `LinearPerpetual`,
tick 0.01, confirmed live. But it is too young to mine.

Note the ranking surprise: **PAXGUSDT tracks XAUUSDT more closely than XAUTUSDT
does** (0.237% vs 0.393%) *and* has 9× the history. That matches the app's own
fallback order, which puts PAX Gold ahead of Tether Gold and dismisses `XAUTUSDT`
as "a much thinner market trading at its own discount".

**Options:**

1. **XAUUSDT only** — faithful to the app, 166 days. Good for validating the
   engine and for coarse questions; too thin for feature mining.
2. **PAXGUSDT as the research instrument** (recommended) — 4.4 years, tracks
   XAUUSDT to 0.237%, and is the app's own first fallback. Mine conditions here,
   then confirm them on XAUUSDT's 166 days as an out-of-sample check. This is
   strictly better than a 70/30 split on 166 days.
3. **XAUTUSDT** — 17 months, but the worst tracking of the three and the one
   contract the app explicitly warns against.

**→ I recommend (2): sync PAXGUSDT for mining, keep XAUUSDT as the live/OOS
instrument. One command either way; say the word and I will sync it.**

---

### Q1 — Which Bybit contract? The spec says `XAUTUSDT`; the code says `XAUUSDT`.

The master prompt says "likely `XAUTUSDT`". **The app uses `XAUUSDT`** and its own comment rejects
the other contract by name:

> `data/remote/BybitApi.kt:53-61`
> ```kotlin
> /**
>  * Bybit's gold commodity perpetual — `baseCoin` XAU, quoted in USDT, ticking in cents.
>  *
>  * Not to be confused with `XAUTUSDT`, the Tether Gold token, which is a thinner market
>  * and trades at its own discount. …
>  */
> const val SYMBOL_GOLD = "XAUUSDT"
> ```

The README repeats it, and the UI headline is `GOLD · XAUUSDT PERP`
(`ui/dashboard/DashboardReport.kt:194`).

**These are different instruments with different liquidity and different prices.** Backtesting one
and trading the other is not a rounding error.

**My recommendation:** `XAUUSDT`, confirmed at runtime against
`/v5/market/instruments-info?category=linear` by checking `baseCoin == "XAU"` and
`symbolType == "commodity"` (the README's stated test — note the *code* verifies neither field
today). Fall back with a hard error, never a silent substitution.

**STATUS: ✅ IMPLEMENTED AND VERIFIED LIVE.** `BybitClient.resolve_gold_symbol`
refuses to proceed unless `baseCoin == "XAU"`. Confirmed against the bytick
mirror on 2026-08-22:

```
symbol XAUUSDT · baseCoin XAU · quoteCoin USDT · LinearPerpetual
tickSize 0.01 · qtyStep 0.001 · minOrderQty 0.001
```

⚠️ **But see Q0** — being the right *instrument* does not make it a usable
research *dataset*. `XAUUSDT` is only 166 days old.

---

### Q2 — UT Bot default: `keyValue` 3.0 or 2.0?

Two defaults exist and they disagree:

| Constant | Value | Citation |
|---|---|---|
| `UtBot.DEFAULT_KEY_VALUE` | **3.0** | `analysis/indicators/UtBot.kt:70` |
| `IndicatorConfig.utKeyValue` | **= UtBot.DEFAULT_KEY_VALUE (3.0)** | `analysis/indicators/IndicatorSet.kt:10` |
| `UtSettings.DEFAULT_KEY_VALUE` | **2.0** | `data/SettingsStore.kt:30` |

`ATR period` is 10 in both. The README says the app ships **2 / 10** and that "the Pine script
itself ships with 3, but 2 is what the common TradingView setup uses"; it also notes that
2 vs 3 "moves the trailing level by 50%".

Every production path builds `IndicatorConfig` from `UtSettings`, so the **effective** default is
2.0 — but any code calling `IndicatorConfig()` directly gets 3.0.

**My recommendation:** default the backtest to **2.0 / 10** (what the app actually shows), and make
both a required, explicit field in every strategy YAML so it can never be implicit.

**→ Confirm 2.0 / 10.**

---

### Q3 — Structure must be recomputed per bar. Confirm the expanding-window rule.

`SwingDetector.filterAlternating` **replaces the last accepted pivot in place** when a later,
more extreme same-type pivot arrives:

> `analysis/structure/SwingDetector.kt:87-95`
> ```kotlin
> if (last.type == point.type) {
>     val moreExtreme = …
>     if (moreExtreme) accepted[accepted.lastIndex] = point
>     continue
> }
> ```

So `lastSwingHigh`, `StructureState`, BOS and CHoCH can change **without any new bar closing at
that index**. Computing structure once over the full history and indexing backwards would be a
look-ahead bug.

Raw pivot detection itself is safe: the loop bound is `candles.size - lookback`
(`SwingDetector.kt:54`), so at bar `t` the newest possible pivot is at `t - lookback`
(lookback = 3 on H1/H4, 2 otherwise, `SwingDetector.kt:24-27`).

**My recommendation:** recompute structure on the slice `[0..t]` for every `t`. This is O(n²) naive;
I will implement it incrementally with a cached accepted-pivot chain plus rollback, and pin the
equivalence with a test.

**→ Confirm. This is the correctness backbone of every structure signal.**

---

### Q4 — Rolling 500-bar window, or full history? This changes the UT level itself.

The app holds only the last **500 bars** per timeframe (`data/MarketDataManager.kt:250`, `:283`).
Every recursive indicator seeds at the **start of that window**, and the UT Bot's seed is
especially consequential: on the first ATR-available bar `prevStop` is forced to `0.0`
(`analysis/indicators/UtBot.kt:100`), so the stop initialises to `close - nLoss` — a **bullish
seed** — and then ratchets and only resets on a cross.

The README acknowledges this directly: 500 bars are requested rather than 250 "because the UT
trailing stop is path-dependent… shallow history can leave the level somewhere a deeper chart
would not."

So there are two incompatible options:

**(a) Full history.** Compute once from the first bar of the dataset. Cleanest, deterministic,
fastest — but the UT level will **not** match what the app displayed live.

**(b) Replicate the 500-bar window.** At each bar `t`, recompute every indicator over
`candles[t-499 .. t]`. Matches the app exactly. ~500× the compute, and makes indicator values a
function of *when you look*, which is philosophically ugly in a backtest.

**My recommendation: (a) full history**, with a documented warm-up burn-in of 500 bars discarded at
the start of every run, and a parity test that shows (a) and (b) converge after ~200 bars in
practice. But this is a judgement call about what you want to measure — the app's *displayed*
behaviour or the *indicator's* behaviour.

**→ Pick (a) or (b).**

### ⚠️ CORRECTION (found while rendering the chart replica)

**(a) is right for recursive indicators and WRONG for support/resistance.** I had assumed the
whole indicator set behaves the same way under more history. It does not.

EMA / ATR / RSI / ADX / UT all **converge** as history grows — an exponential-ish filter forgets
its seed. The swing chain does the opposite: it **accumulates**. Measured on XAUUSDT:

| Timeframe | Accepted swings, full history | Accepted swings, 500-bar window |
|---|---|---|
| 30m | 1,633 | 104 |
| 1h | 590 | 85 |
| 4h | 133 | 61 |

That is 2,356 raw levels versus 250. Because `SupportResistanceEngine.cluster` merges
**transitively** (`:151-158` — each level is compared to the previous one in the bucket, not to
the bucket's origin), 9× the levels collapse into a handful of enormous bands. The first chart
replica had a "support zone" 300 points tall; the app's are ~10 points.

**Resolution, implemented:** `IndicatorConfig.app_window_bars = 500`. Zone construction is
**always** windowed to the app's buffer, independently of the full-history choice for everything
else. Recursive indicators still use full history + 500-bar burn-in as decided.

**Consequence for PHASE 3:** zones must be rebuilt per bar from a *trailing* 500-bar window of
each of M30/H1/H4 — they cannot be computed once. This is the same class of requirement as Q3
(expanding-window structure) and is now the second-largest cost driver in the engine.

---

### Q5 — Which ATR is "the ATR" in the ATR-unit thresholds?

Two ATR series exist:

- `IndicatorSet.atr` — period **14** (`analysis/indicators/Atr.kt:8`, `IndicatorSet.kt:12`)
- UT Bot's internal ATR — period **10** (`analysis/indicators/UtBot.kt:71`, `:85`)

Every "in ATR units" threshold in the app — zone proximity 0.5, UT proximity 0.6, extension
tolerance 1.5, swing separation 0.5, zone merge 0.35 — reads `snapshot.atr`, i.e. the **14** series.
The UT ATR is used *only* inside the trailing-stop recursion.

I am confident this is intentional, but it is nowhere stated and no test pins it.

**My recommendation:** ATR-14 for all thresholds and all `trade_features`; ATR-10 only inside UT Bot.
**→ Confirm.**

---

## ⚠️ HIGH IMPACT — proceeding under a stated default

### Q6 — Three ADX thresholds coexist. Which is authoritative?

| Value | Role | Citation |
|---|---|---|
| 20.0 | the only gate that changes a **regime label** | `analysis/regime/MarketRegimeEngine.kt:36` |
| 15 / 25 / 40 | the **narrative** bands in evidence text | `analysis/confluence/ConfluenceEngine.kt:161-164` |
| 40.0 | `ADX_FULL_WEIGHT`, the **vote-scaling** divisor — declared **twice, independently** | `ConfluenceEngine.kt:50` **and** `analysis/mtf/TimeframeAnalyzer.kt:42` |

An ADX of 22 is simultaneously `TREND_UP` to the regime engine and "developing but not yet
established" on the Analysis page. No test pins the 20.0 boundary.

**Default:** port all three verbatim; expose `adx_trending` on the 20.0 gate. **→ Confirm.**

### Q7 — Thin volatility sample silently reads NORMAL

`VolatilityClassifier` returns `(NORMAL, current, percentile = null)` when fewer than 40 non-null
ATR% samples exist (`analysis/regime/VolatilityClassifier.kt:28`, `:33`). This is
**indistinguishable in the UI from a measured NORMAL**, and while in that state the EXTREME veto,
the `HIGH_VOLATILITY` tag and the `extreme_volatility` alert are all structurally unable to fire.

**Default:** emit a distinct `volatility_unmeasured` feature so runs can be filtered on it; keep the
band as NORMAL for parity. **→ Confirm.**

### Q8 — The orchestrator skips its own 60-bar usability gate

`MarketAnalysisEngine` checks only `series.closed.isEmpty()` (`analysis/MarketAnalysisEngine.kt:53`),
never `isUsable` (≥60 closed bars, `core/Candle.kt:45-49`). `MultiTimeframeEngine` does filter
(`analysis/mtf/MultiTimeframeEngine.kt:58`) and so does `AlertEngine`
(`analysis/alerts/AlertEngine.kt:83`). `MarketSnapshot.isComplete` (`core/MarketSnapshot.kt:34-35`)
exists for exactly this and is never called.

Result: a direction anchor with 20 bars still produces a full Analysis Score and Recommendation.

**Default:** the backtest **will** enforce ≥60 closed bars on every timeframe before emitting any
signal, and will discard the warm-up region entirely. This is a deliberate divergence.
**→ Confirm.**

### Q9 — No historical news calendar exists. How should news risk behave?

The app reads ForexFactory's **current week** JSON only. There is no historical source in the repo.
This is not cosmetic:

- `newsRisk.level == EXTREME` is a **hard AVOID veto** (`analysis/decision/DecisionEngine.kt:55-61`).
- `available == false` adds **+2 entry-risk points** (`DecisionEngine.kt:149`), pushing most trades
  from LOW to MEDIUM.
- `newsFactor` multiplies both quality scores by 1.0 / 0.9 / 0.7 / 0.5 (`ConfluenceEngine.kt:347-352`).

Three options:

1. **`news_available = false`** — faithful to "we don't know", but permanently inflates entry risk
   and caps quality at the LOW-news factor of 1.0.
2. **`news_available = true, level = LOW`** — pretends the calendar is clear. Removes the +2, keeps
   quality undamped. Optimistic and slightly dishonest.
3. **Supply a historical calendar** — you provide a CSV of US high-impact releases (FOMC, CPI, NFP,
   PCE, PPI, retail sales, GDP, ISM, jobless claims) with UTC timestamps and impact grades. Then
   the full chain is reproducible.

**My recommendation:** (3) if you can get the data — I'll write the importer. Otherwise (1) as the
default, with `news_mode` as an explicit YAML field so no run is ambiguous about which it used, and
`news_available` recorded in `trade_features` on every trade.

**→ Pick 1, 2 or 3.**

---

## ⚠️ SPEC-vs-CODE MISMATCHES

### Q26 — Trading sessions do not exist in the app

Grep over `analysis/` and `core/` finds **no asia / london / ny classifier**. The only day
boundaries are UTC midnight (VWAP anchor, `analysis/indicators/Vwap.kt:45`; S/R day bucket,
`analysis/structure/SupportResistanceEngine.kt:114-115`), the 21:00/22:00 UTC weekend roll
(`core/MarketHours.kt:20-23`), and MYT = UTC+8 in the seasonality feature
(`analysis/seasonality/SeasonalityModels.kt:12`).

The master prompt requires `session (asia/london/ny)` and `hour_of_day_myt` as `trade_features`.
These must be **invented**. Proposed (MYT = UTC+8):

| Session | MYT | UTC |
|---|---|---|
| Asia | 07:00–15:00 | 23:00–07:00 |
| London | 15:00–23:00 | 07:00–15:00 |
| New York | 20:30–05:00 | 12:30–21:00 |

London and NY overlap 20:30–23:00 MYT. **→ Confirm the windows, and tell me whether overlap should
produce `london_ny_overlap` as a fourth label or whether NY should win.**

**2026-09-20, partly settled for user-facing surfaces.** `engine/session_map.py` (the watcher's
session liquidity map) now owns ONE canonical definition — Asia 07:00–15:00 / Europe 15:00–20:00 /
US 20:00–05:00 MYT, NY-style overlap resolved by US winning from 20:00 — and everything the reader
sees (chart lines, cockpit, brief, notifications) reads it from there. The **backtest feature
labels** (`features.session_myt`) and **Signal 4's mining inputs** (`signal4.is_asia` etc.)
deliberately keep their own windows: they describe what was measured, and changing them after the
fact would silently change what a saved run means. The mismatch above therefore still stands for
those two, by choice.

### Q27 — "Opportunity Score" does not exist

The master prompt lists `opportunity_score` as a `trade_feature` and as a UI name. **There is no
such field anywhere in the codebase.** The app has exactly five 0–100 scores:

| Score | UI label | Citation |
|---|---|---|
| `longQuality` | "Long quality" | `ConfluenceEngine.kt:90-96` |
| `shortQuality` | "Short quality" | `ConfluenceEngine.kt:97-103` |
| `trendStrength` | "Trend strength" | `ConfluenceEngine.kt:313-320` |
| `analysisScore` | "Analysis score" | `ConfluenceEngine.kt:112` |
| `mtf.alignment` | "Alignment NN%" | `MultiTimeframeEngine.kt:73` |

**My recommendation:** drop `opportunity_score` and record all five instead. **→ Confirm**, or tell
me which one you meant.

### Q28 — LIVE→CONFIRMED substitution (the biggest deliberate divergence)

The app deliberately reads the **forming bar** for "where is price right now" questions:

| What | App slice | Citation |
|---|---|---|
| `LocationAnalyzer` input | M15 **LIVE** | `MarketAnalysisEngine.kt:72-76` |
| VOLUME confluence category | M15 **LIVE** relative volume | `ConfluenceEngine.kt:262` |
| `zone_ut_confluence` / `zone_ema_confluence` / `zone_touch` | **LIVE** | `AlertEngine.kt:176`, `:229`, `:313` |
| `assessment.price` | last close **incl. forming bar** | `core/Candle.kt:43` |

A closed-bar backtest cannot use any of these without look-ahead. Every one will be evaluated on
the CONFIRMED slice instead.

**Consequence:** backtest signals will be **strictly fewer and later** than the app's live alerts —
an intrabar wick into a zone that fully retraces before the close produces an app alert but no
backtest trade. That is the honest direction to err, but it means backtest results are a **lower
bound** on the app's live signal count, not a reproduction of it.

**→ Acknowledge. If you want the optimistic variant too I can add
`execution.allow_intrabar_touch: true` which uses bar high/low instead of close for zone-proximity
tests — but it is not what the app does on a closed bar.**

### Q29 — The example config `ut_support_long_v1` cannot be written as specified

```yaml
entry:
  all_of:
    - signal: ut_support_touch        # ❌ no such concept — UT is a trailing stop, not support
    - expr: "close > ema_200"         # ❌ no EMA 200 in this app (only 7/14/28)
  any_of:
    - signal: ema_upper_reject        # ❌ no "EMA upper" concept
    - signal: ema_fast_cross_slow_up  # ⚠️ no explicit cross signal; nearest is a stack flip
```

**Proposed faithful translation**, using only signals that exist:

```yaml
name: ut_zone_long_v1
symbol: XAUUSDT
timeframe: 30m
entry:
  side: long
  all_of:
    - signal: zone_ut_confluence_long   # price on the UT level inside a support zone
    - signal: htf_bias_bullish          # the 1H/4H context is not against it
  any_of:
    - signal: ema_stack_flip_bullish
    - signal: ut_cross_up
  cooldown_bars: 4
```

**→ Approve this translation, or tell me what `ut_support_touch` was meant to mean.**

---

## ℹ️ LOWER IMPACT — defaults recorded, no answer needed unless you disagree

| # | Question | Default I will take | Citation |
|---|---|---|---|
| Q10 | BOS/CHoCH is latched, not an edge | expose both `bos_bullish` (state) and `bos_bullish_event` (backtest-added edge) | `MarketStructureEngine.kt:72-73` |
| Q11 | In RANGE, one bar breaking both extremes sets `bos = BEARISH` (second assignment wins) | replicate verbatim | `MarketStructureEngine.kt:89-90` |
| Q12 | `cluster` chains transitively → zones wider than `tolerance`; "nearest" = nearest among the 3 **strongest** | replicate verbatim | `SupportResistanceEngine.kt:99-106`, `:151-158` |
| Q13 | ADX smooths `+DM`/`-DM` from index 0, where a synthetic `0.0` sits | replicate verbatim | `Adx.kt:41-52` |
| Q14 | `rmaNullable` would throw on a null after the seed | unreachable with real ADX input; add a guard + test | `MathSeries.kt:60-61` |
| Q15 | Two `Bias.fromScore` dead zones: 0.15 default, 0.2 for Analysis tone | replicate both | `Enums.kt:15`, `MarketAnalysisEngine.kt:200` |
| Q16 | `bbWidthPercentile` and `emaSpreadTrend` are computed but consumed by nothing | port them; expose as features and as **new** `bb_squeeze`/`bb_expansion` signals, clearly marked new | `IndicatorSet.kt:63`, `:70` |
| Q17 | `vwapSessionOffsetMillis` is documented as configurable but never set | keep 0L (UTC midnight); expose in YAML | `IndicatorSet.kt:21` |
| Q18 | The LIVE directional score reuses the CONFIRMED structure | irrelevant — backtest is CONFIRMED-only | `TimeframeAnalyzer.kt:50-57` |
| Q19 | `alignment` truncates, never rounds | replicate `int()` truncation | `MultiTimeframeEngine.kt:73` |
| Q20 | `ConfluenceWeights` never overridden in the app | expose all 7 in YAML, defaults as shipped | `Evidence.kt:46-54` |
| Q21 | On CHoCH the first Evidence item keeps the pre-override score | display-only; category score is correct | `ConfluenceEngine.kt:188-197` |
| Q22 | The "Timeframe alignment" ±0.2 evidence never enters the category score | replicate — display-only | `ConfluenceEngine.kt:236` |
| Q23 | `quality()` treats unknown location identically to measured-neutral | replicate; record `location_known` as a feature | `ConfluenceEngine.kt:335` |
| Q24 | Bare `0.75` and `0.5` in LocationAnalyzer shadow named constants | replicate as separate tunables | `LocationAnalyzer.kt:95`, `:182` |
| Q25 | Zone alerts dedupe on a drifting `zone.mid` with no bar stamp | backtest uses `cooldown_bars` instead | `AlertEngine.kt:339` |
| Q30 | `mtf_flip` compares against the previous **poll** in the app | backtest compares against the previous **closed bar** of the config TF | `AlertEngine.kt:375` |
| Q31 | Bybit `end` inclusive or exclusive? Not determinable from the app (it never pages) | assume **inclusive**; verify empirically in PHASE 1 and assert on overlap | `BybitApi.kt:37-38` |
| Q32 | Bar `complete` is derived from the device clock | in a backtest, derive from `openTime + interval <= min(now, requested_end)` | `BybitMarketDataSource.kt:134` |
| Q33 | VWAP's first session in any window is truncated | discard VWAP for the first partial UTC day of every run | `Vwap.kt:26` |
| Q34 | VWAP is force-nulled on H4 | replicate; a 4H config gets no VWAP leg | `IndicatorSet.kt:126-132` |
| Q35 | Only `utKeyValue`/`utAtrPeriod` are reachable at runtime in the app | expose **all** of `IndicatorConfig` in YAML; defaults as shipped | `SettingsStore.kt:122-123` |

---

## Environment blocker (not a code question)

`C:\inetpub\Claude\ITSupport` grants write access only to `TrustedInstaller`, `SYSTEM` and
`BUILTIN\Administrators`. This session runs as `LAPTOP-E1CN03PN\User`, **not elevated**, so
`backtest\` cannot be created there.

These three documents are currently at:
`C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\7374da65-69a2-4f1c-949e-e44abe04247c\scratchpad\backtest\docs\`

Options: (a) grant your user Modify on the folder from an elevated prompt, (b) put the project
somewhere you own such as `C:\Users\User\backtest`, or (c) keep it in the scratchpad (not durable).
See the response text for the exact command.
