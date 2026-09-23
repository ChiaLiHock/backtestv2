"""Parity tests for the indicator port.

**Parity status: UNVERIFIED against the live app.** No golden CSV has been
exported yet (see ../tools/export_golden.md). Until one exists, these are
*self-consistency* tests: synthetic series with values computed by hand from the
formulas in docs/INDICATORS.md, plus the exact expectations pinned by the Kotlin
unit tests under app/src/test/.

When a golden CSV lands, `test_golden_csv_parity` stops skipping and becomes the
real check.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.indicators import context, mathseries, regime, structure, trend
from backtest.indicators.base import IndicatorConfig
from backtest.indicators.registry import compute_indicators

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "golden"


# ---------------------------------------------------------------------------
# MathSeries --- values pinned by app/src/test/.../MathSeriesTest.kt
# ---------------------------------------------------------------------------


def test_sma_matches_kotlin_fixture():
    """MathSeriesTest.kt:9-18 --- sma([1,2,3,4,5], 3) == [na, na, 2, 3, 4]."""
    out = mathseries.sma(np.array([1.0, 2, 3, 4, 5]), 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0], rtol=1e-12)


def test_rma_matches_kotlin_fixture():
    """MathSeriesTest.kt:20-26 --- seed is the SMA of the first `period` values."""
    out = mathseries.rma(np.array([1.0, 2, 3, 4, 5]), 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx((2.0 * 2 + 4) / 3)
    assert out[4] == pytest.approx((((2.0 * 2 + 4) / 3) * 2 + 5) / 3)


def test_rma_of_constant_is_that_constant():
    """MathSeriesTest.kt:28-32."""
    out = mathseries.rma(np.full(50, 7.5), 14)
    np.testing.assert_allclose(out[13:], 7.5, rtol=1e-12)


def test_rma_is_wilder_not_ema():
    """alpha must be 1/period. An EMA (2/(p+1)) would move faster off the seed."""
    values = np.array([1.0] * 10 + [11.0])
    out = mathseries.rma(values, 10)
    # seed = 1.0 at index 9; next = (1*9 + 11)/10 = 2.0
    assert out[10] == pytest.approx(2.0)
    ema_would_be = (11.0 - 1.0) * (2.0 / 11.0) + 1.0
    assert not math.isclose(out[10], ema_would_be, rel_tol=1e-6)


def test_stdev_is_population_not_sample():
    values = np.array([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    out = mathseries.stdev_population(values, 8)
    assert out[7] == pytest.approx(2.0)  # population sigma of this classic set
    sample_sigma = float(np.std(values, ddof=1))
    assert not math.isclose(out[7], sample_sigma, rel_tol=1e-6)


def test_percentile_rank_is_inclusive():
    sample = np.array([1.0, 2.0, 3.0, 4.0])
    assert mathseries.percentile_rank(sample, 3.0) == pytest.approx(0.75)
    assert mathseries.percentile_rank(np.array([]), 1.0) == 0.5


def test_series_length_invariant():
    """MathSeries.kt:8-10 --- output length always equals input length."""
    values = np.arange(30, dtype="float64")
    for fn, period in (
        (mathseries.sma, 5),
        (mathseries.rma, 5),
        (mathseries.stdev_population, 5),
    ):
        assert fn(values, period).size == values.size
    assert mathseries.rma_nullable(values, 5).size == values.size


def test_to_tick_volume_truncates():
    """BybitMarketDataSource.kt:133 --- (volume * 100).toLong() truncates."""
    out = mathseries.to_tick_volume(np.array([1.239, 0.0, 12.9999]))
    np.testing.assert_array_equal(out, [123.0, 0.0, 1299.0])


def test_to_tick_volume_maps_nan_to_zero_like_kotlin():
    """Kotlin's Double.toLong() maps NaN to 0 rather than propagating it."""
    out = mathseries.to_tick_volume(np.array([np.nan, 1.0]))
    np.testing.assert_array_equal(out, [0.0, 100.0])


def test_percentile_rank_matches_kotlin_nan_semantics():
    """MathSeries.kt:91-94 --- NaN fails `<=` but still fills a denominator slot."""
    # NaN element: counted in the denominator, not in the numerator.
    assert mathseries.percentile_rank(
        np.array([1.0, 2.0, np.nan, 4.0]), 2.0
    ) == pytest.approx(0.5)
    # Non-empty all-NaN sample -> 0.0, NOT 0.5.
    assert mathseries.percentile_rank(np.array([np.nan, np.nan, np.nan]), 1.0) == 0.0
    # NaN value -> nothing is <= it -> 0.0.
    assert mathseries.percentile_rank(np.array([1.0, 2.0, 3.0]), np.nan) == 0.0
    # Only a genuinely empty sample gives the 0.5 default.
    assert mathseries.percentile_rank(np.array([]), 1.0) == 0.5


# --- Bit-level parity against literal transcriptions of the Kotlin -----------
# These guard the fixes that came out of the port audit: cumsum-differencing in
# sma, and reciprocal-multiplication / numpy-summed seeds in rma. Each one was
# measured to change a value in the last bits, and one flipped an sma(10)>sma(30)
# comparison on a tick-rounded series --- i.e. it could add or remove a trade.


def _kotlin_sma(values, period):
    """Literal transcription of MathSeries.kt:15-25."""
    n = len(values)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    total = 0.0
    for i in range(n):
        total += values[i]
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _kotlin_rma(values, period):
    """Literal transcription of MathSeries.kt:32-42."""
    n = len(values)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    seed = 0.0
    for i in range(period):
        seed += values[i]
    out[period - 1] = seed / period
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _gold_like(n: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    return (2400 + np.cumsum(rng.normal(0, 1.5, n))).tolist()


@pytest.mark.parametrize("period", [14, 20, 200])
def test_sma_is_bit_identical_to_kotlin(period: int):
    values = _gold_like(20_000)
    ours = mathseries.sma(np.array(values), period)
    theirs = _kotlin_sma(values, period)
    for i in range(period - 1, len(values)):
        assert ours[i] == theirs[i], f"sma bit mismatch at {i} (period={period})"


@pytest.mark.parametrize("period", [14, 20, 200])
def test_rma_is_bit_identical_to_kotlin(period: int):
    values = _gold_like(20_000, seed=7)
    ours = mathseries.rma(np.array(values), period)
    theirs = _kotlin_rma(values, period)
    for i in range(period - 1, len(values)):
        assert ours[i] == theirs[i], f"rma bit mismatch at {i} (period={period})"


def test_rma_nullable_is_bit_identical_after_leading_nans():
    """The DX series inside ADX arrives with leading NaN; the seed must still
    accumulate sequentially from the first non-NaN."""
    lead = 13
    tail = _gold_like(5_000, seed=3)
    values = np.array([np.nan] * lead + tail)
    ours = mathseries.rma_nullable(values, 14)
    theirs = _kotlin_rma(tail, 14)
    for i in range(13, len(tail)):
        assert ours[lead + i] == theirs[i], f"rma_nullable bit mismatch at {i}"


# ---------------------------------------------------------------------------
# True range / ATR
# ---------------------------------------------------------------------------


def test_true_range_first_bar_degrades_to_high_minus_low():
    """Atr.kt:10-13 --- matters because the UT Bot seeds off it."""
    tr = trend.true_range(
        np.array([10.0, 12.0]), np.array([8.0, 9.0]), np.array([9.0, 11.0])
    )
    assert tr[0] == pytest.approx(2.0)
    assert tr[1] == pytest.approx(max(12 - 9, abs(12 - 9), abs(9 - 9)))


def test_atr_warmup_index():
    """First ATR value lands at index period - 1."""
    n = 40
    high = np.linspace(100, 140, n)
    low = high - 2.0
    close = high - 1.0
    out = trend.atr(high, low, close, 14)
    assert np.isnan(out[12])
    assert not np.isnan(out[13])


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def test_ema_is_sma_seeded():
    """Ema.kt:6-8 --- seeded with the SMA of the first `period` values."""
    values = np.array([1.0, 2, 3, 4, 5, 6, 7, 8])
    out = trend.ema(values, 4)
    assert np.isnan(out[2])
    assert out[3] == pytest.approx(2.5)  # mean(1,2,3,4)
    k = 2.0 / 5
    assert out[4] == pytest.approx((5 - 2.5) * k + 2.5)


def test_ema_alignment_codes():
    align = trend.ema_alignment(
        np.array([3.0, 1.0, 2.0, np.nan]),
        np.array([2.0, 2.0, 1.0, 1.0]),
        np.array([1.0, 3.0, 3.0, 1.0]),
    )
    assert align[0] == trend.ALIGN_BULLISH
    assert align[1] == trend.ALIGN_BEARISH
    assert align[2] == trend.ALIGN_MIXED
    assert align[3] == trend.ALIGN_UNDEFINED


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


def test_rsi_warmup_is_one_later_than_atr():
    """Rsi.kt:8 --- first value lands at index `period`, not period - 1."""
    values = np.cumsum(np.full(40, 1.0)) + 100
    out = context.rsi(values, 14)
    assert np.isnan(out[13])
    assert not np.isnan(out[14])


def test_rsi_monotonic_rise_is_100():
    out = context.rsi(np.arange(1.0, 40.0), 14)
    assert out[14] == pytest.approx(100.0)


def test_rsi_flat_window_is_50_not_100():
    """Rsi.kt:9-11 --- "no movement" is neutral, not maximum strength."""
    out = context.rsi(np.full(40, 100.0), 14)
    assert out[14] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------


def test_bollinger_bands_and_percent_b():
    values = np.array([1.0, 2, 3, 4, 5], dtype="float64")
    bb = context.bollinger(values, 5, 2.0)
    mean = 3.0
    sigma = math.sqrt(((values - mean) ** 2).sum() / 5)
    assert bb.middle[4] == pytest.approx(mean)
    assert bb.upper[4] == pytest.approx(mean + 2 * sigma)
    assert bb.lower[4] == pytest.approx(mean - 2 * sigma)
    assert bb.width[4] == pytest.approx((bb.upper[4] - bb.lower[4]) / mean)
    assert bb.percent_b[4] == pytest.approx(
        (values[4] - bb.lower[4]) / (bb.upper[4] - bb.lower[4])
    )


def test_bb_width_percentile_needs_min_sample():
    """Bollinger.kt:22 --- fewer than 20 samples returns null."""
    width = np.concatenate([np.full(10, np.nan), np.linspace(0.01, 0.02, 15)])
    out = context.bb_width_percentile(width, lookback=100, min_sample=20)
    assert np.isnan(out).all()


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def test_adx_warmup_index():
    """+DI/-DI at period-1; ADX at 2*period-2 (rma_nullable seeds later)."""
    n = 80
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + 1.0
    low = close - 1.0
    res = context.adx(high, low, close, 14)
    assert np.isnan(res.plus_di[12])
    assert not np.isnan(res.plus_di[13])
    assert np.isnan(res.adx[25])
    assert not np.isnan(res.adx[26])


def test_adx_of_pure_uptrend_saturates_and_di_is_bullish():
    n = 80
    close = np.arange(100.0, 100.0 + n)
    high = close + 0.5
    low = close - 0.5
    res = context.adx(high, low, close, 14)
    assert res.plus_di[-1] > res.minus_di[-1]
    assert res.adx[-1] > 90.0
    sign = context.directional_sign(res.plus_di, res.minus_di)
    assert sign[-1] == 1


def test_adx_short_input_returns_all_nan():
    """Adx.kt:35-38 --- n < period + 1 returns empty."""
    res = context.adx(np.arange(5.0), np.arange(5.0) - 1, np.arange(5.0), 14)
    assert np.isnan(res.adx).all()


# ---------------------------------------------------------------------------
# VWAP / relative volume
# ---------------------------------------------------------------------------


def test_vwap_resets_at_utc_midnight():
    day = 86_400_000
    open_time = np.array([day - 3_600_000, day, day + 3_600_000], dtype="int64")
    high = np.array([10.0, 20.0, 30.0])
    low = np.array([10.0, 20.0, 30.0])
    close = np.array([10.0, 20.0, 30.0])
    vol = np.array([1.0, 1.0, 1.0])
    out = context.vwap(open_time, high, low, close, vol)
    assert out[0] == pytest.approx(10.0)
    assert out[1] == pytest.approx(20.0)  # reset: new session
    assert out[2] == pytest.approx(25.0)  # (20 + 30) / 2


def test_vwap_all_zero_volume_returns_nan():
    """Vwap.kt:22 --- a flat VWAP built on zero volume would be fabricated."""
    open_time = np.arange(5, dtype="int64") * 60_000
    price = np.full(5, 10.0)
    out = context.vwap(open_time, price, price, price, np.zeros(5))
    assert np.isnan(out).all()


def test_relative_volume_excludes_current_bar():
    """RelativeVolume.kt:15 --- mean of the PREVIOUS `period` bars."""
    vol = np.concatenate([np.full(20, 100.0), np.array([250.0])])
    out = context.relative_volume(vol, 20)
    assert np.isnan(out[19])
    assert out[20] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# UT Bot --- the behaviours UtBot.kt:56-66 says must not be "cleaned up"
# ---------------------------------------------------------------------------


def _flat_then_move(n: int = 60, jump_at: int = 40, jump: float = 30.0):
    close = np.full(n, 2000.0)
    close[jump_at:] = 2000.0 + jump
    high = close + 1.0
    low = close - 1.0
    return high, low, close


def test_ut_first_available_bar_seeds_bullish():
    """UtBot.kt:60-63 --- nz(prevStop, 0) means the first branch wins, so the
    stop initialises to close - nLoss."""
    high, low, close = _flat_then_move()
    res = trend.ut_bot(high, low, close, key_value=2.0, atr_period=10)
    first = 9  # atr_period - 1
    assert np.isnan(res.trailing_stop[first - 1])
    assert res.trailing_stop[first] < close[first]


def test_ut_no_signal_before_stop_series_exists():
    """UtBot.kt:115-120 --- crossover/crossunder are na-safe."""
    high, low, close = _flat_then_move()
    res = trend.ut_bot(high, low, close, 2.0, 10)
    assert not res.buy[:10].any()
    assert not res.sell[:10].any()


def test_ut_position_carries_forward_between_crosses():
    """UtBot.kt:64 --- pos only changes on an actual cross."""
    high, low, close = _flat_then_move()
    res = trend.ut_bot(high, low, close, 2.0, 10)
    changes = np.flatnonzero(np.diff(res.position.astype("int16")) != 0)
    # Position is a step function: it holds flat between crosses.
    assert changes.size < close.size // 4


def test_ut_key_value_scales_the_stop_distance():
    """README: 2 vs 3 moves the trailing level by 50%."""
    n = 60
    rng = np.random.default_rng(3)
    close = 2000 + np.cumsum(rng.normal(0, 2.0, n))
    high, low = close + 2.0, close - 2.0
    a = trend.ut_bot(high, low, close, 2.0, 10)
    b = trend.ut_bot(high, low, close, 3.0, 10)
    # A wider key value can never sit closer to price on the very first bar.
    first = 9
    assert abs(close[first] - b.trailing_stop[first]) > abs(
        close[first] - a.trailing_stop[first]
    )


def test_ut_stop_ratchets_upward_while_bullish():
    """max(prevStop, src - nLoss) --- the level never retreats inside a leg."""
    n = 80
    close = 2000 + np.arange(n) * 2.0
    high, low = close + 1.0, close - 1.0
    res = trend.ut_bot(high, low, close, 2.0, 10)
    stops = res.trailing_stop[12:]
    assert np.all(np.diff(stops) >= -1e-9)


def test_ut_bias_codes():
    bias = trend.ut_bias(
        np.array([10.0, 8.0, 9.0, np.nan]), np.array([9.0, 9.0, 9.0, 9.0])
    )
    assert bias[0] == trend.BIAS_BULLISH
    assert bias[1] == trend.BIAS_BEARISH
    assert bias[2] == trend.BIAS_NEUTRAL  # exactly equal
    assert bias[3] == trend.BIAS_NEUTRAL


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_raw_pivot_requires_lookback_bars_on_both_sides():
    high = np.array([1.0, 2.0, 5.0, 2.0, 1.0])
    low = np.array([1.0, 2.0, 5.0, 2.0, 1.0])
    is_high, _ = structure.raw_pivots(high, low, 2)
    assert is_high[2]
    # Bars inside the lookback margin can never be pivots.
    assert not is_high[0] and not is_high[1]
    assert not is_high[3] and not is_high[4]


def test_raw_pivot_rejects_ties():
    """SwingDetector.kt:60-61 uses <= / >= so an equal neighbour disqualifies."""
    high = np.array([1.0, 5.0, 5.0, 5.0, 1.0])
    low = np.full(5, 1.0)
    is_high, _ = structure.raw_pivots(high, low, 2)
    assert not is_high.any()


def test_swing_lookback_per_timeframe():
    """SwingDetector.kt:24-27."""
    assert structure.swing_lookback_for("1h") == 3
    assert structure.swing_lookback_for("4h") == 3
    assert structure.swing_lookback_for("30m") == 2
    assert structure.swing_lookback_for("5m") == 2


def test_zigzag_keeps_more_extreme_of_two_same_type_pivots():
    """SwingDetector.kt:87-95 --- the in-place replacement."""
    fold = structure._ZigzagFold(0.5)
    fold.offer(structure.SwingPoint(0, 100.0, 0, structure.SWING_HIGH), 1.0)
    fold.offer(structure.SwingPoint(2, 105.0, 0, structure.SWING_HIGH), 1.0)
    state = fold.state()
    assert state.accepted_count == 1
    assert state.last_high is not None and state.last_high.price == 105.0


def test_zigzag_rejects_pivot_inside_atr_separation():
    fold = structure._ZigzagFold(0.5)
    fold.offer(structure.SwingPoint(0, 100.0, 0, structure.SWING_HIGH), 10.0)
    # threshold = 10 * 0.5 = 5; a low only 1.0 away is not structural.
    fold.offer(structure.SwingPoint(2, 99.0, 0, structure.SWING_LOW), 10.0)
    assert fold.state().accepted_count == 1
    # 6.0 away clears it.
    fold.offer(structure.SwingPoint(4, 94.0, 0, structure.SWING_LOW), 10.0)
    assert fold.state().accepted_count == 2


def test_structure_state_from_hh_hl():
    close = np.array([100.0, 100.0, 100.0])
    states = [
        structure.SwingState(
            last_high=structure.SwingPoint(0, 110.0, 0, structure.SWING_HIGH),
            prev_high=structure.SwingPoint(0, 105.0, 0, structure.SWING_HIGH),
            last_low=structure.SwingPoint(0, 95.0, 0, structure.SWING_LOW),
            prev_low=structure.SwingPoint(0, 90.0, 0, structure.SWING_LOW),
            accepted_count=4,
        )
    ] * 3
    res = structure.market_structure(close, states)
    assert (res.state == structure.STRUCTURE_BULLISH).all()
    assert (res.bos == structure.BIAS_NEUTRAL).all()


def test_range_break_both_extremes_reports_bearish():
    """MarketStructureEngine.kt:89-90 --- the second assignment wins. Q11."""
    states = [
        structure.SwingState(
            last_high=structure.SwingPoint(0, 100.0, 0, structure.SWING_HIGH),
            prev_high=structure.SwingPoint(0, 100.0, 0, structure.SWING_HIGH),
            last_low=structure.SwingPoint(0, 200.0, 0, structure.SWING_LOW),
            prev_low=structure.SwingPoint(0, 200.0, 0, structure.SWING_LOW),
            accepted_count=4,
        )
    ]
    res = structure.market_structure(np.array([150.0]), states)
    assert res.state[0] == structure.STRUCTURE_RANGE
    assert res.bos[0] == structure.BIAS_BEARISH


def test_price_zone_distance_is_zero_inside():
    zone = structure.PriceZone(100.0, 110.0, 1.0, ("t",))
    assert zone.distance_from(105.0) == 0.0
    assert zone.distance_from(95.0) == pytest.approx(5.0)
    assert zone.distance_from(115.0) == pytest.approx(5.0)


def test_cluster_merges_within_tolerance_and_chains():
    """SupportResistanceEngine.kt:151-158 --- chained merge, Q12."""
    levels = [(100.0, 1.0, "a"), (100.4, 1.0, "b"), (100.8, 1.0, "c")]
    zones = structure._cluster(levels, tolerance=0.5)
    assert len(zones) == 1
    # Chained: total width 0.8 exceeds the 0.5 tolerance.
    assert zones[0].high - zones[0].low == pytest.approx(0.8)
    assert zones[0].strength == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def test_volatility_thin_sample_is_normal_but_unmeasured():
    """VolatilityClassifier.kt:33 --- the degraded branch. Q7."""
    atr_pct = np.linspace(0.1, 0.2, 30)
    vol = regime.classify_volatility(atr_pct, lookback=150, min_sample=40)
    assert (vol.band == regime.VOL_NORMAL).all()
    assert not vol.measured.any()
    assert np.isnan(vol.percentile).all()


def test_volatility_bands_at_boundaries():
    atr_pct = np.linspace(0.0, 1.0, 100)
    vol = regime.classify_volatility(atr_pct, lookback=150, min_sample=40)
    # The last bar is the maximum of its own sample -> percentile 1.0 -> EXTREME.
    assert vol.measured[-1]
    assert vol.band[-1] == regime.VOL_EXTREME
    assert vol.percentile[-1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Full registry
# ---------------------------------------------------------------------------


def _synthetic_ohlcv(n: int = 800, seed: int = 11, interval_ms: int = 1_800_000):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 3.0, n))
    spread = np.abs(rng.normal(0, 2.0, n)) + 1.0
    return pd.DataFrame(
        {
            "open_time": np.arange(n, dtype="int64") * interval_ms,
            "open": close - rng.normal(0, 1.0, n),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.abs(rng.normal(100, 30, n)),
            "turnover": np.abs(rng.normal(200000, 50000, n)),
        }
    )


def test_registry_returns_aligned_frame():
    df = _synthetic_ohlcv()
    out = compute_indicators(df, "30m")
    assert len(out) == len(df)
    assert (out["open_time"].to_numpy() == df["open_time"].to_numpy()).all()
    for col in ("atr_14", "ema_7", "rsi_14", "adx_14", "ut_level", "bb_percent_b"):
        assert col in out.columns


def test_registry_is_deterministic():
    df = _synthetic_ohlcv()
    a = compute_indicators(df, "30m")
    b = compute_indicators(df, "30m")
    pd.testing.assert_frame_equal(a, b)


def test_vwap_suppressed_on_4h():
    """IndicatorSet.kt:126-132."""
    df = _synthetic_ohlcv(interval_ms=14_400_000)
    out = compute_indicators(df, "4h")
    assert out["vwap"].isna().all()
    out30 = compute_indicators(_synthetic_ohlcv(), "30m")
    assert out30["vwap"].notna().any()


def test_directional_score_is_bounded():
    df = _synthetic_ohlcv()
    out = compute_indicators(df, "30m")
    score = out["directional_score"].to_numpy()
    assert np.nanmin(score) >= -1.0 - 1e-12
    assert np.nanmax(score) <= 1.0 + 1e-12


def test_usable_gate_matches_min_candles():
    """core/Candle.kt:49 --- MIN_CANDLES = 60 closed bars. OPEN_QUESTIONS Q8."""
    from backtest.indicators.registry import MIN_CANDLES

    df = _synthetic_ohlcv(120)
    out = compute_indicators(df, "30m")
    assert MIN_CANDLES == 60
    assert not out["usable"].to_numpy()[: MIN_CANDLES - 1].any()
    assert out["usable"].to_numpy()[MIN_CANDLES - 1 :].all()


def test_ut_key_value_default_is_two():
    """docs/OPEN_QUESTIONS.md Q2 --- the app's effective default, not UtBot.kt's 3.0."""
    assert IndicatorConfig().ut_key_value == pytest.approx(2.0)
    assert IndicatorConfig().ut_atr_period == 10


# ---------------------------------------------------------------------------
# Golden CSV --- skipped until one is exported from the live app
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (GOLDEN_DIR / "golden.csv").exists(),
    reason="no golden CSV exported yet --- see tools/export_golden.md",
)
def test_golden_csv_parity():
    """Every ported column must match the app within 1e-6 relative tolerance.

    Recursive indicators (ATR, RSI, ADX, EMA, UT level) get a looser 1e-4 because
    the app seeds them at the start of a rolling 500-bar window while we compute
    over full history; the two converge but do not become bit-identical. See
    docs/OPEN_QUESTIONS.md Q4.
    """
    golden = pd.read_csv(GOLDEN_DIR / "golden.csv")
    interval = str(golden.attrs.get("interval", "30m"))
    ours = compute_indicators(golden[["open_time", "open", "high", "low", "close", "volume"]], interval)

    strict = ("bb_upper", "bb_middle", "bb_lower", "bb_percent_b")
    loose = ("atr_14", "rsi_14", "adx_14", "ema_7", "ema_14", "ema_28", "ut_level")

    for col in strict:
        if col in golden.columns:
            np.testing.assert_allclose(
                ours[col].to_numpy(), golden[col].to_numpy(), rtol=1e-6, equal_nan=True
            )
    for col in loose:
        if col in golden.columns:
            np.testing.assert_allclose(
                ours[col].to_numpy(), golden[col].to_numpy(), rtol=1e-4, equal_nan=True
            )
