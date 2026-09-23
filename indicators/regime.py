"""Volatility classification and market regime.

Ports `analysis/regime/VolatilityClassifier.kt` and `MarketRegimeEngine.kt`.
Both are evaluated per bar over a causal trailing window.
"""

from __future__ import annotations

import numpy as np

from .mathseries import NaN
from .structure import STRUCTURE_BEARISH, STRUCTURE_BULLISH
from .trend import ALIGN_BEARISH, ALIGN_BULLISH

# VolatilityBand codes (core/Enums.kt:37).
VOL_LOW, VOL_NORMAL, VOL_HIGH, VOL_EXTREME = 0, 1, 2, 3
VOL_BAND_NAMES = {VOL_LOW: "LOW", VOL_NORMAL: "NORMAL", VOL_HIGH: "HIGH", VOL_EXTREME: "EXTREME"}

# RegimeTag primary codes (core/Enums.kt:50-52).
REGIME_RANGE, REGIME_TREND_UP, REGIME_TREND_DOWN = 0, 1, -1
REGIME_NAMES = {
    REGIME_RANGE: "RANGE",
    REGIME_TREND_UP: "TREND_UP",
    REGIME_TREND_DOWN: "TREND_DOWN",
}

ADX_TREND_FLOOR = 20.0  # MarketRegimeEngine.kt:36


class VolatilitySeries:
    """Per-bar band, percentile and a flag for the degraded branch."""

    __slots__ = ("band", "percentile", "atr_percent", "measured")

    def __init__(self, band, percentile, atr_percent, measured) -> None:
        self.band = band
        self.percentile = percentile
        self.atr_percent = atr_percent
        self.measured = measured


def classify_volatility(
    atr_percent: np.ndarray, lookback: int = 150, min_sample: int = 40
) -> VolatilitySeries:
    """Port of `VolatilityClassifier.kt:30-43`, evaluated at every bar.

    Deliberately percentile-based rather than threshold-based: "ATR above 4.00"
    means one thing when gold is at 1,800 and another at 4,300 (`:17-24`).

    ⚠️ Degraded branch: a sample below ``min_sample`` returns band **NORMAL** with
    a null percentile (`:33`), which in the app is indistinguishable from a
    measured NORMAL — and while in that state the EXTREME veto, the
    HIGH_VOLATILITY tag and the extreme-volatility alert are all structurally
    unable to fire. We keep the band for parity but expose ``measured`` so runs
    can be filtered on it (docs/OPEN_QUESTIONS.md Q7).

    Kotlin nuance replicated: ``current`` is the last non-null of the **whole**
    series so far (`:31`) while the sample is ``takeLast(lookback)`` (`:32`). They
    coincide except when the tail is all-NaN.
    """
    atr_percent = np.asarray(atr_percent, dtype="float64")
    n = atr_percent.size

    band = np.full(n, VOL_NORMAL, dtype="int8")
    percentile = np.full(n, NaN, dtype="float64")
    current_series = np.full(n, NaN, dtype="float64")
    measured = np.zeros(n, dtype=bool)

    # `current` is the last non-NaN of the WHOLE series so far (`:31`), which is
    # not necessarily atr_percent[t] — forward-fill reproduces that exactly.
    known = ~np.isnan(atr_percent)
    if known.any():
        idx = np.where(known, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        filled = atr_percent[idx]
        # Everything before the first known value stays NaN.
        first_known = int(np.argmax(known))
        filled[:first_known] = NaN
        current_series = filled

    pct = _trailing_percentile(atr_percent, current_series, lookback, min_sample)
    percentile = pct
    measured = ~np.isnan(pct)

    band[measured & (pct < 0.25)] = VOL_LOW
    band[measured & (pct >= 0.25) & (pct < 0.75)] = VOL_NORMAL
    band[measured & (pct >= 0.75) & (pct < 0.90)] = VOL_HIGH
    band[measured & (pct >= 0.90)] = VOL_EXTREME

    return VolatilitySeries(band, percentile, current_series, measured)


def _trailing_percentile(
    sample_source: np.ndarray,
    values: np.ndarray,
    lookback: int,
    min_sample: int,
    chunk: int = 8192,
) -> np.ndarray:
    """Rank ``values[i]`` against the non-NaN entries of the trailing window of
    ``sample_source``. NaN where the window holds fewer than ``min_sample``.

    Split from the Bollinger helper because here the ranked value and the sample
    come from different arrays: `VolatilityClassifier.kt:31-32` ranks the
    forward-filled last-known ATR% against the raw trailing window.
    """
    sample_source = np.asarray(sample_source, dtype="float64")
    values = np.asarray(values, dtype="float64")
    n = sample_source.size
    out = np.full(n, NaN, dtype="float64")
    if n == 0 or lookback <= 0:
        return out

    padded = np.concatenate(
        [np.full(lookback - 1, NaN, dtype="float64"), sample_source]
    )
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


class RegimeSeries:
    __slots__ = ("primary", "high_volatility", "low_volatility", "news_risk", "net_direction")

    def __init__(self, primary, high_volatility, low_volatility, news_risk, net_direction) -> None:
        self.primary = primary
        self.high_volatility = high_volatility
        self.low_volatility = low_volatility
        self.news_risk = news_risk
        self.net_direction = net_direction


def classify_regime(
    adx_values: np.ndarray,
    ema_alignment: np.ndarray,
    adx_direction: np.ndarray,
    structure_state: np.ndarray,
    volatility_band: np.ndarray,
    news_risk_high: np.ndarray | None = None,
) -> RegimeSeries:
    """Port of `MarketRegimeEngine.kt:38-86`.

    Three directional votes — EMA alignment, ADX DI dominance, structure state —
    are summed; a net of ±2 plus ``adx >= 20`` produces a trend label
    (`:49-68`). A market can carry several tags at once.

    All inputs must come from the **direction anchor (H1), CONFIRMED slice**
    (`MarketAnalysisEngine.kt:78-83`).
    """
    adx_values = np.asarray(adx_values, dtype="float64")
    n = adx_values.size

    adx_filled = np.where(np.isnan(adx_values), 0.0, adx_values)
    trending = adx_filled >= ADX_TREND_FLOOR

    ema_vote = np.zeros(n, dtype="int8")
    ema_vote[np.asarray(ema_alignment) == ALIGN_BULLISH] = 1
    ema_vote[np.asarray(ema_alignment) == ALIGN_BEARISH] = -1

    struct_vote = np.zeros(n, dtype="int8")
    struct_vote[np.asarray(structure_state) == STRUCTURE_BULLISH] = 1
    struct_vote[np.asarray(structure_state) == STRUCTURE_BEARISH] = -1

    net = (
        ema_vote.astype("int16")
        + np.asarray(adx_direction, dtype="int16")
        + struct_vote.astype("int16")
    )

    primary = np.full(n, REGIME_RANGE, dtype="int8")
    primary[trending & (net >= 2)] = REGIME_TREND_UP
    primary[trending & (net <= -2)] = REGIME_TREND_DOWN

    vb = np.asarray(volatility_band)
    high_vol = (vb == VOL_HIGH) | (vb == VOL_EXTREME)
    low_vol = vb == VOL_LOW

    if news_risk_high is None:
        news = np.zeros(n, dtype=bool)
    else:
        news = np.asarray(news_risk_high, dtype=bool)

    return RegimeSeries(primary, high_vol, low_vol, news, net.astype("int8"))
