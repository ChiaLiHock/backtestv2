"""ATR, EMA stack and the UT Bot — the trend-following family.

Exact ports of `analysis/indicators/Atr.kt`, `Ema.kt` and `UtBot.kt`.
"""

from __future__ import annotations

import numpy as np

from .mathseries import NaN, rma

# ---------------------------------------------------------------------------
# ATR  (Atr.kt)
# ---------------------------------------------------------------------------


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Port of `Atr.kt:14-22`.

    The first bar has no previous close so it degrades to ``high - low``. This
    matches Pine's ``tr(true)`` and matters because the UT Bot seeds off it
    (`Atr.kt:10-13`).
    """
    high = np.asarray(high, dtype="float64")
    low = np.asarray(low, dtype="float64")
    close = np.asarray(close, dtype="float64")
    n = high.size
    if n == 0:
        return np.empty(0, dtype="float64")

    out = np.empty(n, dtype="float64")
    out[0] = high[0] - low[0]
    if n > 1:
        prev_close = close[:-1]
        out[1:] = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
    return out


def atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder ATR. First value at index ``period - 1``. Port of `Atr.kt:25-26`."""
    return rma(true_range(high, low, close), period)


def atr_percent(atr_values: np.ndarray, close: np.ndarray) -> np.ndarray:
    """ATR as a percentage of close — the scale-free form. Port of `Atr.kt:29-33`."""
    atr_values = np.asarray(atr_values, dtype="float64")
    close = np.asarray(close, dtype="float64")
    out = np.full(atr_values.size, NaN, dtype="float64")
    valid = (~np.isnan(atr_values)) & (~np.isnan(close)) & (close != 0.0)
    out[valid] = atr_values[valid] / close[valid] * 100.0
    return out


# ---------------------------------------------------------------------------
# EMA  (Ema.kt)
# ---------------------------------------------------------------------------


def ema(source: np.ndarray, period: int) -> np.ndarray:
    """SMA-seeded EMA, matching Pine's ``ta.ema``. Port of `Ema.kt:9-20`.

    First value lands at index ``period - 1``; ``k = 2/(period+1)``.
    """
    source = np.asarray(source, dtype="float64")
    n = source.size
    if period <= 0 or n < period:
        return np.full(n, NaN, dtype="float64")

    out = np.full(n, NaN, dtype="float64")
    prev = float(source[:period].sum()) / period
    out[period - 1] = prev
    k = 2.0 / (period + 1)
    for i in range(period, n):
        prev = (source[i] - prev) * k + prev
        out[i] = prev
    return out


# EmaAlignment as integer codes so the whole frame stays numeric.
ALIGN_UNDEFINED, ALIGN_BULLISH, ALIGN_BEARISH, ALIGN_MIXED = 0, 1, -1, 2

# EmaSpreadTrend codes.
SPREAD_UNDEFINED, SPREAD_EXPANDING, SPREAD_COMPRESSING, SPREAD_STABLE = 0, 1, -1, 2


def ema_alignment(
    fast: np.ndarray, mid: np.ndarray, slow: np.ndarray
) -> np.ndarray:
    """Port of `Ema.kt:34-44`. Any NaN -> UNDEFINED."""
    fast = np.asarray(fast, dtype="float64")
    mid = np.asarray(mid, dtype="float64")
    slow = np.asarray(slow, dtype="float64")

    out = np.full(fast.size, ALIGN_UNDEFINED, dtype="int8")
    known = (~np.isnan(fast)) & (~np.isnan(mid)) & (~np.isnan(slow))
    out[known] = ALIGN_MIXED
    out[known & (fast > mid) & (mid > slow)] = ALIGN_BULLISH
    out[known & (fast < mid) & (mid < slow)] = ALIGN_BEARISH
    return out


def separation_in_atr(
    fast: np.ndarray, slow: np.ndarray, atr_values: np.ndarray
) -> np.ndarray:
    """``(EMA7 - EMA28) / ATR``. Positive = bullish spread. Port of `Ema.kt:47-52`."""
    fast = np.asarray(fast, dtype="float64")
    slow = np.asarray(slow, dtype="float64")
    atr_values = np.asarray(atr_values, dtype="float64")

    out = np.full(fast.size, NaN, dtype="float64")
    valid = (
        (~np.isnan(fast))
        & (~np.isnan(slow))
        & (~np.isnan(atr_values))
        & (atr_values > 0.0)
    )
    out[valid] = (fast[valid] - slow[valid]) / atr_values[valid]
    return out


def spread_trend(separation: np.ndarray, lookback: int = 10) -> np.ndarray:
    """Port of `IndicatorSet.kt:90-103`.

    ``before <= 0.0001`` -> STABLE; ``now > before*1.15`` -> EXPANDING;
    ``now < before*0.85`` -> COMPRESSING; else STABLE. Any NaN -> UNDEFINED.
    """
    separation = np.asarray(separation, dtype="float64")
    n = separation.size
    out = np.full(n, SPREAD_UNDEFINED, dtype="int8")
    if n <= lookback:
        return out

    now = np.abs(separation[lookback:])
    before = np.abs(separation[:-lookback])
    known = (~np.isnan(now)) & (~np.isnan(before))

    seg = np.full(now.size, SPREAD_UNDEFINED, dtype="int8")
    seg[known] = SPREAD_STABLE
    tiny = known & (before <= 0.0001)
    seg[known & ~tiny & (now > before * 1.15)] = SPREAD_EXPANDING
    seg[known & ~tiny & (now < before * 0.85)] = SPREAD_COMPRESSING
    seg[tiny] = SPREAD_STABLE
    out[lookback:] = seg
    return out


def extension_from_slow_ema(
    close: np.ndarray, slow: np.ndarray, atr_values: np.ndarray
) -> np.ndarray:
    """``(close - EMA28) / ATR`` — the core "extended" measure.

    Port of `IndicatorSet.kt:82-88`; `LocationAnalyzer.kt:72` recomputes the same
    quantity inline.
    """
    close = np.asarray(close, dtype="float64")
    slow = np.asarray(slow, dtype="float64")
    atr_values = np.asarray(atr_values, dtype="float64")

    out = np.full(close.size, NaN, dtype="float64")
    valid = (
        (~np.isnan(close))
        & (~np.isnan(slow))
        & (~np.isnan(atr_values))
        & (atr_values > 0.0)
    )
    out[valid] = (close[valid] - slow[valid]) / atr_values[valid]
    return out


# ---------------------------------------------------------------------------
# UT Bot  (UtBot.kt) --- the "UT Dynamic Level"
# ---------------------------------------------------------------------------


class UtBotResult:
    """Index-aligned outputs of the UT Bot.

    ``trailing_stop`` is the **UT Dynamic Level** — an ATR-based trailing stop.
    It is deliberately never called support or resistance (`UtBot.kt:9-10`).
    """

    __slots__ = ("trailing_stop", "position", "buy", "sell")

    def __init__(
        self,
        trailing_stop: np.ndarray,
        position: np.ndarray,
        buy: np.ndarray,
        sell: np.ndarray,
    ) -> None:
        self.trailing_stop = trailing_stop
        self.position = position
        self.buy = buy
        self.sell = sell


def ut_bot(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    key_value: float = 2.0,
    atr_period: int = 10,
) -> UtBotResult:
    """Faithful port of the UT Bot Pine study — `UtBot.kt:73-129`.

    Four behaviours are replicated on purpose (`UtBot.kt:56-66`); do NOT tidy any
    of them, each one changes where signals fire:

    1. ``atr()`` is Wilder RMA, so the first value appears at index ``period-1``.
    2. ``nz(xATRTrailingStop[1], 0)`` means the first ATR-available bar compares
       against **0**. Gold is positive, so the first branch wins and the stop
       initialises to ``close - nLoss`` — a bullish seed. Substituting ``close``
       for that 0 moves every subsequent flip.
    3. ``pos`` only changes on an actual cross and otherwise carries forward.
    4. ``crossover``/``crossunder`` compare the current close against the
       **current** stop and the previous close against the **previous** stop, and
       are false while the stop is still NaN.

    Because the recursion seeds at the start of whatever slice it is handed, the
    result depends on how much history is supplied. See docs/OPEN_QUESTIONS.md Q4.
    """
    high = np.asarray(high, dtype="float64")
    low = np.asarray(low, dtype="float64")
    close = np.asarray(close, dtype="float64")
    n = close.size

    stop = np.full(n, NaN, dtype="float64")
    position = np.zeros(n, dtype="int8")
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    if n == 0:
        return UtBotResult(stop, position, buy, sell)

    atr_values = atr(high, low, close, atr_period)

    for i in range(n):
        current_atr = atr_values[i]
        if np.isnan(current_atr):
            # xATR is na, so nLoss is na and the whole expression evaluates to na.
            position[i] = position[i - 1] if i > 0 else 0
            continue

        n_loss = key_value * current_atr
        src = close[i]
        prev_src = close[i - 1] if i > 0 else src
        prev_stop_raw = stop[i - 1] if i > 0 else NaN
        has_prev = not np.isnan(prev_stop_raw)
        prev_stop = prev_stop_raw if has_prev else 0.0  # nz(xATRTrailingStop[1], 0)

        if src > prev_stop and prev_src > prev_stop:
            stop[i] = max(prev_stop, src - n_loss)
        elif src < prev_stop and prev_src < prev_stop:
            stop[i] = min(prev_stop, src + n_loss)
        elif src > prev_stop:
            stop[i] = src - n_loss
        else:
            stop[i] = src + n_loss

        if prev_src < prev_stop and src > prev_stop:
            position[i] = 1
        elif prev_src > prev_stop and src < prev_stop:
            position[i] = -1
        else:
            position[i] = position[i - 1] if i > 0 else 0

        # crossover/crossunder are na-safe: no signal until the stop series exists.
        if has_prev:
            buy[i] = src > stop[i] and prev_src <= prev_stop_raw
            sell[i] = src < stop[i] and prev_src >= prev_stop_raw

    return UtBotResult(stop, position, buy, sell)


# Bias codes, matching core/Enums.kt Bias.sign.
BIAS_NEUTRAL, BIAS_BULLISH, BIAS_BEARISH = 0, 1, -1


def ut_bias(close: np.ndarray, trailing_stop: np.ndarray) -> np.ndarray:
    """Port of `UtBot.kt:23-28`: above the level is bullish, below is bearish,
    equal or unknown is neutral."""
    close = np.asarray(close, dtype="float64")
    trailing_stop = np.asarray(trailing_stop, dtype="float64")
    out = np.full(close.size, BIAS_NEUTRAL, dtype="int8")
    known = (~np.isnan(close)) & (~np.isnan(trailing_stop))
    out[known & (close > trailing_stop)] = BIAS_BULLISH
    out[known & (close < trailing_stop)] = BIAS_BEARISH
    return out


def bias_from_score(score: np.ndarray, dead_zone: float = 0.15) -> np.ndarray:
    """Port of `core/Enums.kt:15-19`.

    Default dead zone 0.15. `MarketAnalysisEngine.kt:200` overrides it to 0.2 for
    Analysis-page tone only — two dead zones coexist (OPEN_QUESTIONS Q15).
    """
    score = np.asarray(score, dtype="float64")
    out = np.full(score.size, BIAS_NEUTRAL, dtype="int8")
    out[score > dead_zone] = BIAS_BULLISH
    out[score < -dead_zone] = BIAS_BEARISH
    return out
