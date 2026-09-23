# SIGNAL_MAP.md — tradable-event vocabulary

> ## ⚠️ READ THIS FIRST — implemented vs proposed
>
> This file was written during PHASE 0 as an inventory of **everything the app can
> express**. The engine implements a subset. The two drifted apart, so the authoritative
> status is stated here and pinned by a test (`tests/test_signal_docs.py`), which fails if
> the engine gains a signal this file does not mention.
>
> **Using a name from the "not implemented" list in a config raises `KeyError`.**

## ✅ Implemented — usable in a config today (99)

```
  adx_meaningful                    adx_strong                        adx_trending
  adx_very_weak                     bb_at_lower_band                  bb_at_upper_band
  bb_expansion                      bb_lower_band_area                bb_squeeze
  bb_upper_band_area                bos_bearish                       bos_bearish_event
  bos_bullish                       bos_bullish_event                 choch_bearish
  choch_bearish_event               choch_bullish                     choch_bullish_event
  di_bearish                        di_bullish                        dynamic_level_touch
  ema_spread_compressing            ema_spread_expanding              ema_stack_bearish
  ema_stack_bullish                 ema_stack_flip_bearish            ema_stack_flip_bullish
  ema_stack_mixed                   htf_15m_ut_bearish                htf_15m_ut_bullish
  htf_1h_bearish                    htf_1h_bullish                    htf_1h_ema_stack_bearish
  htf_1h_ema_stack_bullish          htf_1h_ut_bearish                 htf_1h_ut_bullish
  htf_1h_ut_cross_down              htf_1h_ut_cross_up                htf_30m_ut_bearish
  htf_30m_ut_bullish                htf_4h_bearish                    htf_4h_bullish
  htf_4h_ema_stack_bearish          htf_4h_ema_stack_bullish          htf_4h_not_bearish
  htf_4h_ut_bearish                 htf_4h_ut_bullish                 htf_5m_ut_bearish
  htf_5m_ut_bullish                 macd_above_signal                 macd_above_zero
  macd_below_signal                 macd_below_zero                   macd_cross_down
  macd_cross_up                     macd_hist_rising                  market_closed
  mtf_dip_long                      mtf_dip_short                     mtf_reclaim_long
  mtf_reclaim_short                 price_above_all_emas              price_above_vwap
  price_below_all_emas              price_below_vwap                  price_extended_above_ema28
  price_extended_below_ema28        price_near_mean                   regime_range_bound
  regime_trend_down                 regime_trend_up                   rsi_above_50
  rsi_above_70                      rsi_below_30                      rsi_below_50
  session_asia                      session_london                    session_ny
  structure_bearish                 structure_bullish                 structure_range
  usable                            ut_bias_bearish                   ut_bias_bullish
  ut_cross_down                     ut_cross_up                       ut_level_near
  ut_position_long                  ut_position_short                 volatility_extreme
  volatility_high                   volatility_low                    volatility_measured
  volatility_normal                 volume_above_average              volume_dead
  volume_normal                     volume_surge                      volume_thin
```

### ENTRY_RULES.md §4 additions (2026-08-23)

`htf_5m_ut_*`, `htf_15m_ut_*`, `htf_30m_ut_*`, `htf_4h_ema_stack_*`, `mtf_dip_*` and
`mtf_reclaim_*` were added so the multi-timeframe pullback rule has exactly **one**
definition, shared by `tools/validate_rule.py` and the live signal path
(`docs/AUTOMATION.md` §B). They read the aligned higher-timeframe frames the same way the
existing `htf_1h_*` / `htf_4h_*` signals do.

`mtf_dip_*` takes its lookback from `thresholds['dip_lookback']` (default 3) and **includes
the current bar** — a bar that dips and reclaims within itself is a valid setup.

⚠️ A leg whose timeframe has no data cannot be true, so a short 5m history silently kills
every signal before it starts. `validate_rule.py` now warns by name when this truncates a
panel; a config doing the same will simply produce no trades.

## ⛔ Proposed in this document but NOT implemented (57)

These describe real app behaviour and remain a valid to-do list, but the engine has no
evaluator for them yet.

```
  adx_developing                    analysis_score                    avoid
  bias_bearish                      bias_bullish                      entry_risk_extreme
  entry_risk_high                   entry_risk_low                    entry_risk_medium
  htf_bias_bearish                  htf_bias_bullish                  htf_bias_neutral
  htf_flip_bearish                  htf_flip_bullish                  htf_score
  location_poor                     location_severe                   long_quality_ge_45
  mtf_aligned_100                   mtf_aligned_70                    mtf_alignment
  mtf_conflict                      mtf_overall_score                 news_high
  news_just_released                news_medium                       news_unavailable
  news_veto                         no_clear_setup                    regime_high_volatility
  regime_low_volatility             regime_news_risk                  setup_long
  setup_long_event                  setup_short                       setup_short_event
  short_quality_ge_45               stf_bias_bearish                  stf_bias_bullish
  stf_score                         swing_high_confirmed              swing_low_confirmed
  trend_strength_ge_40              ut_confirmed_agrees               volatility_turned_extreme
  wait_for_confirmation             wait_for_pullback                 zone_ema_confluence_long
  zone_ema_confluence_short         zone_inside_resistance            zone_inside_support
  zone_resistance_near              zone_resistance_touch             zone_support_near
  zone_support_touch                zone_ut_confluence_long           zone_ut_confluence_short
```

**Why each family is missing:**

| Family | Why |
|---|---|
| `zone_*` | Support/resistance zones must be rebuilt per bar from a trailing 500-bar window across M30/H1/H4. Costly and not yet wired into the engine loop. |
| `setup_*`, `no_clear_setup`, `avoid`, `wait_*`, `entry_risk_*`, `location_*`, `*_quality_*` | These come from `ConfluenceEngine` / `DecisionEngine`, which are **not ported**. Porting them is a substantial piece of work and they depend on news data. |
| `news_*` | ForexFactory serves the current week only; there is no historical calendar. All runs have `news_available = false`. |
| `mtf_*`, `stf_*`, `bias_*`, `htf_bias_*`, `htf_flip_*` | `MultiTimeframeEngine` is not ported. The `htf_1h_*` / `htf_4h_*` signals that ARE implemented read the raw higher-timeframe frame directly instead. |
| `swing_*_confirmed`, `volatility_turned_extreme` | Edge detectors that were specified but never built. |
| `analysis_score`, `htf_score`, `mtf_*_score` | Scores, not booleans — they belong in `trade_features`, not the signal registry. |

## Columns usable in an `expr:` condition

Any of these can be referenced directly, e.g. `expr: "close > ema_28"`:

```
  adx_14                            adx_direction                     atr_14
  atr_percent                       bb_lower                          bb_middle
  bb_percent_b                      bb_upper                          bb_width
  bb_width_percentile               bos                               choch
  confirmed_bias                    directional_score                 ema_14
  ema_28                            ema_7                             ema_alignment
  ema_separation_atr                ema_spread_trend                  extension_atr
  last_swing_high                   last_swing_low                    macd
  macd_hist                         macd_signal                       minus_di
  plus_di                           prev_swing_high                   prev_swing_low
  relative_volume                   rsi_14                            structure_state
  usable                            ut_bias                           ut_buy
  ut_level                          ut_position                       ut_sell
  volatility_band                   volatility_measured               volatility_percentile
  vwap                              close                             high
  low                               open
```

---

Legend:
- **Slice** — ✅ CONFIRMED (closed bars, safe) · ⛔ LIVE (intrabar in the app; **the backtest
  evaluates it on the closed bar instead** — see `INDICATORS.md` §15 / OPEN_QUESTIONS Q28).
- **Edge** — `event` fires on the transition only; `state` is true for as long as the condition holds.
  A `state` signal used as an entry trigger needs `cooldown_bars` or it will re-fire every bar.
- **TF** — which timeframe the signal is evaluated on. `config` = the strategy's `timeframe:`.

⚠️ **Naming note.** The master prompt's examples (`ut_support_touch`, `ema_upper_reject`) do not
map onto this app. The app is explicit and repeated that the UT line is a **trailing stop, not
support or resistance** (`IND/UtBot.kt:9-10`, `ANA/structure/SupportResistanceEngine.kt:46-47`,
`ui/dashboard/DashboardReport.kt:160`), and support/resistance zones are built **separately** from
swing structure and session extremes. The closest true equivalent of `ut_support_touch` is
`zone_ut_confluence_long`. See OPEN_QUESTIONS Q29.

---

## A. UT Dynamic Level

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `ut_cross_up` | `utBot.buy[t]` — i.e. `close[t] > stop[t] && close[t-1] <= stop[t-1]`, and `stop[t-1] != null` | event | ✅ | config | `IND/UtBot.kt:118` |
| `ut_cross_down` | `utBot.sell[t]` — `close[t] < stop[t] && close[t-1] >= stop[t-1]` | event | ✅ | config | `IND/UtBot.kt:119` |
| `ut_bias_bullish` | `close[t] > trailingStop[t]` | state | ✅ | config | `IND/UtBot.kt:25` |
| `ut_bias_bearish` | `close[t] < trailingStop[t]` | state | ✅ | config | `IND/UtBot.kt:26` |
| `ut_position_long` | `utBot.position[t] == 1` (latched since the last up-cross) | state | ✅ | config | `IND/UtBot.kt:110` |
| `ut_position_short` | `utBot.position[t] == -1` | state | ✅ | config | `IND/UtBot.kt:111` |
| `ut_level_near` | `abs(close - utLevel)/atr14 <= 0.6` | state | ⛔→✅ | config | `ANA/alerts/AlertEngine.kt:47`, `:182` |

**Expression form:** `ut_level` is exposed for `expr:` use, e.g. `"close > ut_level"`.

---

## B. Structural support / resistance zones

Zones are built once, market-wide, from **M30 + H1 + H4** swings and session extremes — never
per-timeframe (`ANA/MarketAnalysisEngine.kt:141-151`, `ANA/alerts/AlertEngine.kt:99-101`).

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `zone_support_touch` | `nearestSupport.distanceFrom(close)/atr <= 0.25` | state | ⛔→✅ | market-wide | `AlertEngine.kt:57`, `:321-322` |
| `zone_resistance_touch` | `nearestResistance.distanceFrom(close)/atr <= 0.25` | state | ⛔→✅ | market-wide | `AlertEngine.kt:323-324` |
| `zone_support_near` | `nearestSupport.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | market-wide | `AlertEngine.kt:44`, `:188` |
| `zone_resistance_near` | `nearestResistance.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | market-wide | `AlertEngine.kt:190` |
| `zone_inside_support` | `nearestSupport.distanceFrom(close) == 0.0` (price inside the band) | state | ⛔→✅ | market-wide | `ANA/structure/SupportResistanceEngine.kt:20-24` |
| `zone_inside_resistance` | `nearestResistance.distanceFrom(close) == 0.0` | state | ⛔→✅ | market-wide | same |

`atr` here is the **entry-anchor ATR-14**; in the app that is M15 LIVE (`AlertEngine.kt:313`).
The backtest uses the config timeframe's CONFIRMED ATR-14 — divergence recorded in Q28.

---

## C. Confluence — the app's headline setups

These are the two rules the alert feature exists for (`README` "Alerts", `AlertEngine.kt:156-165`).

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `zone_ut_confluence_long` | `abs(close-utLevel)/atr <= 0.6` **AND** `utLevel <= close` **AND** `nearestSupport.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | config | `AlertEngine.kt:182`, `:188-189` |
| `zone_ut_confluence_short` | `abs(close-utLevel)/atr <= 0.6` **AND** `utLevel >= close` **AND** `nearestResistance.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | config | `AlertEngine.kt:190-191` |
| `zone_ema_confluence_long` | at least one of EMA14/EMA28/VWAP within `0.5*atr` of close **AND** `mtf.higherTimeframeBias != BEARISH` **AND** `nearestSupport.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | config | `AlertEngine.kt:234-247` |
| `zone_ema_confluence_short` | same dynamic-line test **AND** `mtf.higherTimeframeBias != BULLISH` **AND** `nearestResistance.distanceFrom(close)/atr <= 0.5` | state | ⛔→✅ | config | `AlertEngine.kt:248-249` |
| `dynamic_level_touch` | `min(abs(close-x)/atr for x in [ema14, ema28, vwap]) <= 0.5` | state | ⛔→✅ | config | `AlertEngine.kt:54`, `:234-238` |

⚠️ **Precedence in the app:** when both fire on one timeframe, the UT rule wins and the EMA rule is
suppressed (`AlertEngine.kt:88-96`). The backtest exposes them independently; replicate the
precedence yourself with `zone_ema_confluence_long AND NOT zone_ut_confluence_long` if you want
app-identical behaviour.
⚠️ `vwap` is **null on H4** (`IND/IndicatorSet.kt:126-132`), so on a 4H config the VWAP leg
silently cannot contribute.

---

## D. EMA stack

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `ema_stack_bullish` | `ema7 > ema14 > ema28` | state | ✅ | config | `IND/Ema.kt:40` |
| `ema_stack_bearish` | `ema7 < ema14 < ema28` | state | ✅ | config | `IND/Ema.kt:41` |
| `ema_stack_mixed` | not in order, none null | state | ✅ | config | `IND/Ema.kt:42` |
| `ema_stack_flip_bullish` | `alignment[t] == BULLISH && alignment[t-1] != BULLISH` | event | ✅ | config | derived from `IND/Ema.kt:34-44` |
| `ema_stack_flip_bearish` | `alignment[t] == BEARISH && alignment[t-1] != BEARISH` | event | ✅ | config | derived |
| `ema_spread_expanding` | `abs(sep[t]) > abs(sep[t-10]) * 1.15` | state | ✅ | config | `IND/IndicatorSet.kt:99` |
| `ema_spread_compressing` | `abs(sep[t]) < abs(sep[t-10]) * 0.85` | state | ✅ | config | `IND/IndicatorSet.kt:100` |
| `price_extended_above_ema28` | `(close - ema28)/atr14 > 1.5` | state | ⛔→✅ | M15 | `ANA/confluence/LocationAnalyzer.kt:47`, `:75` |
| `price_extended_below_ema28` | `(close - ema28)/atr14 < -1.5` | state | ⛔→✅ | M15 | `LocationAnalyzer.kt:85` |
| `price_near_mean` | `abs((close-ema28)/atr14) <= 0.75` | state | ⛔→✅ | M15 | `LocationAnalyzer.kt:95` |

`ema_spread_*` exist in the app but are consumed by nothing (`INDICATORS.md` §3.4) — they are
offered here because they are computed and available.
**Expression form:** `ema_7`, `ema_14`, `ema_28`, `ema_separation_atr` for `expr:` use.
⚠️ There is **no EMA 200** in this app. The master prompt's example `"close > ema_200"` has no
counterpart — see OPEN_QUESTIONS Q29.

---

## E. Bollinger channel

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `bb_at_upper_band` | `percentB > 0.95` | state | ✅ | config | `LocationAnalyzer.kt:112` |
| `bb_upper_band_area` | `0.85 < percentB <= 0.95` | state | ✅ | config | `LocationAnalyzer.kt:122` |
| `bb_at_lower_band` | `percentB < 0.05` | state | ✅ | config | `LocationAnalyzer.kt:131` |
| `bb_lower_band_area` | `0.05 <= percentB < 0.15` | state | ✅ | config | `LocationAnalyzer.kt:141` |
| `bb_squeeze` | `widthPercentile(t, 100) < 0.25` | state | ✅ | config | ⚠️ **derived** — see note |
| `bb_expansion` | `widthPercentile(t, 100) > 0.75` | state | ✅ | config | ⚠️ **derived** |

⚠️ `bb_squeeze` / `bb_expansion` **do not exist in the app**. `bbWidthPercentile` is computed
(`IND/Bollinger.kt:18-24`, `IND/IndicatorSet.kt:70`) but read by nothing; the 0.25/0.75 cut points
are borrowed from `VolatilityClassifier` for consistency, not extracted. **Flagged as new — Q16.**
**Expression form:** `bb_upper`, `bb_middle`, `bb_lower`, `bb_percent_b`, `bb_width`, `bb_width_percentile`.

---

## F. Momentum & trend strength

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `rsi_above_70` | `rsi > 70` | state | ✅ | config | `ANA/confluence/ConfluenceEngine.kt:249` |
| `rsi_above_50` | `rsi > 50` | state | ✅ | config | `ConfluenceEngine.kt:250` |
| `rsi_below_50` | `rsi <= 50` | state | ✅ | config | derived |
| `rsi_below_30` | `rsi <= 30` | state | ✅ | config | `ConfluenceEngine.kt:252` |
| `adx_trending` | `adx >= 20.0` | state | ✅ | H1 | `ANA/regime/MarketRegimeEngine.kt:36`, `:47` |
| `adx_very_weak` | `adx < 15` | state | ✅ | config | `ConfluenceEngine.kt:161` |
| `adx_developing` | `15 <= adx < 25` | state | ✅ | config | `ConfluenceEngine.kt:162` |
| `adx_meaningful` | `25 <= adx < 40` | state | ✅ | config | `ConfluenceEngine.kt:163` |
| `adx_strong` | `adx >= 40` | state | ✅ | config | `ConfluenceEngine.kt:164` |
| `di_bullish` | `plusDi > minusDi` | state | ✅ | config | `IND/Adx.kt:16` |
| `di_bearish` | `minusDi > plusDi` | state | ✅ | config | `IND/Adx.kt:17` |

⚠️ The app uses **three different ADX cut-point families** (15/25/40 narrative, 20 regime gate,
40 vote-scaling). `adx_trending` uses the only one that changes a label. **Q6.**
**Expression form:** `rsi_14`, `adx_14`, `plus_di`, `minus_di`.

---

## G. Market structure

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `structure_bullish` | `higherHigh && higherLow` | state | ✅ | config | `ANA/structure/MarketStructureEngine.kt:66` |
| `structure_bearish` | `lowerHigh && lowerLow` | state | ✅ | config | `MarketStructureEngine.kt:67` |
| `structure_range` | neither | state | ✅ | config | `MarketStructureEngine.kt:68` |
| `bos_bullish` | `state != BEARISH && close > lastSwingHigh` | state† | ✅ | config | `MarketStructureEngine.kt:80`, `:89` |
| `bos_bearish` | `state != BULLISH && close < lastSwingLow` | state† | ✅ | config | `MarketStructureEngine.kt:84`, `:90` |
| `choch_bullish` | `state == BEARISH && close > lastSwingHigh` | state† | ✅ | config | `MarketStructureEngine.kt:86` |
| `choch_bearish` | `state == BULLISH && close < lastSwingLow` | state† | ✅ | config | `MarketStructureEngine.kt:81` |
| `bos_bullish_event` | `bos_bullish[t] && !bos_bullish[t-1]` | event | ✅ | config | backtest-added edge detector |
| `bos_bearish_event` | `bos_bearish[t] && !bos_bearish[t-1]` | event | ✅ | config | backtest-added |
| `choch_bullish_event` | `choch_bullish[t] && !choch_bullish[t-1]` | event | ✅ | config | backtest-added |
| `choch_bearish_event` | `choch_bearish[t] && !choch_bearish[t-1]` | event | ✅ | config | backtest-added |
| `swing_high_confirmed` | a new accepted swing HIGH entered the chain at bar `t` | event | ✅ | config | `ANA/structure/SwingDetector.kt:65` |
| `swing_low_confirmed` | a new accepted swing LOW entered the chain at bar `t` | event | ✅ | config | `SwingDetector.kt:66` |

† **latched, not an edge** — `close > lastSwingHigh` stays true until a new swing forms. The app
compensates with a 6-bar alert cooldown (`AlertEngine.kt:304`). The `*_event` variants are the
backtest's own edge detectors and are **new**, not ported. **Q10.**
⛔ All of section G depends on the unstable swing chain (`INDICATORS.md` §7.1, R2/R3) and requires
expanding-window recomputation. **Q3.**

---

## H. Regime & volatility

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `regime_trend_up` | `adx >= 20 && netDirection >= 2` | state | ✅ | H1 | `MarketRegimeEngine.kt:65` |
| `regime_trend_down` | `adx >= 20 && netDirection <= -2` | state | ✅ | H1 | `MarketRegimeEngine.kt:66` |
| `regime_range_bound` | neither of the above | state | ✅ | H1 | `MarketRegimeEngine.kt:67` |
| `regime_high_volatility` | band in {HIGH, EXTREME} | state | ✅ | H1 | `MarketRegimeEngine.kt:73` |
| `regime_low_volatility` | band == LOW | state | ✅ | H1 | `MarketRegimeEngine.kt:74` |
| `regime_news_risk` | newsRisk in {HIGH, EXTREME} | state | ✅ | — | `MarketRegimeEngine.kt:77` |
| `volatility_low` | `atrPercentile < 0.25` | state | ✅ | H1 | `ANA/regime/VolatilityClassifier.kt:37` |
| `volatility_normal` | `0.25 <= atrPercentile < 0.75` | state | ✅ | H1 | `VolatilityClassifier.kt:38` |
| `volatility_high` | `0.75 <= atrPercentile < 0.90` | state | ✅ | H1 | `VolatilityClassifier.kt:39` |
| `volatility_extreme` | `atrPercentile >= 0.90` | state | ✅ | H1 | `VolatilityClassifier.kt:40` |
| `volatility_turned_extreme` | `band[t] == EXTREME && band[t-1] != EXTREME` | event | ✅ | H1 | `AlertEngine.kt:432-433` |

⚠️ A thin ATR% sample (<40) yields `NORMAL` with a null percentile — `volatility_normal` will be
true but `volatility_low/high/extreme` are all structurally unreachable. **Q7.**

---

## I. Multi-timeframe

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `htf_bias_bullish` | `mtf.higherTimeframeBias == BULLISH` (weighted H1 0.25 + H4 0.30) | state | ✅ | H1+H4 | `ANA/mtf/MultiTimeframeEngine.kt:62`, `:66` |
| `htf_bias_bearish` | `... == BEARISH` | state | ✅ | H1+H4 | same |
| `htf_bias_neutral` | `... == NEUTRAL` (score within ±0.15) | state | ✅ | H1+H4 | `CORE/Enums.kt:15-19` |
| `htf_flip_bullish` | `htfBias[t] == BULLISH && htfBias[t-1] != BULLISH` | event | ✅ | H1+H4 | `AlertEngine.kt:376-377` |
| `htf_flip_bearish` | `htfBias[t] == BEARISH && htfBias[t-1] != BEARISH` | event | ✅ | H1+H4 | same |
| `stf_bias_bullish` | `mtf.shortTermBias == BULLISH` (M5 0.10 + M15 0.15 + M30 0.20) | state | ✅ | M5–M30 | `MultiTimeframeEngine.kt:61`, `:65` |
| `stf_bias_bearish` | `... == BEARISH` | state | ✅ | M5–M30 | same |
| `mtf_conflict` | `stfBias != NEUTRAL && htfBias != NEUTRAL && stfBias != htfBias` | state | ✅ | all | `MultiTimeframeEngine.kt:75-77` |
| `mtf_aligned_70` | `mtf.alignment >= 70` | state | ✅ | all | derived from `:73` |
| `mtf_aligned_100` | `mtf.alignment >= 100` | state | ✅ | all | derived |

⚠️ `mtf_flip_*` requires the *previous evaluation*, which in the app is the previous **poll**, not
the previous bar (`AlertEngine.kt:375`). The backtest defines it against the previous **closed bar
of the config timeframe** — a deliberate divergence. **Q30.**
**Expression form:** `mtf_alignment`, `htf_score`, `stf_score`, `mtf_overall_score`.

---

## J. Confluence scores & the decision chain

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `bias_bullish` | `confluence.bias == BULLISH` (directionalScore > 0.15) | state | mixed | H1 | `ANA/confluence/ConfluenceEngine.kt:71` |
| `bias_bearish` | `confluence.bias == BEARISH` | state | mixed | H1 | same |
| `long_quality_ge_45` | `longQuality >= 45` | state | mixed | — | `ANA/decision/DecisionEngine.kt:29` |
| `short_quality_ge_45` | `shortQuality >= 45` | state | mixed | — | same |
| `trend_strength_ge_40` | `trendStrength >= 40` | state | mixed | — | `DecisionEngine.kt:35` |
| `location_poor` | `locationForBias <= -0.35` | state | ⛔→✅ | M15 | `DecisionEngine.kt:32` |
| `location_severe` | `locationForBias <= -0.6` | state | ⛔→✅ | M15 | `DecisionEngine.kt:158` |
| `ut_confirmed_agrees` | `utConfirmed.bias == confluence.bias && bias != NEUTRAL` | state | ✅ | H1 | `ANA/MarketAnalysisEngine.kt:96-98` |
| `setup_long` | full chain → `POTENTIAL_LONG_SETUP` | state | mixed | — | `DecisionEngine.kt:110-116` |
| `setup_short` | full chain → `POTENTIAL_SHORT_SETUP` | state | mixed | — | `DecisionEngine.kt:117-123` |
| `setup_long_event` | `setup_long[t] && !setup_long[t-1]` | event | mixed | — | backtest-added |
| `setup_short_event` | `setup_short[t] && !setup_short[t-1]` | event | mixed | — | backtest-added |
| `wait_for_pullback` | chain → `WAIT_FOR_PULLBACK` | state | mixed | — | `DecisionEngine.kt:90-97` |
| `wait_for_confirmation` | chain → `WAIT_FOR_CONFIRMATION` | state | mixed | — | `DecisionEngine.kt:81-87`, `:100-107` |
| `no_clear_setup` | chain → `NO_CLEAR_SETUP` | state | mixed | — | `DecisionEngine.kt:71-78` |
| `avoid` | chain → `AVOID` (news veto or extreme volatility) | state | mixed | — | `DecisionEngine.kt:55-68` |
| `entry_risk_low` | `entryRisk == LOW` (points <= 1) | state | mixed | — | `DecisionEngine.kt:162` |
| `entry_risk_medium` | points 2–3 | state | mixed | — | `DecisionEngine.kt:163` |
| `entry_risk_high` | points 4–5 | state | mixed | — | `DecisionEngine.kt:164` |
| `entry_risk_extreme` | points >= 6 | state | mixed | — | `DecisionEngine.kt:165` |

`setup_long` / `setup_short` are the app's **highest-conviction** output and correspond to the
`SETUP` alert (`AlertEngine.kt:345-366`).
⚠️ "mixed" slice = the chain blends H1 CONFIRMED direction with M15 **LIVE** location and volume.
Backtest evaluates everything CONFIRMED. **Q28.**
⚠️ Without a historical news calendar, `avoid` can only ever fire on extreme volatility, and every
`entry_risk_*` shifts up by 2 points. **Q9.**

---

## K. Volume

| signal_id | Condition | Edge | Slice | TF | Citation |
|---|---|---|---|---|---|
| `volume_surge` | `relativeVolume >= 1.5` | state | ⛔→✅ | M15 | `ConfluenceEngine.kt:274` |
| `volume_above_average` | `relativeVolume >= 1.2` | state | ⛔→✅ | M15 | `ConfluenceEngine.kt:275` |
| `volume_normal` | `0.8 <= relativeVolume < 1.2` | state | ⛔→✅ | M15 | `ConfluenceEngine.kt:276` |
| `volume_thin` | `relativeVolume < 0.8` | state | ⛔→✅ | M15 | `ConfluenceEngine.kt:277-278` |
| `volume_dead` | `relativeVolume < 0.5` | state | ⛔→✅ | M15 | `ConfluenceEngine.kt:278` |

Bybit reports genuine traded volume, unlike OANDA's tick count — but the app scales it
`(volume*100).toLong()` (`data/remote/BybitMarketDataSource.kt:133`). Only ratios are used.

---

## L. News (⛔ unavailable for historical backtests)

| signal_id | Condition | Citation |
|---|---|---|
| `news_veto` | `newsRisk.level == EXTREME` — HIGH-impact USD event within 30 min | `ANA/news/NewsRiskEngine.kt:75`, `:15` |
| `news_high` | HIGH-impact within 60 min, **or** a HIGH-impact event released in the last 15 min | `NewsRiskEngine.kt:73`, `:76` |
| `news_medium` | HIGH-impact within 240 min, or MEDIUM-impact within 30 min | `NewsRiskEngine.kt:77-78` |
| `news_just_released` | a gold-relevant HIGH-impact event printed within the last 15 min | `NewsRiskEngine.kt:66-68`, `:73` |
| `news_unavailable` | calendar could not be read — **+2 entry-risk points** | `NewsRiskEngine.kt:18-25`, `DecisionEngine.kt:149` |

⛔ ForexFactory's public feed is **current week only**. Unless a historical calendar is supplied,
every backtest runs with `news_unavailable == true`. **Q9.**

---

## M. Session / time filters — ⚠️ backtest-only, **not** ported

**No asia/london/ny session logic exists in the app** (verified by grep over `analysis/` and
`core/`). These are new derived features required by the master prompt's `trade_features` list.

| signal_id | Proposed definition (MYT = UTC+8) | Status |
|---|---|---|
| `session_asia` | 07:00–15:00 MYT | **NEW — needs your sign-off** |
| `session_london` | 15:00–23:00 MYT | **NEW** |
| `session_ny` | 20:30–05:00 MYT | **NEW** |
| `market_closed` | Sat, or Fri ≥22:00 UTC, or Sun <21:00 UTC | ✅ ported — `CORE/MarketHours.kt:25-33` |

**Q26 — please confirm or correct the three session windows.**

---

## N. Signals the app expresses but that are **not** tradable entries

Listed so the vocabulary is complete; they are available as `trade_features` only.

| Name | Why not an entry | Citation |
|---|---|---|
| `analysis_score` | "measures how much the evidence agrees… not a probability" | `ConfluenceEngine.kt:26-29` |
| `seasonality_*` | separate Clock tab; never feeds `MarketAssessment` | `ANA/seasonality/` |
| `ema_spread_trend` | computed, consumed by nothing | `IND/IndicatorSet.kt:63` |
| `bb_width_percentile` | computed, consumed by nothing | `IND/IndicatorSet.kt:70` |
| `data_state` | Fresh / Stale / MarketClosed / Error — feed health, not market state | `CORE/MarketSnapshot.kt:7-19` |

---

## O. Coverage against the master prompt's example signal_ids

| Prompt example | Verdict | Nearest real signal |
|---|---|---|
| `ut_support_touch` | ❌ **does not exist** — UT is not support | `zone_ut_confluence_long` |
| `ema_upper_reject` | ❌ no "EMA upper" concept | `bb_at_upper_band` + `ema_stack_bearish` |
| `ema_fast_cross_slow_up` | ⚠️ no explicit cross signal | `ema_stack_flip_bullish` |
| `regime_range_bound_lower_band_tag` | ⚠️ composite | `regime_range_bound` AND `bb_at_lower_band` |
| `close > ema_200` | ❌ **no EMA 200 in this app** | `close > ema_28`, or `htf_bias_bullish` |

**See OPEN_QUESTIONS Q29.** The example config `ut_support_long_v1` cannot be written as specified;
a faithful translation is proposed there.
