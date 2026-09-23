# INDICATORS.md — Indicator Archaeology

**Source app:** `C:\inetpub\Claude\ITSupport` — XAUUSD Market Intelligence Engine (Kotlin / Android).
**Method:** every entry below was read from source and its `path:line-line` citation re-verified by a
second pass. Nothing here is inferred from the README or from indicator convention.
**Parity status:** ✅ **VERIFIED against the live app on 2026-08-23.** The user compared the
app's Chart legend card against `tools/plot_chart.py` output on 1H and 30m with UT inputs
**2.0 / 10** (confirmed matching) and reported all values agreeing — UT Dynamic Level,
EMA 7/14/28, Bollinger upper/lower, VWAP, plus the S/R zone placement. The golden-CSV
export in `tools/export_golden.md` is therefore no longer required; it remains available
if a future change needs re-checking.

All paths are relative to `C:\inetpub\Claude\ITSupport`.
Shorthand: `IND/` = `app/src/main/java/com/example/itsupport/analysis/indicators/`,
`ANA/` = `app/src/main/java/com/example/itsupport/analysis/`,
`CORE/` = `app/src/main/java/com/example/itsupport/core/`.

---

## 0. Facts that govern everything below

| Fact | Citation | Consequence for the backtest |
|---|---|---|
| The gold symbol is **`XAUUSDT`**, not `XAUTUSDT` | `data/remote/BybitApi.kt:61` | ⛔ The master prompt names `XAUTUSDT`. The code and its comment (`BybitApi.kt:53-60`) explicitly reject that contract as "Tether Gold… a thinner market". **See OPEN_QUESTIONS Q1.** |
| Category is `linear`, host is `api.bytick.com` | `data/remote/BybitApi.kt:51`, `:12-14` | Matches the spec. |
| Timeframes are **M5, M15, M30, H1, H4 only** | `CORE/Timeframe.kt:6-11` | There is **no 1m** in the app. 1m is a backtest-only addition for intrabar TP/SL resolution; no app indicator is defined on it. |
| Bybit interval codes: `5/15/30/60/240` | `data/remote/BybitMarketDataSource.kt:95-102` | Matches the spec. |
| Every indicator runs **twice**: CONFIRMED (closed bars) and LIVE (closed + forming) | `CORE/Candle.kt:31-41`, `IND/IndicatorSet.kt:186-204` | The backtest is bar-closed by construction ⇒ **port the CONFIRMED path only**. Every LIVE-sourced rule is flagged below. |
| `CandleSeries.closed` drops the last bar iff `!complete` | `CORE/Candle.kt:37-38` | Equivalent to the spec's "drop the last kline". |
| `complete = startTime + intervalMillis <= nowUtc` | `data/remote/BybitMarketDataSource.kt:134` | Derived from the **device clock**, not from the exchange. In a backtest this is deterministic. |
| The app keeps only a rolling **500-bar** window | `data/MarketDataManager.kt:250` (`DEFAULT_CANDLE_COUNT = 500`), `:283` (`takeLast(maxBars)`) | ⛔ **Every recursive indicator seeds from the start of that window.** UT Bot, ATR, RSI, ADX and EMA values therefore depend on how much history is resident. **See OPEN_QUESTIONS Q4** — this materially changes signals. |
| Minimum usable history is 60 closed bars | `CORE/Candle.kt:45-49` (`MIN_CANDLES = 60`) | Used by MTF and alerts, **but not by the orchestrator** — see §9.3. |
| `analysis` package has zero Android imports | README "Architecture" | The whole judgement chain is portable as pure functions. |

---

## 1. Shared series maths (`IND/MathSeries.kt`)

These are helpers, not display indicators, but every formula below depends on picking the right one.

### 1.1 `sma` — Simple moving average
| Field | Value |
|---|---|
| **Source** | `IND/MathSeries.kt:15-25` |
| **Inputs** | any `List<Double>`. Only caller: Bollinger basis, over `closes` (`IND/IndicatorSet.kt:111`, `:124`) |
| **Parameters** | `period` — supplied by caller; Bollinger passes 20 (`IND/Bollinger.kt:29`) |
| **Formula** | rolling running sum: `s += v[i]; if i>=period: s -= v[i-period]; if i>=period-1: out[i] = s/period` |
| **State** | stateless (value at `i` depends only on `v[i-period+1..i]`) |
| **Warm-up** | first non-null at index `period-1`. Pinned by `app/src/test/.../MathSeriesTest.kt:9-18` |
| **Repaint** | **No** — trailing window only |

### 1.2 `rma` — Wilder smoothing (Pine `ta.rma`)
| Field | Value |
|---|---|
| **Source** | `IND/MathSeries.kt:32-42` |
| **Inputs** | any `List<Double>`. Callers: `Atr.values` (`IND/Atr.kt:26`), `Adx.calculate` ×3 (`IND/Adx.kt:50-52`) |
| **Parameters** | `period` from caller |
| **Formula** | `seed = mean(v[0..period-1])`; `out[period-1] = seed`; then `out[i] = (out[i-1]*(period-1) + v[i]) / period` |
| **State** | **recursive**, α = `1/period`. **Seed = SMA of the first `period` values** — *not* `v[0]`, *not* an EMA α of `2/(p+1)` |
| **Warm-up** | first non-null at index `period-1`. Pinned by `MathSeriesTest.kt:20-32` |
| **Repaint** | **No look-ahead.** But warm-up-sensitive: seed slides with the 500-bar window (see §0) |

> `IND/MathSeries.kt:28-31` is explicit that substituting an EMA here "would silently de-synchronise
> this app from TradingView". The Python port must use α = 1/period with an SMA seed.

### 1.3 `rmaNullable` — Wilder smoothing over a series with leading nulls
| Field | Value |
|---|---|
| **Source** | `IND/MathSeries.kt:48-64` |
| **Inputs** | `List<Double?>`. Sole caller: the DX series inside ADX (`IND/Adx.kt:71`) |
| **Formula** | `firstIndex` = index of first non-null; `seedIndex = firstIndex + period - 1`; `out[seedIndex] = mean(v[firstIndex .. firstIndex+period-1])`; then recursive as `rma`, **skipping** nulls (`v[i] ?: continue`, `:60`) |
| **State** | recursive; alignment preserved (output length == input length) |
| **Warm-up** | first non-null at `firstIndex + period - 1` |
| **Repaint** | **No** |
| **Edge case** | a null *after* the seed leaves `out[i] = null`, and the next non-null iteration dereferences `out[i-1]!!` (`:61`) → would throw. Unreachable with real ADX input. **OPEN_QUESTIONS Q14** |

### 1.4 `stdevPopulation` — population standard deviation (Pine `ta.stdev`)
| Field | Value |
|---|---|
| **Source** | `IND/MathSeries.kt:70-85` |
| **Formula** | per index `i ≥ period-1`: `mean` of the window, then `sqrt( Σ(v[j]-mean)² / period )` — **divisor is `period`, not `period-1`** |
| **Warm-up** | first non-null at index `period-1` |
| **Repaint** | **No** |

> `IND/MathSeries.kt:67-68`: using the sample formula "would widen every Bollinger band slightly".
> In pandas this is `.rolling(period).std(ddof=0)`.

### 1.5 `percentileRank`
| Field | Value |
|---|---|
| **Source** | `IND/MathSeries.kt:91-94` |
| **Formula** | `count(x in sample where x <= value) / len(sample)`; returns `0.5` for an empty sample |
| **Note** | inclusive `<=`, so ranking a value against a sample containing itself never returns 0 |

---

## 2. ATR (`IND/Atr.kt`)

### 2.1 True Range
| Field | Value |
|---|---|
| **Source** | `IND/Atr.kt:14-22` |
| **Inputs** | high, low, previous close |
| **Formula** | `i == 0` → `high - low` (no previous close); else `max(high-low, abs(high-prevClose), abs(low-prevClose))` |
| **State** | stateless (1-bar memory) |
| **Repaint** | **No** |

> `IND/Atr.kt:10-13`: the degenerate first bar "matches Pine's `tr(true)` and matters because the
> UT Bot seeds off it."

### 2.2 ATR
| Field | Value |
|---|---|
| **Name in UI** | not shown directly; drives every ATR-unit threshold |
| **Source** | `IND/Atr.kt:25-26` |
| **Parameters** | `DEFAULT_PERIOD = 14` (`IND/Atr.kt:8`); wired as `IndicatorConfig.atrPeriod = 14` (`IND/IndicatorSet.kt:12`) |
| **Formula** | `rma(trueRange(candles), 14)` |
| **State** | recursive via `rma` — seed = SMA of first 14 TR values |
| **Warm-up** | first non-null at index 13 |
| **Repaint** | **No** (warm-up-sensitive, §0) |

⚠️ **Two different ATRs exist.** `IndicatorSet.atr` uses period **14**; the UT Bot internally
recomputes its own ATR with period **10** (`IND/UtBot.kt:85`, `IND/IndicatorSet.kt:11`). Every
"in ATR units" threshold in the app uses the **14** series. **OPEN_QUESTIONS Q5.**

### 2.3 ATR%
| Field | Value |
|---|---|
| **Source** | `IND/Atr.kt:29-33` |
| **Formula** | `atr[i] / close[i] * 100.0`; null if either is null or `close == 0` |
| **Consumer** | `VolatilityClassifier` (§6.1) — the *only* consumer |

---

## 3. EMA stack (`IND/Ema.kt`)

### 3.1 `Ema.values`
| Field | Value |
|---|---|
| **UI name** | "EMA 7/14/28" (`ui/dashboard/DashboardReport.kt:109`) |
| **Source** | `IND/Ema.kt:9-20` |
| **Inputs** | `closes` = `candles.map { it.close }` (`IND/IndicatorSet.kt:111`) |
| **Parameters** | `emaFast = 7`, `emaMid = 14`, `emaSlow = 28` (`IND/IndicatorSet.kt:18-20`), applied at `IND/IndicatorSet.kt:119-121` |
| **Formula** | `seed = mean(src[0..period-1])`; `out[period-1] = seed`; `k = 2/(period+1)`; `out[i] = (src[i] - out[i-1])*k + out[i-1]` |
| **State** | **recursive**, SMA-seeded (matches Pine `ta.ema`) |
| **Warm-up** | first non-null at index `period-1` → 6 / 13 / 27 |
| **Repaint** | **No** (warm-up-sensitive, §0) |

### 3.2 `EmaStack.alignmentAt` → `EmaAlignment`
| Field | Value |
|---|---|
| **UI name** | the value of the "EMA 7/14/28" row — literally `BULLISH` / `BEARISH` / `MIXED` / `UNDEFINED` |
| **Source** | `IND/Ema.kt:34-44`; enum at `IND/Ema.kt:55` |
| **Emitted** | `a>b && b>c` → `BULLISH`; `a<b && b<c` → `BEARISH`; any null → `UNDEFINED`; else `MIXED` |
| **Repaint** | **No** |

### 3.3 `EmaStack.separationInAtr`
| Field | Value |
|---|---|
| **Source** | `IND/Ema.kt:47-52` |
| **Formula** | `(ema7 - ema28) / atr14`; null if either EMA is null or `atr <= 0` |
| **Repaint** | **No** |

### 3.4 `EmaSpreadTrend` (computed, **never consumed**)
| Field | Value |
|---|---|
| **Source** | enum `IND/Ema.kt:58`; assignment `IND/IndicatorSet.kt:90-103` |
| **Parameters** | `spreadLookback = 10` (`IND/IndicatorSet.kt:23`); hysteresis 1.15 / 0.85; epsilon 0.0001 |
| **Formula** | `now = abs(sep(i))`, `before = abs(sep(i-10))`; `before <= 0.0001` → `STABLE`; `now > before*1.15` → `EXPANDING`; `now < before*0.85` → `COMPRESSING`; else `STABLE`; any null → `UNDEFINED` |
| **Repaint** | **No** |
| **Status** | **Dead output** — present on every snapshot (`IND/IndicatorSet.kt:63`, `:151`) but read by no engine, alert or screen. **OPEN_QUESTIONS Q16** |

### 3.5 `IndicatorSet.extensionFromSlowEma`
| Field | Value |
|---|---|
| **Source** | `IND/IndicatorSet.kt:82-88` |
| **Formula** | `(close - ema28) / atr14`; null if `atr <= 0` |
| **Note** | `LocationAnalyzer` recomputes the identical quantity inline (`ANA/confluence/LocationAnalyzer.kt:72`) rather than calling this |

---

## 4. Momentum & channel indicators

### 4.1 RSI (`IND/Rsi.kt`)
| Field | Value |
|---|---|
| **UI name** | "RSI" (`ui/dashboard/DashboardReport.kt:110`), shown rounded to 0 dp |
| **Source** | `IND/Rsi.kt:13-42` |
| **Inputs** | `closes` |
| **Parameters** | `DEFAULT_PERIOD = 14` (`IND/Rsi.kt:5`), wired at `IND/IndicatorSet.kt:13`, applied `IND/IndicatorSet.kt:123` |
| **Formula** | gains/losses from `src[i]-src[i-1]` (`:19-23`); `avgGain = mean(gains[1..period])`, `avgLoss = mean(losses[1..period])` (`:28-33`); `out[period] = rsiFrom(...)`; then Wilder recursion `avg = (avg*(period-1) + x[i]) / period` (`:37-38`) |
| **`rsiFrom`** | `IND/Rsi.kt:44-49` — both zero → **50.0**; `avgLoss==0` → 100; `avgGain==0` → 0; else `100 - 100/(1 + avgGain/avgLoss)` |
| **State** | recursive; seeded from bars `1..period` |
| **Warm-up** | first non-null at index **`period`** (= 14), *one later than* ATR/EMA |
| **Repaint** | **No** |

> ⚠️ RSI does **not** call `MathSeries.rma`; it reimplements Wilder smoothing inline (`IND/Rsi.kt:28-40`).
> The flat-window → 50 branch is a deliberate deviation from the naive formula (`IND/Rsi.kt:9-11`).

### 4.2 Bollinger Bands (`IND/Bollinger.kt`)
| Field | Value |
|---|---|
| **UI name** | "BB %B" (`ui/dashboard/DashboardReport.kt:121`, 2 dp). Bands drawn on the chart overlay |
| **Source** | `IND/Bollinger.kt:32-58` |
| **Inputs** | `closes` |
| **Parameters** | `DEFAULT_PERIOD = 20`, `DEFAULT_MULTIPLIER = 2.0` (`IND/Bollinger.kt:29-30`), wired `IND/IndicatorSet.kt:15-16`, applied `:124` |
| **Formula** | `middle = sma(src,20)`; `sd = stdevPopulation(src,20)`; `upper = middle + 2*sd`; `lower = middle - 2*sd`; `width = (upper-lower)/middle` (null if `middle==0`); `percentB = (src[i]-lower)/(upper-lower)` (null if `upper==lower`) |
| **State** | stateless |
| **Warm-up** | first non-null at index 19 |
| **Repaint** | **No** |

**`widthPercentile`** — `IND/Bollinger.kt:18-24`. `lookback = 100` (default, never overridden),
sample = `width[max(0,i-99) .. i]` non-null; returns null if fewer than 20 samples; else
`percentileRank(sample, width[i])`.
**Status: dead output** — exposed as `bbWidthPercentile` (`IND/IndicatorSet.kt:70`, `:158`) and read
by nothing. **OPEN_QUESTIONS Q16.**

### 4.3 ADX / DMI (`IND/Adx.kt`)
| Field | Value |
|---|---|
| **UI name** | "ADX" (`ui/dashboard/DashboardReport.kt:111`, 0 dp) |
| **Source** | `IND/Adx.kt:33-73` |
| **Inputs** | high, low, close (via `Atr.trueRange`) |
| **Parameters** | `DEFAULT_PERIOD = 14` (`IND/Adx.kt:25`), wired `IND/IndicatorSet.kt:14`, applied `:125` |
| **Guard** | `n < period + 1` → all nulls (`IND/Adx.kt:35-38`) |
| **Formula** | see below |
| **State** | recursive (three `rma` + one `rmaNullable`) |
| **Warm-up** | `+DI`/`-DI` first non-null at index 13; **ADX first non-null at index 26** (`rmaNullable` seeds at `13 + 14 - 1`) |
| **Repaint** | **No** |

```python
tr = true_range(candles)                       # Adx.kt:40
plus_dm  = [0.0]*n; minus_dm = [0.0]*n         # index 0 stays 0.0
for i in 1..n-1:                               # Adx.kt:43-48
    up   = high[i] - high[i-1]
    down = low[i-1] - low[i]
    plus_dm[i]  = up   if (up > down and up > 0)   else 0.0
    minus_dm[i] = down if (down > up and down > 0) else 0.0

sm_tr    = rma(tr,       14)                   # Adx.kt:50-52
sm_plus  = rma(plus_dm,  14)
sm_minus = rma(minus_dm, 14)

for i in 0..n-1:                               # Adx.kt:58-69
    if sm_tr[i] is None or sm_plus[i] is None or sm_minus[i] is None or sm_tr[i] == 0.0:
        continue                               # leaves plusDi/minusDi/dx = None
    pdi = 100.0 * sm_plus[i]  / sm_tr[i]
    mdi = 100.0 * sm_minus[i] / sm_tr[i]
    dx[i] = 0.0 if (pdi + mdi) == 0.0 else 100.0 * abs(pdi - mdi) / (pdi + mdi)

adx = rma_nullable(dx, 14)                     # Adx.kt:71
```

⚠️ **Port-critical:** `plus_dm`/`minus_dm` are smoothed **from array index 0**, and index 0 holds a
synthetic `0.0`. `rma` therefore seeds on `[0.0, dm1 … dm13]`. Combined with `tr[0] = high-low`,
this is a small, deliberate-looking deviation from the textbook DMI seed. Replicate exactly.
**OPEN_QUESTIONS Q13.**

**`directionalSign`** — `IND/Adx.kt:12-20`: `+DI > -DI` → `1`; `-DI > +DI` → `-1`; equal or null → `0`.

**ADX thresholds are quoted at three different values in three places** — see OPEN_QUESTIONS Q6.

### 4.4 VWAP (`IND/Vwap.kt`)
| Field | Value |
|---|---|
| **UI name** | "VWAP", rendered only as "price above" / "price below" (`ui/dashboard/DashboardReport.kt:113-120`) |
| **Source** | `IND/Vwap.kt:19-42` |
| **Inputs** | `typicalPrice = (high+low+close)/3` (`CORE/Candle.kt:20`) weighted by `tickVolume` |
| **Parameters** | `sessionOffsetMillis`, default `0L` = **00:00 UTC anchor** (`IND/IndicatorSet.kt:21`). No production call site ever passes non-zero. **OPEN_QUESTIONS Q17** |
| **Formula** | `session = floorDiv(timeUtc - offset, 86_400_000)`; on session change reset `cumPV` and `cumVol`; `cumPV += typical*vol`; `cumVol += vol`; `out[i] = cumPV/cumVol` |
| **Guard** | returns **all nulls** if `candles.all { tickVolume <= 0 }` (`IND/Vwap.kt:22`) |
| **State** | **stateful within a session**, resets at each UTC midnight |
| **Warm-up** | value on the very first bar, but the first session in the window is **truncated** (starts mid-session) |
| **Repaint** | **No** |
| **⛔ Suppressed on H4** | `IND/IndicatorSet.kt:126-132` force-nulls the whole VWAP series when `timeframe == H4`. The VWAP dashboard row and the VWAP leg of `zone_ema_confluence` therefore cannot exist on 4H |

### 4.5 Relative tick volume (`IND/RelativeVolume.kt`)
| Field | Value |
|---|---|
| **UI name** | "Tick volume", rendered as "N.NNx its 20-bar average" (`ui/dashboard/DashboardReport.kt:122-125`) |
| **Source** | `IND/RelativeVolume.kt:16-26` |
| **Parameters** | `DEFAULT_PERIOD = 20` (`IND/RelativeVolume.kt:13`), wired `IND/IndicatorSet.kt:17`, applied `:133` |
| **Formula** | for `i >= period`: `average = mean(vol[i-20 .. i-1])` — **the current bar is excluded**; `out[i] = vol[i]/average` if `average > 0` |
| **State** | stateless |
| **Warm-up** | first non-null at index `period` (= 20) |
| **Repaint** | **No** |

**`forming`** — `IND/RelativeVolume.kt:35-52`. LIVE-only: divides by `average * elapsedFraction`,
with `MIN_ELAPSED = 0.05` (`:55`). ⚠️ In the first seconds of a bar the divisor is 0.05, so the
reading can reach ~20×. **Not reachable in a closed-bar backtest — do not port for signals.**

**Volume source note:** Bybit volume is scaled `(volume * 100).toLong()`
(`data/remote/BybitMarketDataSource.kt:133`). Only *relative* volume is ever used, so the scale
cancels — but the Python port must apply the same integer truncation to match bit-for-bit.

---

## 5. UT Bot — the "UT Dynamic Level" (`IND/UtBot.kt`)

**This is the app's headline level and the anchor of the strongest alerts.**

| Field | Value |
|---|---|
| **UI name** | **"UT Dynamic Level"** (`ui/dashboard/DashboardReport.kt:103`). Explicitly **not** support or resistance — `IND/UtBot.kt:9-10`, `ANA/structure/SupportResistanceEngine.kt:46-47`, `ui/dashboard/DashboardReport.kt:160` |
| **Source** | `IND/UtBot.kt:73-129` |
| **Inputs** | `close` as `src`; its own ATR over high/low/close |
| **Parameters** | `DEFAULT_KEY_VALUE = 3.0`, `DEFAULT_ATR_PERIOD = 10` (`IND/UtBot.kt:70-71`). **But** the user-facing default is `2.0 / 10` (`data/SettingsStore.kt:30-31`). ⛔ **OPEN_QUESTIONS Q2** |
| **Configurable at** | Settings gear → two inputs; persisted as `ut_key_value` / `ut_atr_period` (`data/SettingsStore.kt:153-154`), read at `:122-123` |
| **State** | **recursive and path-dependent** — `stop[i]` depends on `stop[i-1]`, `position[i]` carries forward |
| **Warm-up** | `stop` first non-null at index `atrPeriod-1` (= 9); `buy`/`sell` first possible at index `atrPeriod` (= 10), because they require `prevStopRaw != null` |
| **Repaint** | **No look-ahead.** ⛔ But **strongly** warm-up-sensitive — see the seed note below |

```python
atr = Atr.values(candles, atrPeriod)          # Wilder RMA, period 10 by default
stop = [None]*n; pos = [0]*n; buy = [False]*n; sell = [False]*n

for i in range(n):
    if atr[i] is None:                        # UtBot.kt:89-94
        stop[i] = None
        pos[i]  = pos[i-1] if i > 0 else 0
        continue

    nLoss   = keyValue * atr[i]               # UtBot.kt:96
    src     = close[i]
    prevSrc = close[i-1] if i > 0 else src    # UtBot.kt:98
    prevStopRaw = stop[i-1] if i > 0 else None
    prevStop    = prevStopRaw if prevStopRaw is not None else 0.0   # nz(...,0)  UtBot.kt:100

    if   src > prevStop and prevSrc > prevStop: stop[i] = max(prevStop, src - nLoss)
    elif src < prevStop and prevSrc < prevStop: stop[i] = min(prevStop, src + nLoss)
    elif src > prevStop:                        stop[i] = src - nLoss
    else:                                       stop[i] = src + nLoss     # UtBot.kt:102-107

    if   prevSrc < prevStop and src > prevStop: pos[i] = 1
    elif prevSrc > prevStop and src < prevStop: pos[i] = -1
    else:                                       pos[i] = pos[i-1] if i > 0 else 0   # UtBot.kt:109-113

    if prevStopRaw is not None:                                        # UtBot.kt:116-120
        buy[i]  = src > stop[i] and prevSrc <= prevStopRaw
        sell[i] = src < stop[i] and prevSrc >= prevStopRaw
```

**Four behaviours replicated on purpose** (`IND/UtBot.kt:56-66`) — do not "clean up" any of them:

1. ATR is Wilder RMA, so the first value is at index `period-1`.
2. `nz(xATRTrailingStop[1], 0)` → **the first ATR-available bar compares against `0`.** Gold is
   positive, so the first branch wins and the stop initialises to `close - nLoss` — a **bullish
   seed**. Substituting `close` for that `0` moves every subsequent flip.
3. `pos` only changes on an actual cross, otherwise carries forward.
4. `crossover`/`crossunder` compare current close vs **current** stop and previous close vs
   **previous** stop, and are false while the stop is still `na`.

> ⛔ **Backtest consequence of (2) + the 500-bar window.** The app re-seeds the recursion at the
> start of whatever slice it holds. A backtest over 2 years computed in one pass will produce a
> *different* trailing-stop path than the app showed live. The README itself says the app requests
> 500 rather than 250 bars because "the UT trailing stop is path-dependent — it ratchets via
> `max(prevStop, src - nLoss)` and only resets on a cross, so shallow history can leave the level
> somewhere a deeper chart would not." **OPEN_QUESTIONS Q4 — this is the single largest parity
> decision in the project.**

### 5.1 `UtBotResult.statusAt` → `UtStatus`
| Field | Value |
|---|---|
| **Source** | `IND/UtBot.kt:20-42`; data class `IND/UtBot.kt:45-51` |
| **`level`** | `trailingStop[i]` |
| **`bias`** | `close > level` → `BULLISH`; `close < level` → `BEARISH`; equal or null → `NEUTRAL` (`IND/UtBot.kt:23-28`) |
| **`crossedThisBar`** | `buy[i] || sell[i]` |
| **`crossDirection`** | `buy[i]` → `BULLISH`; `sell[i]` → `BEARISH`; else `NEUTRAL` |

---

## 6. Regime & volatility

### 6.1 `VolatilityClassifier` → `VolatilityBand`
| Field | Value |
|---|---|
| **UI name** | "Volatility" tile; values `LOW` / `NORMAL` / `HIGH` / `EXTREME` (`ui/dashboard/DashboardReport.kt:75`) |
| **Source** | `ANA/regime/VolatilityClassifier.kt:30-43`; enum `CORE/Enums.kt:37` |
| **Inputs** | the **ATR% series of the direction anchor (H1), CONFIRMED slice** (`ANA/MarketAnalysisEngine.kt:65`) |
| **Parameters** | `DEFAULT_LOOKBACK = 150` (`:27`), `MIN_SAMPLE = 40` (`:28`) |
| **Formula** | `current = last non-null of the WHOLE series`; `sample = takeLast(150).filterNotNull()`; if `len(sample) < 40` → `(NORMAL, current, None)`; else `pct = percentileRank(sample, current)` |
| **Bands** | `pct < 0.25` → LOW; `< 0.75` → NORMAL; `< 0.90` → HIGH; else EXTREME (`:36-41`) |
| **Repaint** | **No** |
| ⚠️ **Degraded branch** | thin sample returns `NORMAL` with `percentile = null` — **indistinguishable in the UI from a measured NORMAL**, and while in that state the EXTREME veto, the `HIGH_VOLATILITY` tag and the `extreme_volatility` alert are all structurally unable to fire. **OPEN_QUESTIONS Q7** |

### 6.2 `MarketRegimeEngine` → `RegimeTag`
| Field | Value |
|---|---|
| **UI name** | regime description sentence, e.g. "Trending up (ADX 27) with normal volatility." |
| **Source** | `ANA/regime/MarketRegimeEngine.kt:38-86`; enum `CORE/Enums.kt:50-52` |
| **Labels** | `TREND_UP`, `TREND_DOWN`, `RANGE`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `NEWS_RISK` |
| **Inputs** | H1 CONFIRMED snapshot, H1 `StructureState`, `VolatilityReading`, news `Grade` (`ANA/MarketAnalysisEngine.kt:78-83`) |
| **Parameter** | `ADX_TREND_FLOOR = 20.0` (`:36`) |
| **Repaint** | **No** directly; inherits structure's instability (§7.1) |

```python
trending = (adx or 0.0) >= 20.0                              # :47
votes = [ema_vote, adx_direction, structure_vote]            # :49-61
#   ema_vote:       BULLISH=+1, BEARISH=-1, else 0
#   adx_direction:  +DI>-DI = +1, -DI>+DI = -1, else 0
#   structure_vote: BULLISH=+1, BEARISH=-1, RANGE=0
net = sum(votes)
primary = TREND_UP   if (trending and net >=  2) else \
          TREND_DOWN if (trending and net <= -2) else RANGE   # :64-68

tags = {primary}                                              # :70-78
if volatility.band in (HIGH, EXTREME): tags.add(HIGH_VOLATILITY)
elif volatility.band == LOW:           tags.add(LOW_VOLATILITY)
if news_risk in (HIGH, EXTREME):       tags.add(NEWS_RISK)
```

Description strings: `ANA/regime/MarketRegimeEngine.kt:88-101` — trend text uses `adx.toInt()`
(truncation, not rounding).

---

## 7. Market structure

### 7.1 `SwingDetector` — fractal pivots + zigzag filter ⛔ **REPAINT-CRITICAL**
| Field | Value |
|---|---|
| **UI name** | drawn as swing markers on the chart; feeds "Market Structure" verdict |
| **Source** | `ANA/structure/SwingDetector.kt:41-102` |
| **Inputs** | high/low of the **CONFIRMED** candle slice, plus the ATR-14 series (`ANA/mtf/TimeframeAnalyzer.kt:50-54`) |
| **Parameters** | `lookback` = **3 for H1/H4, 2 for M5/M15/M30** (`:24-27`); `minSeparationAtr = 0.5` (`:20`, never overridden) |

```python
# detectRaw — SwingDetector.kt:50-69
if len(candles) < lookback*2 + 1: return []
for i in range(lookback, len(candles) - lookback):        # :54  <-- NOTE the upper bound
    isHigh = isLow = True
    for k in range(1, lookback+1):                        # :59-63
        if candle[i].high <= candle[i-k].high or candle[i].high <= candle[i+k].high: isHigh = False
        if candle[i].low  >= candle[i-k].low  or candle[i].low  >= candle[i+k].low:  isLow  = False
        if not isHigh and not isLow: break
    if isHigh: out.append(SwingPoint(i, high, time, HIGH))
    if isLow:  out.append(SwingPoint(i, low,  time, LOW))

# filterAlternating — SwingDetector.kt:71-102
fallbackAtr = mean(non-null atr)   # if finite and > 0
for point in raw:
    last = accepted[-1] if accepted else None
    if last is None: accepted.append(point); continue
    if last.type == point.type:                           # :87-95
        more_extreme = point.price > last.price if HIGH else point.price < last.price
        if more_extreme: accepted[-1] = point             # <-- IN-PLACE REPLACEMENT
        continue
    reference = atr[point.index] or fallbackAtr
    threshold = (reference or 0.0) * 0.5                  # :97-98
    if abs(point.price - last.price) >= threshold: accepted.append(point)
```

**Repaint verdict: `detectRaw` is causally safe; `filterAlternating` is NOT stable.**

* **Causally safe:** the loop bound is `len(candles) - lookback` (`:54`), so at bar `t` the newest
  possible pivot sits at `t - lookback`. A pivot is **confirmed `lookback` bars late** and never
  uses a bar beyond its own index + lookback. There is no forward leak *provided the series is
  truncated at `t`*.
* **Not stable:** `accepted[accepted.lastIndex] = point` (`:93`) **retroactively replaces the last
  accepted pivot** when a later, more extreme same-type pivot arrives. So `lastSwingHigh` /
  `lastSwingLow` — and therefore `StructureState`, BOS and CHoCH — can change *without any new bar
  closing at that index*. The accepted chain is also order-dependent from its first pivot, so
  dropping old bars out of the 500-bar window can flip every downstream accept/reject.

> ⛔ **Backtest requirement:** structure **must** be recomputed on an expanding window ending at
> bar `t` for every `t`. Computing it once over the whole series and indexing backwards is a
> look-ahead bug. **OPEN_QUESTIONS Q3.**

### 7.2 `MarketStructureEngine` → `StructureState`, BOS, CHoCH
| Field | Value |
|---|---|
| **UI name** | "Market Structure" section; verdict `Bullish` / `Bearish` / `Range` (`ANA/MarketAnalysisEngine.kt:236-240`) |
| **Source** | `ANA/structure/MarketStructureEngine.kt:45-105`; enum `CORE/Enums.kt:31` |
| **Repaint** | inherits §7.1 |

```python
swings = SwingDetector.detect(...)
highs = [s for s in swings if s.type == HIGH]; lows = [...]
lastHigh, prevHigh = highs[-1], highs[-2]      # :55-58
lastLow,  prevLow  = lows[-1],  lows[-2]

higherHigh = lastHigh.price > prevHigh.price   # :60-63
higherLow  = lastLow.price  > prevLow.price
lowerHigh  = lastHigh.price < prevHigh.price
lowerLow   = lastLow.price  < prevLow.price

state = BULLISH if (higherHigh and higherLow) else \
        BEARISH if (lowerHigh  and lowerLow)  else RANGE      # :65-69

close = candles[-1].close                       # :71  <-- LAST BAR ONLY
brokeAbove = lastHigh is not None and close > lastHigh.price  # :72
brokeBelow = lastLow  is not None and close < lastLow.price   # :73

bos = choch = NEUTRAL                           # :76-92
if state == BULLISH:  bos = BULLISH  if brokeAbove else bos;  choch = BEARISH if brokeBelow else choch
if state == BEARISH:  bos = BEARISH  if brokeBelow else bos;  choch = BULLISH if brokeAbove else choch
if state == RANGE:    bos = BULLISH  if brokeAbove else bos;  bos   = BEARISH if brokeBelow else bos
```

⚠️ BOS/CHoCH is a **latched state, not a one-shot event**: `close > lastSwingHigh` stays true until
a new swing forms. The alert layer works around this with a 6-bar cooldown rather than edge
detection (`ANA/alerts/AlertEngine.kt:269-273`, `:304`). A backtest treating it as an event must
add its own edge detection. **OPEN_QUESTIONS Q10.**
⚠️ In `RANGE`, a single bar breaking *both* extremes sets `bos = BEARISH` (the second assignment
wins, `:90`). **OPEN_QUESTIONS Q11.**

### 7.3 `SupportResistanceEngine` → `PriceZone`, `KeyLevels`
| Field | Value |
|---|---|
| **UI name** | "KEY LEVELS" → "Resistance" / "Current" / "Support" (`ui/dashboard/DashboardReport.kt:127-131`) |
| **Source** | `ANA/structure/SupportResistanceEngine.kt:68-161` |
| **Inputs** | swings from **M30, H1, H4 only** (`ANA/MarketAnalysisEngine.kt:141-144`); `dailyCandles` = **H1 CONFIRMED** candles (`:149`); `referenceAtr` = **H1 CONFIRMED ATR-14** (`:150`) |
| **Parameters** | `ZONE_TOLERANCE_ATR = 0.35` (`:54`), `MAX_ZONES_PER_SIDE = 3` (`:55`), timeframe weights H4=1.0 / H1=0.8 / M30=0.5 (`:57-61`), recency `exp(-age/12.0)` floored at 0.25 (`:85`) |
| **Session weights** | previous-day high 0.9, low 0.9, close 0.6; session high 0.7, low 0.7 (`:121-129`) |
| **Day boundary** | `floorDiv(timeUtc, 86_400_000)` = **UTC midnight** (`:114-115`) |
| **Repaint** | ⛔ **Yes — drifts intrabar.** "Session high/low" is recomputed from every bar of the current day, so the zone band moves as the session extends. Also inherits §7.1 |

```python
tolerance = referenceAtr * 0.35                        # :74
if tolerance <= 0: return Empty

raw = []
for tf, swings in swingsByTimeframe.items():           # :79-92
    base = {H4:1.0, H1:0.8, M30:0.5}.get(tf)
    if base is None: continue
    for pos, swing in enumerate(swings):
        age = len(swings) - 1 - pos
        recency = max(exp(-age/12.0), 0.25)
        raw.append(RawLevel(swing.price, base*recency, f"{tf.label} swing high|low"))

raw += session_levels(dailyCandles)                    # :94  (see below)
zones = cluster(raw, tolerance)                        # :98

resistance = sorted([z for z in zones if z.mid >  price], key=-strength)[:3]   # :99-102
resistance.sort(key=lambda z: z.distanceFrom(price))
support    = sorted([z for z in zones if z.mid <= price], key=-strength)[:3]   # :103-106
support.sort(key=lambda z: z.distanceFrom(price))

# cluster — :135-160 : sort by price, then greedily chain
#   if abs(level.price - bucket[-1].price) <= tolerance: bucket.append(level)
#   else: flush(); bucket = [level]
#   zone = PriceZone(low=min, high=max, strength=SUM of weights, sources=distinct)
```

⚠️ `cluster` chains **transitively** — each level is compared to the *previous level in the bucket*,
not to the bucket's origin, so a zone can end up arbitrarily wider than `tolerance`. **Q12.**
⚠️ `nearestResistance`/`nearestSupport` (`:31-32`) are the nearest **among the 3 strongest**, not
the nearest overall. **Q12.**
⚠️ A zone straddling the current price is classified purely by its `mid` (`:99`, `:103`).
⚠️ `PriceZone.contains` (`:17`) is never called anywhere. The canonical "at a zone" test is
`distanceFrom(price)/atr <= threshold` in the alert layer.

**`PriceZone`** — `:9-25`: `mid = (low+high)/2`; `distanceFrom(p)` = `low-p` if below, `p-high` if
above, **`0.0` if inside**.

---

## 8. Multi-timeframe

### 8.1 `TimeframeAnalyzer.directionalScore`
| Field | Value |
|---|---|
| **UI name** | the per-timeframe bias in the "MULTI-TIMEFRAME" block (`ui/dashboard/DashboardReport.kt:85-97`) |
| **Source** | `ANA/mtf/TimeframeAnalyzer.kt:77-98` |
| **Parameter** | `ADX_FULL_WEIGHT = 40.0` (`:42`) |
| **Repaint** | inherits §7.1 via `structureVote` |

```python
emaVote = +1.0 if BULLISH else -1.0 if BEARISH else 0.0        # :80-84
utVote  = ut.bias.sign                                          # :85   (+1/-1/0)
adxVote = adxDirection * clamp((adx or 0.0)/40.0, 0.0, 1.0)     # :86-87
trend   = clamp(emaVote*0.4 + utVote*0.4 + adxVote*0.2, -1, 1)  # :89
structureVote = +1.0 if BULLISH else -1.0 if BEARISH else 0.0   # :91-95
score   = clamp(trend*0.65 + structureVote*0.35, -1, 1)         # :97
```

⚠️ **The LIVE score reuses the CONFIRMED structure** — `structure` is computed once from
`indicators.confirmed.candles` (`:50-54`) and passed to both `directionalScore` calls (`:56-57`).
There is no LIVE structure. **Q18.**

`confirmedBias = Bias.fromScore(confirmedScore)`, `liveBias = Bias.fromScore(liveScore)` (`:63-64`).
`hasUnconfirmedSignal = hasFormingBar && liveBias != confirmedBias` (`:35-36`).

### 8.2 `Bias.fromScore`
`CORE/Enums.kt:15-19`: `score > deadZone` → BULLISH; `score < -deadZone` → BEARISH; else NEUTRAL.
**Default `deadZone = 0.15`.** ⚠️ `MarketAnalysisEngine` overrides it to **0.2** for Analysis-page
tone only (`ANA/MarketAnalysisEngine.kt:200`). Two dead zones coexist. **Q15.**

### 8.3 `MultiTimeframeEngine.combine`
| Field | Value |
|---|---|
| **UI name** | "Alignment NN%" + interpretation sentence (`ui/dashboard/DashboardReport.kt:98`) |
| **Source** | `ANA/mtf/MultiTimeframeEngine.kt:54-90` |
| **Weights** | M5 0.10, M15 0.15, M30 0.20, H1 0.25, H4 0.30 (`:46-52`) |
| **Subsets** | shortTerm = [M5, M15, M30]; higher = [H1, H4] (`CORE/Timeframe.kt:20-23`) |

```python
usable = {tf: a for tf, a in analyses.items() if a.usable}      # :58  (>= 60 closed bars)
if not usable: return Empty

shortTermScore = weighted(usable, weights, [M5,M15,M30])        # :61-63
higherScore    = weighted(usable, weights, [H1,H4])
overall        = weighted(usable, weights, [M5,M15,M30,H1,H4])
# weighted(): sum(score*w)/sum(w) over the subset, 0.0 if total weight <= 0   :92-106

overallSign = Bias.fromScore(overall).sign                      # :68
agreeingWeight = sum(w[tf] for tf,a in usable.items()
                     if overallSign != 0 and a.confirmedBias.sign == overallSign)   # :69-71
totalWeight    = sum(w[tf] for tf in usable)
alignment      = 0 if totalWeight <= 0 else int((agreeingWeight/totalWeight)*100)   # :73  TRUNCATES

conflict = shortBias != NEUTRAL and higherBias != NEUTRAL and shortBias != higherBias  # :75-77
```

⚠️ `alignment` **truncates** (`.toInt()`), never rounds. **Q19.**
⚠️ When `overall` is inside the dead zone, `overallSign == 0` ⇒ `alignment == 0` even if all five
timeframes agree perfectly on a weak read.
⚠️ `perTimeframe` exposes the **unfiltered** map (`:80`), while every score uses only `usable`.

Interpretation strings: `:108-122`.

---

## 9. Confluence, location and decision

### 9.1 `ConfluenceWeights`
`ANA/confluence/Evidence.kt:46-54` — trend **0.22**, structure **0.20**, higherTimeframe **0.18**,
momentum **0.15**, location **0.15**, volume **0.05**, volatility **0.05**.
`directionTotal = 0.75` (`:55`), `qualityTotal = 0.25` (`:56`).
Never overridden anywhere in `app/src`. **Q20.**

`EvidenceCategory` display names (`ANA/confluence/Evidence.kt:11-19`): `Trend`, `Structure`,
`Higher timeframe`, `Location`, `Momentum`, `Volume`, `Volatility`.

### 9.2 `ConfluenceEngine.score`
| Field | Value |
|---|---|
| **Source** | `ANA/confluence/ConfluenceEngine.kt:52-115` |
| **directionAnchor** | **H1** (`ANA/MarketAnalysisEngine.kt:38`) |
| **entryAnchor** | **M15** (`ANA/MarketAnalysisEngine.kt:41`) |
| **Parameter** | `ADX_FULL_WEIGHT = 40.0` (`:50`) — **a second, independent declaration** of the same constant as `TimeframeAnalyzer.kt:42`. **Q6** |

```python
trend      = trendCategory(H1)                 # :63
structure  = structureCategory(H1)             # :64
higherTf   = higherTimeframeCategory(mtf)      # :65
momentum   = momentumCategory(H1)              # :66

directionalScore = clamp(sum(c.score*c.weight for c in [trend,structure,higherTf,momentum])
                         / 0.75, -1, 1)        # :69-70
bias = Bias.fromScore(directionalScore)        # :71   (deadZone 0.15)

locationScore = location.forLong  if bias == BULLISH else \
                location.forShort if bias == BEARISH else \
                min(location.forLong, location.forShort)          # :75-79

analysisScore = clamp(round(abs(directionalScore)*100), 0, 100)   # :112
```

**TREND category** (`:122-171`) — weight 0.22:
```python
emaVote = +1.0/-1.0/0.0 by EmaAlignment                          # :127-131
utVote  = ut.bias.sign                                            # :147
adxVote = adxDirection * clamp(adx/40.0, 0, 1)                    # :156
score   = clamp(emaVote*0.4 + utVote*0.4 + adxVote*0.2, -1, 1)    # :169
```
Uses the **CONFIRMED** snapshot (`:123`). Evidence text bands: `<15` "Very weak trend", `<25`
"developing but not yet established", `<40` "Meaningful trend", else "Strong trend" (`:160-165`).

**STRUCTURE category** (`:173-208`) — weight 0.20:
```python
score = +1.0 if BULLISH else -1.0 if BEARISH else 0.0             # :175-179
if structure.hasChoch:
    score = choch.sign * 0.5                                       # :191  OVERRIDES, not adjusts
elif structure.hasBos:
    score = clamp(score + bos.sign*0.3, -1, 1)                     # :199
```
⚠️ When CHoCH fires, the first Evidence item still carries the **pre-override** score while the
category returns the CHoCH value. **Q21.**

**HIGHER_TIMEFRAME category** (`:214-240`) — weight 0.18:
```python
base  = mtf.analysisFor(H4).directionalScore or mtf.higherTimeframeScore   # :215-216
score = clamp(base * (0.5 + 0.5*(mtf.alignment/100.0)), -1, 1)             # :218
```
Deliberately H4, **not** H1, to avoid double-counting the TREND category (`:210-213`).
⚠️ The "Timeframe alignment NN%" evidence item carries ±0.2 (`:236`) but that number **never enters
the category score** — it is display-only. **Q22.**

**MOMENTUM category** (`:242-258`) — weight 0.15:
```python
score = clamp((rsi - 50.0)/25.0, -1, 1)     # :247   RSI 75 or 25 saturates
```
CONFIRMED RSI (`:243`). Narrative bands 70/50/30 at `:249-252`.

**LOCATION category** (`:80-82`) — weight 0.15. Score = `locationScore` above; see §9.4.

**VOLUME category** (`:261-292`) — weight 0.05. **Unsigned confirmation.**
⛔ Reads `entryAnchor.live.relativeVolume` (`:262`) — **LIVE, M15**.
```python
score = 1.0 if rv >= 1.5 else 0.6 if rv >= 1.2 else 0.2 if rv >= 0.8 else -0.3 if rv >= 0.5 else -0.6   # :273-279
```
Missing volume → score 0.0 with "Tick volume unavailable" evidence (`:263-271`).

**VOLATILITY category** (`:294-311`) — weight 0.05:
`NORMAL` → 1.0, `LOW` → 0.2, `HIGH` → -0.2, `EXTREME` → -1.0 (`:295-300`).

**`trendStrength`** (`:313-320`) — "Trend strength NN/100":
```python
blended = clamp(adx/40.0,0,1)*0.45 + abs(directionalScore)*0.30 + (alignment/100.0)*0.25
trendStrength = clamp(round(blended*100), 0, 100)
```

**`quality`** (`:326-356`) — "Long quality" / "Short quality", computed **independently**:
```python
if directionAgreement <= 0.0: return 0                            # :333
locationFactor   = 0.55 + 0.45*((locationValue + 1.0)/2.0)        # :335
volumeFactor     = 1.05 if vol >= 0.6 else 1.0 if vol >= 0.2 else 0.85    # :336-340
volatilityFactor = {NORMAL:1.0, LOW:0.85, HIGH:0.8, EXTREME:0.6}   # :341-346
newsFactor       = {LOW:1.0, MEDIUM:0.9, HIGH:0.7, EXTREME:0.5}    # :347-352
quality = clamp(round(directionAgreement * locationFactor * volumeFactor
                      * volatilityFactor * newsFactor * 100), 0, 100)     # :354-355
```
where `directionAgreement` = `max(directionalScore, 0)` for long (`:91`) and
`max(-directionalScore, 0)` for short (`:98`).
⚠️ `shortQuality != 100 - longQuality`; both can be low at once (`:322-325`).
⚠️ An **unknown** location (`LocationRead.Unknown`, both 0.0) is treated exactly like a measured
neutral location. **Q23.**

### 9.3 ⛔ `MarketAnalysisEngine` — orchestration & the usability gap
`ANA/MarketAnalysisEngine.kt:43-134`. Order: per-TF analyses → MTF combine → volatility → news →
key levels → location → regime → confluence → UT agreement → decision.

| Step | Slice used | Citation |
|---|---|---|
| per-TF analysis | both | `:51-55` |
| `price` | `snapshot.lastPrice` = **last close incl. forming bar** (`CORE/Candle.kt:43`) | `:58` |
| volatility | H1 **CONFIRMED** atrPercent | `:65` |
| key levels | M30/H1/H4 swings (CONFIRMED), H1 CONFIRMED candles & ATR | `:141-151` |
| location | **M15 LIVE** (falls back to H1 LIVE) | `:72-76` |
| regime | H1 **CONFIRMED** | `:78-83` |
| confluence | H1 CONFIRMED + M15 **LIVE** volume | `:85-93` |
| `utAgrees` | H1 **CONFIRMED** UT bias == confluence bias, and bias != NEUTRAL | `:95-98` |

⛔ **Usability gap:** the orchestrator only checks `series.closed.isEmpty()` (`:53`), never
`isUsable` (≥60 closed bars). `MultiTimeframeEngine` filters on `usable` (`ANA/mtf/MultiTimeframeEngine.kt:58`)
and `AlertEngine` skips unusable timeframes (`ANA/alerts/AlertEngine.kt:83`), but a direction anchor
with 20 bars still produces a full Analysis Score and Recommendation. `MarketSnapshot.isComplete`
(`CORE/MarketSnapshot.kt:34-35`) exists for exactly this and is never consulted. **Q8.**

### 9.4 `LocationAnalyzer`
| Field | Value |
|---|---|
| **UI name** | "Location" section; verdict `Good` / `Neutral` / `Poor` (`ANA/MarketAnalysisEngine.kt:247-251`) |
| **Source** | `ANA/confluence/LocationAnalyzer.kt:55-215` |
| **Inputs** | ⛔ **M15 LIVE snapshot**, `KeyLevels`, `mtf.higherTimeframeBias` |
| **Parameters** | `EXTENSION_TOLERANCE = 1.5` (`:47`), `ZONE_PROXIMITY_ATR = 0.75` (`:50`), `COUNTER_TREND_DAMP = 0.4` (`:53`) |
| **Repaint** | ⛔ **Yes** — LIVE-sourced and zone-dependent |

```python
if snapshot is None or atr is None or atr <= 0: return Unknown          # :60-61
forLong = forShort = 0.0
longDamp  = 0.4 if higherBias == BEARISH else 1.0                       # :68
shortDamp = 0.4 if higherBias == BULLISH else 1.0                       # :69

# --- extension from EMA28 (in ATR-14 units) --- :72-106
extension = (close - ema28)/atr
if extension >  1.5:  forLong  -= min((extension - 1.5)/2.0, 1.0)       # :76-77
if extension < -1.5:  forShort -= min((-extension - 1.5)/2.0, 1.0)      # :86-87
if abs(extension) <= 0.75: forLong += 0.25; forShort += 0.25            # :95-97

# --- Bollinger %B --- :109-151   (first matching branch only)
if   pctB > 0.95: forLong  -= 0.5;  forShort += 0.15*shortDamp          # :112-114
elif pctB > 0.85: forLong  -= 0.25                                      # :122-123
elif pctB < 0.05: forShort -= 0.5;  forLong  += 0.15*longDamp           # :131-133
elif pctB < 0.15: forShort -= 0.25                                      # :141-142

# --- structural zone proximity --- :154-178
rDist = keyLevels.nearestResistance.distanceFrom(close)/atr
sDist = keyLevels.nearestSupport.distanceFrom(close)/atr
if rDist < 0.75:                                                        # :157-160
    c = 1.0 - rDist/0.75;  forLong  -= 0.6*c;  forShort += 0.3*c*shortDamp
if sDist < 0.75:                                                        # :168-171
    c = 1.0 - sDist/0.75;  forShort -= 0.6*c;  forLong  += 0.3*c*longDamp

# --- pullback into dynamic support/resistance --- :181-200
atDynamic = any(abs(close - v)/atr <= 0.5 for v in [ema14, ema28, vwap] if v is not None)
if atDynamic and higherBias == BULLISH and (extension or 0.0) <  1.0: forLong  += 0.5   # :183-184
if atDynamic and higherBias == BEARISH and (extension or 0.0) > -1.0: forShort += 0.5   # :192-193

forLong  = clamp(forLong,  -1, 1)                                       # :202
forShort = clamp(forShort, -1, 1)                                       # :203
```

Summary sentence cutoffs (`:217-228`): both `<= -0.4` → "Poor location for either direction";
`forLong <= -0.4` → "Poor location to buy"; `forShort <= -0.4` → "Poor location to sell";
`forLong >= 0.4` → "Constructive location for a long"; `forShort >= 0.4` → short; else "Neutral".

⚠️ `forShort` is deliberately **not** `-forLong` (`:38-42`).
⚠️ Bare literals `0.75` (`:95`) and `0.5` (`:182`) shadow the named constants. **Q24.**

### 9.5 `DecisionEngine` → `Recommendation`
| Field | Value |
|---|---|
| **UI name** | "RECOMMENDATION" — display strings at `CORE/Enums.kt:40-47` |
| **Labels** | `POTENTIAL LONG SETUP`, `POTENTIAL SHORT SETUP`, `WAIT FOR PULLBACK`, `WAIT FOR CONFIRMATION`, `NO CLEAR SETUP`, `AVOID` |
| **Source** | `ANA/decision/DecisionEngine.kt:37-125` |
| **Parameters** | `MINIMUM_QUALITY = 45` (`:29`), `POOR_LOCATION = -0.35` (`:32`), `MINIMUM_TREND_STRENGTH = 40` (`:35`) |

**Strict ordering — risk vetoes evaluated before quality (`:20-25`):**
```python
best = max(longQuality, shortQuality)                                    # :46
locationForBias = forLong if BULLISH else forShort if BEARISH else min(both)   # :47-51

1. if newsRisk.isVeto:                       return AVOID                # :55-61
2. if volatility.band == EXTREME:            return AVOID                # :62-68
3. if bias == NEUTRAL or trendStrength < 40 or best < 45:
                                             return NO_CLEAR_SETUP       # :71-78
4. if mtf.conflict:                          return WAIT_FOR_CONFIRMATION # :81-87
5. if locationForBias <= -0.35:              return WAIT_FOR_PULLBACK    # :90-97
6. if not utConfirmedAgrees:                 return WAIT_FOR_CONFIRMATION # :100-107
7. return POTENTIAL_LONG_SETUP if BULLISH else POTENTIAL_SHORT_SETUP     # :110-124
```

**`entryRisk`** (`:131-167`) → `Grade` (`CORE/Enums.kt:24-28`: LOW / MEDIUM / HIGH / EXTREME):
```python
points  = {EXTREME:4, HIGH:2, MEDIUM:1, LOW:0}[newsRisk.level]           # :141-146
if not newsRisk.available:            points += 2                        # :149
points += {EXTREME:4, HIGH:1, LOW:1, NORMAL:0}[volatility.band]          # :151-156
if locationForBias <= -0.6:           points += 2
elif locationForBias <= -0.35:        points += 1                        # :158
if mtf.conflict:                      points += 1                        # :159
grade = LOW if points<=1 else MEDIUM if points<=3 else HIGH if points<=5 else EXTREME   # :161-166
```
⚠️ Note `VolatilityBand.LOW` also adds 1 point — quiet is treated as a hazard, same as HIGH.
⚠️ "No calendar" is +2, so a backtest without news data starts every trade at MEDIUM risk. **Q9.**

---

## 10. News risk (`ANA/news/NewsRiskEngine.kt`)

| Field | Value |
|---|---|
| **UI name** | "News risk" tile; "NEXT IMPORTANT EVENT" block |
| **Source** | `ANA/news/NewsRiskEngine.kt:58-89` |
| **Relevance filter** | `:48-53` — currency must be `USD` (`:46`); then `impact == HIGH` passes, else the title must contain one of 26 keywords (`:38-44`) |
| **Parameters** | cutoffs 30 / 60 / 240 min for HIGH impact, 30 min for MEDIUM (`:75-78`); `WHIPSAW_WINDOW_MS = 15 min` (`:108`) |
| **Repaint** | **No** (clock-driven, deterministic given the calendar) |

```python
relevant   = sorted(filter(isGoldRelevant, events), key=timeUtc)         # :55-56
upcoming   = [e for e in relevant if e.timeUtc >= nowUtc]                # :62
next_      = min(upcoming, key=timeUtc) if upcoming else None            # :63
justReleased = max([e for e in relevant
                    if e.timeUtc < nowUtc and nowUtc - e.timeUtc <= 15*60_000],
                   key=timeUtc, default=None)                            # :66-68
m = next_.minutesUntil(nowUtc) if next_ else None

level = HIGH    if justReleased and justReleased.impact == HIGH else \
        LOW     if next_ is None                                  else \
        EXTREME if next_.impact == HIGH   and m <= 30             else \
        HIGH    if next_.impact == HIGH   and m <= 60             else \
        MEDIUM  if next_.impact == HIGH   and m <= 240            else \
        MEDIUM  if next_.impact == MEDIUM and m <= 30             else LOW      # :72-80
```
`isVeto = (level == EXTREME)` (`:15`).
`NewsRisk.Unavailable` (`:18-25`) carries `level = LOW` but `available = false` — and
**`available == false` costs +2 entry-risk points** (`ANA/decision/DecisionEngine.kt:149`).

⛔ **Backtest blocker:** the app sources this from ForexFactory's *current* weekly JSON. There is no
historical calendar. **Q9.**

---

## 11. Alerts — the tradable-event catalogue (`ANA/alerts/`)

Nine kinds; each is a candidate backtest signal. Full conditions in `SIGNAL_MAP.md`.

| Kind | UI label | Default | Slice | Cooldown | Source |
|---|---|---|---|---|---|
| `ZONE_UT_CONFLUENCE` | "Support/resistance + UT level" | **on** | ⛔ LIVE | `tf.millis*3` | `AlertEngine.kt:167-211` |
| `ZONE_EMA_CONFLUENCE` | "Support/resistance + EMA/VWAP" | **on** | ⛔ LIVE | `tf.millis*3` | `:222-267` |
| `UT_CROSS` | "UT Bot cross (closed bar)" | **on** | ✅ CONFIRMED | `tf.millis*3` | `:126-154` |
| `ZONE_TOUCH` | "Support/resistance touch" | off | ⛔ LIVE | 30 min | `:311-342` |
| `SETUP` | "Setup reaches actionable quality" | **on** | mixed | 30 min | `:345-366` |
| `STRUCTURE_BREAK` | "Break / change of character" | **on** | ✅ CONFIRMED | `tf.millis*6` | `:275-306` |
| `MTF_FLIP` | "Higher-timeframe bias flip" | **on** | ✅ CONFIRMED | 60 min | `:369-391` |
| `NEWS_IMMINENT` | "High-impact news imminent" | **on** | clock | 6 h | `:399-422` |
| `EXTREME_VOLATILITY` | "Volatility turns extreme" | off | ✅ CONFIRMED | 60 min | `:425-447` |

Enum + labels: `ANA/alerts/Alert.kt:22-72`. Defaults = all except `EXTREME_VOLATILITY` and
`ZONE_TOUCH` (`:70`).
Proximity constants: `ZONE_NEAR_ATR = 0.5` (`:44`), `UT_NEAR_ATR = 0.6` (`:47`),
`DYNAMIC_NEAR_ATR = 0.5` (`:54`), `ZONE_TOUCH_ATR = 0.25` (`:57`).
Alert floor: `minimumTimeframe = M15` (`:108`); selectable = M15/M30/H1/H4 (`:123-124`) — **M5 is
never alertable**. Timeframe set = `Timeframe.ordered.filter { minutes >= floor }` (`:116-117`).
Gating: `isAlertable` requires `DataState.Fresh`, or `MarketClosed` with the weekend toggle (`:117-121`).
Precedence: `ZONE_UT_CONFLUENCE` **suppresses** `ZONE_EMA_CONFLUENCE` for the same timeframe (`:90-96`).

`AlertDeduper.admit` (`ANA/alerts/AlertDeduper.kt:25-35`): expire, then first-wins per `key`;
`prime = true` records without emitting.

⛔ **Repaint note:** `ZONE_UT_CONFLUENCE` / `ZONE_EMA_CONFLUENCE` / `ZONE_TOUCH` all read the LIVE
(forming-bar) snapshot and their dedupe keys carry **no bar timestamp** (`:206`, `:264`, `:339`) —
unlike `UT_CROSS`, which anchors on `lastClosedBarTime` (`:150`). An intrabar wick can therefore
produce a HIGH-severity alert describing a state that never existed on any closed bar.
**For the backtest these must be evaluated on the closed bar. Q25.**

---

## 12. Market hours & sessions (`CORE/MarketHours.kt`)

| Field | Value |
|---|---|
| **Source** | `CORE/MarketHours.kt:25-33` |
| **Parameters** | `FRIDAY_CLOSE_HOUR_UTC = 22` (`:20`), `SUNDAY_OPEN_HOUR_UTC = 21` (`:23`) |
| **Formula** | Saturday → closed; Friday `hour >= 22` → closed; Sunday `hour < 21` → closed; else open |

⛔ **There is no asia / london / new-york session classifier anywhere in the codebase.** Verified by
grep across `analysis/` and `core/`. The only "session" concepts are (a) the VWAP UTC-midnight
anchor and (b) `SupportResistanceEngine`'s UTC-midnight day bucket. The `session` /
`hour_of_day_myt` trade features in the master prompt are **backtest-only derived features**. **Q26.**

⚠️ **Three mutually inconsistent day boundaries coexist:** UTC midnight (S/R zones,
`ANA/structure/SupportResistanceEngine.kt:114`), 21:00/22:00 UTC (`MarketHours`), and MYT = UTC+8
(seasonality, `ANA/seasonality/SeasonalityModels.kt:12`).

---

## 13. Seasonality (`ANA/seasonality/`) — separate feature, not in the signal chain

Surfaced on its own "Clock" tab; does **not** feed `MarketAssessment`, the decision chain or alerts.
Documented for completeness.

| Field | Value |
|---|---|
| **Source** | `ANA/seasonality/SeasonalityEngine.kt:45-…`; models `ANA/seasonality/SeasonalityModels.kt` |
| **Parameters** | `MAX_SAMPLES = 100` (`:29`), `STRICT_MIN_SAMPLES = 30` (`:32`), `MIN_USABLE_SAMPLES = 20` (`:35`), `HIGHLIGHT_PCT = 65` (`:38`) |
| **Timezone** | **MYT = UTC+8** (`SeasonalityModels.kt:12`) — the only place the app buckets by local time |
| **Anchor rule** | the bar containing `nowUtc` is the anchor and is **excluded** (`:56`, `:65`) — no look-ahead |
| **Match modes** | `STRICT_WEEKDAY` if ≥30 same-weekday-same-time hits, else degrades to `TIME_ONLY` (`:68-74`) |
| **"NEXT BAR" statistic** | the bar that *followed* each historical match, looked up by expected open time (`:79`) |
| **Repaint** | **No** — but the mode can silently degrade |

---

## 14. Complete indicator/label inventory (quick index)

**Numeric series:** `atr`, `atrPercent`, `ema7`, `ema14`, `ema28`, `emaSeparationAtr`, `rsi`,
`bbUpper`, `bbMiddle`, `bbLower`, `bbPercentB`, `bbWidth`, `bbWidthPercentile`†, `adx`, `plusDi`,
`minusDi`, `vwap`, `relativeVolume`, `utBot.trailingStop`, `utBot.position`
— all on `IndicatorSnapshot` (`IND/IndicatorSet.kt:142-166`). († = computed, never consumed.)

**Enum labels:**
| Enum | Constants | Citation |
|---|---|---|
| `Bias` | BULLISH, BEARISH, NEUTRAL | `CORE/Enums.kt:4-5` |
| `Grade` | LOW, MEDIUM, HIGH, EXTREME | `CORE/Enums.kt:24-25` |
| `StructureState` | BULLISH, BEARISH, RANGE | `CORE/Enums.kt:31` |
| `VolatilityBand` | LOW, NORMAL, HIGH, EXTREME | `CORE/Enums.kt:37` |
| `Recommendation` | 6 values, see §9.5 | `CORE/Enums.kt:40-47` |
| `RegimeTag` | TREND_UP, TREND_DOWN, RANGE, HIGH_VOLATILITY, LOW_VOLATILITY, NEWS_RISK | `CORE/Enums.kt:50-52` |
| `EmaAlignment` | BULLISH, BEARISH, MIXED, UNDEFINED | `IND/Ema.kt:55` |
| `EmaSpreadTrend`† | EXPANDING, COMPRESSING, STABLE, UNDEFINED | `IND/Ema.kt:58` |
| `EvidenceCategory` | 7 values, see §9.1 | `ANA/confluence/Evidence.kt:11-19` |
| `SwingType` | HIGH, LOW | `ANA/structure/SwingDetector.kt:7` |
| `AlertKind` | 9 values, see §11 | `ANA/alerts/Alert.kt:22-66` |
| `AlertSeverity` | HIGH, NORMAL | `ANA/alerts/Alert.kt:13` |
| `MatchMode` | STRICT_WEEKDAY, TIME_ONLY | `ANA/seasonality/SeasonalityModels.kt` |
| `SeasonalityStatus` | OK, INSUFFICIENT, UNAVAILABLE | `ANA/seasonality/SeasonalityModels.kt` |

**Scores (all 0–100 ints):** `longQuality`, `shortQuality`, `trendStrength`, `analysisScore`,
`mtf.alignment`. **There is no "Opportunity Score" in this codebase** — see `OPEN_QUESTIONS.md` Q27.

**Levels & zones:** `utConfirmed.level` / `utLive.level` (UT Dynamic Level),
`keyLevels.nearestSupport` / `nearestResistance` (`PriceZone` = low/high/strength/sources),
`lastSwingHigh` / `previousSwingHigh` / `lastSwingLow` / `previousSwingLow`.

---

## 15. Repaint / look-ahead register

| # | What | Verdict | Citation |
|---|---|---|---|
| R1 | Fractal pivot needs `lookback` bars to the right | **Confirmed-late, causally safe** — loop bound is `size - lookback` | `SwingDetector.kt:54` |
| R2 | `filterAlternating` replaces the last accepted pivot in place | ⛔ **Unstable** — must recompute on an expanding window | `SwingDetector.kt:93` |
| R3 | Accepted swing chain is order-dependent from the window's first pivot | ⛔ **Window-dependent** | `SwingDetector.kt:78-100` + `MarketDataManager.kt:250,283` |
| R4 | UT Bot / ATR / RSI / ADX / EMA seed at the slice start | ⛔ **Warm-up-sensitive**, not look-ahead | `UtBot.kt:100`, `MathSeries.kt:36` |
| R5 | S/R "Session high/low" recomputed from the running day | ⛔ **Drifts intrabar** | `SupportResistanceEngine.kt:126-131` |
| R6 | Zone dedupe key uses the drifting `zone.mid` | ⛔ Re-fires inside its own cooldown | `AlertEngine.kt:339` |
| R7 | `LocationAnalyzer` reads the M15 **LIVE** snapshot | ⛔ **Intrabar** | `MarketAnalysisEngine.kt:72-76` |
| R8 | VOLUME category reads M15 **LIVE** relative volume | ⛔ **Intrabar**, up to ~20× at bar open | `ConfluenceEngine.kt:262`, `RelativeVolume.kt:50` |
| R9 | Zone/EMA/touch alerts read LIVE with no bar-anchored key | ⛔ **Intrabar** | `AlertEngine.kt:176,229,313` |
| R10 | `assessment.price` = `snapshot.lastPrice` = forming bar's close | ⛔ **Intrabar** | `Candle.kt:43`, `MarketAnalysisEngine.kt:58` |
| R11 | BOS/CHoCH is latched, not an edge | Needs backtest-side edge detection | `MarketStructureEngine.kt:72-73` |
| R12 | Orchestrator skips the 60-bar usability gate | ⛔ Headline verdict changes as history backfills | `MarketAnalysisEngine.kt:53` |
| R13 | `VolatilityClassifier` thin sample → silent `NORMAL` | ⛔ Vetoes structurally cannot fire | `VolatilityClassifier.kt:33` |
| R14 | HTF (H1/H4) bias finalises only on HTF bar close | Expected; must be evaluated on HTF-closed bars only | `Candle.kt:37-38` |
| R15 | Seasonality anchor bar excluded from its own sample | ✅ Safe | `SeasonalityEngine.kt:56,65` |

**Rule for the port:** every ⛔ LIVE-sourced input above must be replaced by its CONFIRMED
equivalent, and every ⛔ window-dependent computation must run on an expanding slice `[0..t]`.
That is a *deliberate divergence from the app* and is recorded as such in `OPEN_QUESTIONS.md` Q28.
