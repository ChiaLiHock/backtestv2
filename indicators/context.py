"""RSI, Bollinger, ADX/DMI, VWAP and relative volume — the context family.

Exact ports of `analysis/indicators/Rsi.kt`, `Bollinger.kt`, `Adx.kt`, `Vwap.kt`
and `RelativeVolume.kt`.
"""

from __future__ import annotations

import numpy as np

from .mathseries import NaN, percentile_rank, rma, rma_nullable, sma, stdev_population
from .trend import true_range

# ---------------------------------------------------------------------------
# RSI  (Rsi.kt)
# ---------------------------------------------------------------------------


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """Port of `Rsi.kt:44-49`.

    A completely flat window returns **50**, not the 100 a naive ``avgLoss == 0``
    branch would give — "no movement" is neutral, not maximum strength
    (`Rsi.kt:9-11`).
    """
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(source: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI. **First value lands at index ``period``** — one later than
    ATR/EMA. Port of `Rsi.kt:13-42`.

    Note this does NOT call ``rma``; the Kotlin reimplements Wilder smoothing
    inline (`Rsi.kt:28-40`) and seeds from bars ``1..period``.
    """
    source = np.asarray(source, dtype="float64")
    n = source.size
    if period <= 0 or n < period + 1:
        return np.full(n, NaN, dtype="float64")

    gains = np.zeros(n, dtype="float64")
    losses = np.zeros(n, dtype="float64")
    change = source[1:] - source[:-1]
    gains[1:] = np.where(change > 0, change, 0.0)
    losses[1:] = np.where(change < 0, -change, 0.0)

    out = np.full(n, NaN, dtype="float64")
    avg_gain = float(gains[1 : period + 1].sum()) / period
    avg_loss = float(losses[1 : period + 1].sum()) / period
    out[period] = _rsi_from(avg_gain, avg_loss)

    scale = period - 1
    inv = 1.0 / period
    for i in range(period + 1, n):
        avg_gain = (avg_gain * scale + gains[i]) * inv
        avg_loss = (avg_loss * scale + losses[i]) * inv
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


# ---------------------------------------------------------------------------
# MACD  --- ⚠️ NOT PORTED FROM THE APP
# ---------------------------------------------------------------------------
# The Android app has no MACD anywhere; this is a backtest-only addition, added
# on request for rule-building. It is therefore NOT covered by the indicator
# parity that was confirmed against the live app --- there is nothing on screen
# to compare it with. Standard construction, SMA-seeded EMAs to stay consistent
# with the rest of this module (and with Pine's `ta.ema`).


class MacdResult:
    __slots__ = ("macd", "signal", "hist")

    def __init__(self, macd, signal, hist) -> None:
        self.macd = macd
        self.signal = signal
        self.hist = hist


def _ema_skip_nan(values: np.ndarray, period: int) -> np.ndarray:
    """SMA-seeded EMA over a series that has leading NaN, preserving alignment."""
    values = np.asarray(values, dtype="float64")
    n = values.size
    out = np.full(n, NaN, dtype="float64")
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < period:
        return out
    first = int(valid[0])
    seed = 0.0
    for i in range(first, first + period):
        seed += values[i]
    prev = seed / period
    out[first + period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(first + period, n):
        v = values[i]
        if np.isnan(v):
            continue
        prev = (v - prev) * k + prev
        out[i] = prev
    return out


def macd(
    source: np.ndarray, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> MacdResult:
    """MACD line, signal line and histogram.

    ``macd = EMA(fast) - EMA(slow)``; ``signal = EMA(macd, signal_period)``;
    ``hist = macd - signal``. First MACD value at index ``slow - 1``; first
    signal value ``signal_period - 1`` bars after that.
    """
    from .trend import ema as _ema

    source = np.asarray(source, dtype="float64")
    fast_line = _ema(source, fast)
    slow_line = _ema(source, slow)
    line = fast_line - slow_line
    sig = _ema_skip_nan(line, signal_period)
    return MacdResult(line, sig, line - sig)


# ---------------------------------------------------------------------------
# Bollinger  (Bollinger.kt)
# ---------------------------------------------------------------------------


class BollingerBands:
    __slots__ = ("upper", "middle", "lower", "width", "percent_b")

    def __init__(self, upper, middle, lower, width, percent_b) -> None:
        self.upper = upper
        self.middle = middle
        self.lower = lower
        self.width = width
        self.percent_b = percent_b


def bollinger(
    source: np.ndarray, period: int = 20, multiplier: float = 2.0
) -> BollingerBands:
    """Port of `Bollinger.kt:32-58`.

    ``width = (upper - lower) / middle``  (NaN when middle == 0)
    ``percentB = (src - lower) / (upper - lower)``  (NaN when the band is flat)
    """
    source = np.asarray(source, dtype="float64")
    middle = sma(source, period)
    sd = stdev_population(source, period)

    upper = middle + multiplier * sd
    lower = middle - multiplier * sd

    width = np.full(source.size, NaN, dtype="float64")
    ok = (~np.isnan(middle)) & (middle != 0.0)
    width[ok] = (upper[ok] - lower[ok]) / middle[ok]

    percent_b = np.full(source.size, NaN, dtype="float64")
    span = upper - lower
    ok_b = (~np.isnan(span)) & (span > 0.0)
    percent_b[ok_b] = (source[ok_b] - lower[ok_b]) / span[ok_b]

    return BollingerBands(upper, middle, lower, width, percent_b)


def bb_width_percentile(
    width: np.ndarray, lookback: int = 100, min_sample: int = 20
) -> np.ndarray:
    """Where each bar's band width sits against its own recent history.

    Port of `Bollinger.kt:18-24`. Absolute width means nothing on its own, so
    "squeeze" is a low percentile of recent width. Returns NaN until at least
    ``min_sample`` non-NaN widths exist in the trailing window.

    This is computed by the app on every snapshot and read by **nothing**
    (docs/OPEN_QUESTIONS.md Q16). Ported because it is available and useful.
    """
    return _rolling_percentile_rank(width, lookback, min_sample)


def _rolling_percentile_rank(
    values: np.ndarray, lookback: int, min_sample: int, chunk: int = 8192
) -> np.ndarray:
    """Trailing-window percentile rank of each value against its own recent history.

    For every index ``i``: rank ``values[i]`` against the non-NaN entries of
    ``values[max(0, i-lookback+1) : i+1]``, returning NaN when that sample holds
    fewer than ``min_sample`` values.

    Vectorised over a padded sliding window and processed in chunks so peak
    memory stays at ``chunk * lookback`` floats rather than ``n * lookback`` —
    on a 240k-bar 1m series the naive form would allocate ~190 MB and the naive
    Python loop took minutes.
    """
    values = np.asarray(values, dtype="float64")
    n = values.size
    out = np.full(n, NaN, dtype="float64")
    if n == 0 or lookback <= 0:
        return out

    # Left-pad with NaN so window i is exactly values[i-lookback+1 : i+1].
    padded = np.concatenate([np.full(lookback - 1, NaN, dtype="float64"), values])
    windows = np.lib.stride_tricks.sliding_window_view(padded, lookback)

    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        block = windows[start:stop]
        current = values[start:stop, None]
        valid = ~np.isnan(block)
        counts = valid.sum(axis=1)
        with np.errstate(invalid="ignore"):
            below = ((block <= current) & valid).sum(axis=1)
        ok = (counts >= min_sample) & ~np.isnan(values[start:stop])
        segment = np.full(stop - start, NaN, dtype="float64")
        np.divide(below, counts, out=segment, where=ok)
        out[start:stop] = segment
    return out


# ---------------------------------------------------------------------------
# ADX / DMI  (Adx.kt)
# ---------------------------------------------------------------------------


class AdxResult:
    __slots__ = ("adx", "plus_di", "minus_di")

    def __init__(self, adx, plus_di, minus_di) -> None:
        self.adx = adx
        self.plus_di = plus_di
        self.minus_di = minus_di


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> AdxResult:
    """Full Wilder DMI/ADX. Port of `Adx.kt:33-73`.

    Port-critical detail: ``+DM``/``-DM`` are smoothed **from array index 0**,
    where a synthetic ``0.0`` sits (`Adx.kt:41-52`). Combined with
    ``tr[0] = high - low`` this is a small deviation from the textbook seed.
    Replicated verbatim — see docs/OPEN_QUESTIONS.md Q13.

    Warm-up: ``+DI``/``-DI`` first appear at index ``period - 1``; **ADX first
    appears at index ``2*period - 2``** because ``rma_nullable`` seeds at
    ``(period-1) + period - 1``.
    """
    high = np.asarray(high, dtype="float64")
    low = np.asarray(low, dtype="float64")
    close = np.asarray(close, dtype="float64")
    n = high.size

    if n < period + 1:
        empty = np.full(n, NaN, dtype="float64")
        return AdxResult(empty.copy(), empty.copy(), empty.copy())

    tr = true_range(high, low, close)

    plus_dm = np.zeros(n, dtype="float64")
    minus_dm = np.zeros(n, dtype="float64")
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm[1:] = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm[1:] = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    smoothed_tr = rma(tr, period)
    smoothed_plus = rma(plus_dm, period)
    smoothed_minus = rma(minus_dm, period)

    plus_di = np.full(n, NaN, dtype="float64")
    minus_di = np.full(n, NaN, dtype="float64")
    dx = np.full(n, NaN, dtype="float64")

    ok = (
        (~np.isnan(smoothed_tr))
        & (~np.isnan(smoothed_plus))
        & (~np.isnan(smoothed_minus))
        & (smoothed_tr != 0.0)
    )
    plus_di[ok] = 100.0 * smoothed_plus[ok] / smoothed_tr[ok]
    minus_di[ok] = 100.0 * smoothed_minus[ok] / smoothed_tr[ok]

    di_sum = plus_di + minus_di
    zero_sum = ok & (di_sum == 0.0)
    live = ok & (di_sum != 0.0)
    dx[zero_sum] = 0.0
    dx[live] = 100.0 * np.abs(plus_di[live] - minus_di[live]) / di_sum[live]

    return AdxResult(rma_nullable(dx, period), plus_di, minus_di)


def directional_sign(plus_di: np.ndarray, minus_di: np.ndarray) -> np.ndarray:
    """Direction implied by DI dominance, independent of trend strength.

    Port of `Adx.kt:12-20`.
    """
    plus_di = np.asarray(plus_di, dtype="float64")
    minus_di = np.asarray(minus_di, dtype="float64")
    out = np.zeros(plus_di.size, dtype="int8")
    known = (~np.isnan(plus_di)) & (~np.isnan(minus_di))
    out[known & (plus_di > minus_di)] = 1
    out[known & (minus_di > plus_di)] = -1
    return out


# ---------------------------------------------------------------------------
# VWAP  (Vwap.kt)
# ---------------------------------------------------------------------------

_DAY_MS = 86_400_000


def vwap(
    open_time: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    tick_volume: np.ndarray,
    session_offset_ms: int = 0,
) -> np.ndarray:
    """Session VWAP anchored to 00:00 UTC, typical price weighted by tick volume.

    Port of `Vwap.kt:19-42`.

    Returns all-NaN if the feed reports no volume at all — a flat VWAP built on
    zero volume would be a fabricated level (`Vwap.kt:15-17`).

    The first session in any window is **truncated** (it starts mid-session), so
    its values are averaged from an arbitrary anchor rather than the true 00:00
    open. See docs/OPEN_QUESTIONS.md Q33.
    """
    open_time = np.asarray(open_time, dtype="int64")
    tick_volume = np.asarray(tick_volume, dtype="float64")
    n = open_time.size
    out = np.full(n, NaN, dtype="float64")
    if n == 0 or not np.any(tick_volume > 0.0):
        return out

    typical = (
        np.asarray(high, dtype="float64")
        + np.asarray(low, dtype="float64")
        + np.asarray(close, dtype="float64")
    ) / 3.0

    # Math.floorDiv semantics — floor division, correct for negative epochs too.
    session = np.floor_divide(open_time - session_offset_ms, _DAY_MS)

    cum_pv = 0.0
    cum_vol = 0.0
    current = session[0]
    for i in range(n):
        if session[i] != current:
            current = session[i]
            cum_pv = 0.0
            cum_vol = 0.0
        volume = tick_volume[i]
        cum_pv += typical[i] * volume
        cum_vol += volume
        if cum_vol > 0.0:
            out[i] = cum_pv / cum_vol
    return out


# ---------------------------------------------------------------------------
# Relative volume  (RelativeVolume.kt)
# ---------------------------------------------------------------------------


def relative_volume(tick_volume: np.ndarray, period: int = 20) -> np.ndarray:
    """``volume[i]`` divided by the mean of the **previous** ``period`` bars,
    excluding the current bar. Port of `RelativeVolume.kt:16-26`.

    First value at index ``period``.

    The LIVE ``forming()`` variant (`RelativeVolume.kt:35-52`) prorates by elapsed
    bar time with ``MIN_ELAPSED = 0.05``, which can read up to ~20x in the first
    seconds of a bar. It is **deliberately not ported**: a closed-bar backtest
    never sees a forming bar, and porting it would import an intrabar artefact
    that inflates the volume confluence category. See docs/OPEN_QUESTIONS.md Q28.
    """
    tick_volume = np.asarray(tick_volume, dtype="float64")
    n = tick_volume.size
    out = np.full(n, NaN, dtype="float64")
    if n <= period:
        return out

    csum = np.cumsum(tick_volume, dtype="float64")
    # mean(volume[i-period : i]) for i in [period, n)
    trailing = np.empty(n - period, dtype="float64")
    trailing[0] = csum[period - 1]
    if n - period > 1:
        trailing[1:] = csum[period:-1] - csum[: n - period - 1]
    averages = trailing / period

    idx = np.arange(period, n)
    ok = averages > 0.0
    out[idx[ok]] = tick_volume[idx[ok]] / averages[ok]
    return out
