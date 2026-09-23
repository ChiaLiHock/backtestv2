# Exporting a golden CSV from the live app

Until this is done, **indicator parity is UNVERIFIED**. `tests/test_parity.py`
currently proves only self-consistency: that the Python matches the formulas as
documented in `docs/INDICATORS.md`, and that it reproduces the values pinned by
the Kotlin unit tests. It does **not** yet prove the Python matches what your
phone displays.

`test_golden_csv_parity` skips itself while `tools/golden/golden.csv` is absent
and starts enforcing the moment it exists.

---

## What I need from you

One CSV per timeframe, taken from the app, containing the **CONFIRMED** (closed-bar)
value of every indicator column, for at least 600 consecutive bars.

600 is not arbitrary: the ADX warm-up alone is 26 bars, the volatility percentile
wants 150, and the UT trailing stop is path-dependent, so a short window would
"pass" for the wrong reason.

Save as `backtest/tools/golden/golden.csv` (or `golden_30m.csv`, `golden_1h.csv`, …).

### Required columns

| Column | Source in the app |
|---|---|
| `open_time` | `Candle.timeUtc` — UTC epoch **milliseconds**, bar OPEN |
| `open`, `high`, `low`, `close`, `volume` | the raw candle |
| `atr_14` | `IndicatorSnapshot.atr` |
| `ema_7`, `ema_14`, `ema_28` | `IndicatorSnapshot.ema7/ema14/ema28` |
| `rsi_14` | `IndicatorSnapshot.rsi` |
| `adx_14`, `plus_di`, `minus_di` | `IndicatorSnapshot.adx/plusDi/minusDi` |
| `bb_upper`, `bb_middle`, `bb_lower`, `bb_percent_b` | `IndicatorSnapshot.bb*` |
| `vwap` | `IndicatorSnapshot.vwap` |
| `relative_volume` | `IndicatorSnapshot.relativeVolume` |
| `ut_level` | `IndicatorSnapshot.ut.level` — the UT Dynamic Level |
| `ut_position` | `utBot.position[i]` (`1`/`-1`/`0`) |
| `structure_state` | `MarketStructure.state` as `BULLISH`/`BEARISH`/`RANGE` |

Blank cell for a warm-up null. Full float precision — do **not** round to the
2 dp the UI shows, or the comparison becomes meaningless.

---

## The important part: settings must match

Before exporting, note these from the app and tell me the values:

1. **UT Bot inputs** (gear icon): `keyValue` and `atrPeriod`.
   The port defaults to **2.0 / 10** (`docs/OPEN_QUESTIONS.md` Q2). If your app
   shows anything else, the UT column will not match and nothing downstream will.
2. **Which feed produced the bars** — the dashboard names it. It must say
   `GOLD · XAUUSDT PERP`. If it fell back to PAXG or Yahoo, the prices are a
   different instrument and parity is meaningless.
3. **The timeframe** of the export.

---

## Easiest way to produce it

The `analysis` package is pure Kotlin with zero Android imports, so a JVM unit
test can dump the CSV without a device. Add a **temporary** test under
`app/src/test/` — do not commit it — along these lines:

```kotlin
// app/src/test/java/com/example/itsupport/ExportGolden.kt  (TEMPORARY)
@Test
fun exportGolden() {
    val candles: List<Candle> = /* load the 600+ bars you want */
    val series = CandleSeries(Timeframe.M30, candles, System.currentTimeMillis())
    val config = IndicatorConfig(utKeyValue = 2.0, utAtrPeriod = 10)
    val set = IndicatorSet.compute(Timeframe.M30, series.closed, config)

    val sb = StringBuilder()
    sb.appendLine(
        "open_time,open,high,low,close,volume,atr_14,ema_7,ema_14,ema_28," +
        "rsi_14,adx_14,plus_di,minus_di,bb_upper,bb_middle,bb_lower," +
        "bb_percent_b,vwap,relative_volume,ut_level,ut_position"
    )
    set.candles.indices.forEach { i ->
        fun d(v: Double?) = v?.toString() ?: ""
        val c = set.candles[i]
        sb.appendLine(
            listOf(
                c.timeUtc, c.open, c.high, c.low, c.close, c.tickVolume,
                d(set.atr[i]), d(set.ema.ema7[i]), d(set.ema.ema14[i]), d(set.ema.ema28[i]),
                d(set.rsi[i]), d(set.adx.adx[i]), d(set.adx.plusDi[i]), d(set.adx.minusDi[i]),
                d(set.bollinger.upper[i]), d(set.bollinger.middle[i]), d(set.bollinger.lower[i]),
                d(set.bollinger.percentB[i]), d(set.vwap[i]), d(set.relativeVolume[i]),
                d(set.utBot.trailingStop[i]), set.utBot.position[i]
            ).joinToString(",")
        )
    }
    java.io.File("golden_30m.csv").writeText(sb.toString())
}
```

Run it with:

```bash
./gradlew :app:testDebugUnitTest --tests "*ExportGolden*"
```

⚠️ **Note on the volume column.** The app stores `tickVolume = (volume * 100).toLong()`
(`BybitMarketDataSource.kt:133`). Export that integer, not the raw float — the
Python applies the same truncation and I want to compare like with like.

⚠️ **Delete the test afterwards.** The instruction for this project is that the
existing app source stays read-only; I have not modified it and this file would
be the only exception.

---

## If a JVM test is inconvenient

The Dashboard's "Copy for AI" card puts the whole page on the clipboard. That is
**not** enough for parity — it carries one snapshot at 0–2 dp, not a series. It
is useful as a *spot check*: paste it here and I will compare that single bar
against the port. It will catch a gross error (wrong UT inputs, wrong feed) but
not a subtle one.

---

## What happens once the CSV exists

`test_golden_csv_parity` compares every column:

* **1e-6 relative** for stateless indicators — Bollinger bands, %B.
* **1e-4 relative** for recursive ones — ATR, RSI, ADX, EMA, UT level.

The looser tolerance on the recursive family is not slack, it is the known
consequence of `docs/OPEN_QUESTIONS.md` Q4: the app seeds every recursion at the
start of a rolling 500-bar window while the backtest computes over full history.
The two converge but never become bit-identical. If the recursive columns fail at
1e-4 the cause is a real porting bug, not warm-up drift — the drift is far
smaller than that by 600 bars.
