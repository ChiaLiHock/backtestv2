"""Shared series maths — exact port of `analysis/indicators/MathSeries.kt`.

Invariant carried over from the Kotlin (`MathSeries.kt:8-10`): **every function
returns an array the same length as its input, with NaN during warm-up.** That is
what lets every downstream index line up with a candle position.

Kotlin `Double?` maps to `float64` NaN here. `None` is never used inside a series.
"""

from __future__ import annotations

import numpy as np

NaN = np.nan


def _empty(n: int) -> np.ndarray:
    return np.full(n, NaN, dtype="float64")


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average. First value lands at index ``period - 1``.

    Port of `MathSeries.kt:15-25` — the **running sum**, add-then-subtract,
    replicated literally.

    A cumulative-sum difference is the same arithmetic on paper but not in
    IEEE-754: ``cumsum`` grows to ``n * price`` so the subtraction cancels away
    significant bits, and the error grows with the bar index. The Kotlin's
    accumulator stays at window scale. Measured over 20k bars of gold-like
    prices, cumsum-differencing was ~250x less accurate and flipped an
    ``sma(10) > sma(30)`` comparison on a tick-rounded series. Parity beats
    elegance here — a boolean that flips is a trade that appears or vanishes.
    """
    values = np.asarray(values, dtype="float64")
    n = values.size
    if period <= 0 or n < period:
        return _empty(n)

    out = _empty(n)
    total = 0.0
    for i in range(n):
        total += values[i]
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (Pine ``ta.rma``), seeded with the SMA of the first
    ``period`` values.

    Port of `MathSeries.kt:32-42`. alpha = 1/period — **not** an EMA's 2/(p+1).
    `MathSeries.kt:28-31` is explicit that substituting an EMA "would silently
    de-synchronise this app from TradingView".
    """
    values = np.asarray(values, dtype="float64")
    n = values.size
    if period <= 0 or n < period:
        return _empty(n)

    out = _empty(n)
    # Sequential left-to-right seed, matching `MathSeries.kt:35-37`. numpy's
    # pairwise summation associates differently and lands 1 ULP away on ~19% of
    # period-14 windows; the seed is the initial condition of a recursive filter,
    # so that difference is carried forever.
    seed = 0.0
    for i in range(period):
        seed += values[i]
    prev = seed / period
    out[period - 1] = prev
    scale = period - 1
    for i in range(period, n):
        # Divide, do not multiply by a precomputed 1/period: 1/14 is inexact, so
        # the reciprocal form rounds twice and differs on ~36% of steps.
        prev = (prev * scale + values[i]) / period
        out[i] = prev
    return out


def rma_nullable(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing over a series that already has leading NaN (the DX series
    inside ADX). Seeds from the first ``period`` non-NaN values, keeps alignment.

    Port of `MathSeries.kt:48-64`.

    Note on the Kotlin edge case (`MathSeries.kt:60-61`): a NaN appearing *after*
    the seed is skipped, leaving that slot NaN, and the next real value then reads
    ``out[i-1]`` — which in Kotlin would be a null-dereference crash. It is
    unreachable with real ADX input because DX has no interior nulls once seeded.
    Here the carried value is used instead of crashing, which is the behaviour the
    Kotlin *intended*; see docs/OPEN_QUESTIONS.md Q14.
    """
    values = np.asarray(values, dtype="float64")
    n = values.size
    out = _empty(n)

    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return out
    first = int(valid[0])
    if n - first < period:
        return out

    window = values[first : first + period]
    if np.isnan(window).any():
        # Kotlin bails out entirely if any of the seed values is null.
        return out

    seed = 0.0
    for i in range(first, first + period):
        seed += values[i]
    prev = seed / period
    seed_index = first + period - 1
    out[seed_index] = prev
    scale = period - 1
    for i in range(seed_index + 1, n):
        v = values[i]
        if np.isnan(v):
            continue
        prev = (prev * scale + v) / period
        out[i] = prev
    return out


def stdev_population(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling **population** standard deviation — matches Pine's ``stdev``.

    Port of `MathSeries.kt:70-85`. Divisor is ``period``, not ``period - 1``:
    `MathSeries.kt:67-68` notes the sample formula "would widen every Bollinger
    band slightly". In pandas terms this is ``.rolling(period).std(ddof=0)``.
    """
    values = np.asarray(values, dtype="float64")
    n = values.size
    if period <= 0 or n < period:
        return _empty(n)

    out = _empty(n)
    # Sliding window view keeps this O(n * period) like the Kotlin, but vectorised.
    windows = np.lib.stride_tricks.sliding_window_view(values, period)
    means = windows.mean(axis=1)
    variances = ((windows - means[:, None]) ** 2).sum(axis=1) / period
    out[period - 1 :] = np.sqrt(variances)
    return out


def percentile_rank(sample: np.ndarray, value: float) -> float:
    """Fraction of ``sample`` at or below ``value``, in [0, 1].

    Port of `MathSeries.kt:91-94`. Empty sample returns 0.5. Comparison is ``<=``,
    so ranking a value against a sample containing itself never returns 0.

    NaN handling matches the Kotlin exactly and deliberately: a NaN element fails
    ``x <= value`` so it does not count, but it **still occupies a slot in the
    denominator**. A non-empty all-NaN sample therefore returns 0.0, not 0.5, and
    a NaN ``value`` returns 0.0. Stripping NaN here instead would change the
    denominator and could move a volatility band across a cut point.

    Both Kotlin call sites strip nulls before calling (`Bollinger.kt:21`
    ``mapNotNull``, `VolatilityClassifier.kt:32` ``filterNotNull``), so in
    practice the sample arrives clean — this is about not silently disagreeing.
    """
    sample = np.asarray(sample, dtype="float64")
    if sample.size == 0:
        return 0.5
    with np.errstate(invalid="ignore"):
        below = int(np.count_nonzero(sample <= value))
    return float(below) / sample.size


def last_value(series: np.ndarray) -> float:
    """Last non-NaN value, or NaN if the series never warmed up.

    Port of `MathSeries.kt:97`.
    """
    series = np.asarray(series, dtype="float64")
    valid = np.flatnonzero(~np.isnan(series))
    return float(series[valid[-1]]) if valid.size else NaN


def value_ago(series: np.ndarray, back: int) -> float:
    """Value ``back`` bars before the end, skipping nothing. NaN if out of range.

    Port of `MathSeries.kt:100`.
    """
    series = np.asarray(series, dtype="float64")
    idx = series.size - 1 - back
    return float(series[idx]) if 0 <= idx < series.size else NaN


def to_tick_volume(volume: np.ndarray) -> np.ndarray:
    """Bybit fractional-ounce volume -> the app's integer ``tickVolume``.

    `BybitMarketDataSource.kt:133`: ``(volume * 100).toLong()``. Only *relative*
    volume is ever used so the scale cancels, but the integer truncation does not
    — replicate it or relative-volume ratios drift in the last digits.

    Kotlin's ``Double.toLong()`` maps NaN to 0 and saturates at Long.MIN/MAX;
    ``np.trunc`` would keep NaN and propagate it through the whole VWAP series.
    Matched here so a single malformed bar degrades the same way in both.
    """
    scaled = np.asarray(volume, dtype="float64") * 100.0
    scaled = np.nan_to_num(scaled, nan=0.0, posinf=9.223372036854776e18,
                           neginf=-9.223372036854776e18)
    return np.trunc(scaled)
