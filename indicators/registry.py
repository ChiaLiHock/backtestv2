"""Assembles every indicator for one timeframe into a single aligned frame.

Port of `analysis/indicators/IndicatorSet.kt` + `analysis/mtf/TimeframeAnalyzer.kt`.

**CONFIRMED only.** The app computes each indicator twice — over closed bars
(CONFIRMED) and over closed + forming (LIVE) — and reads LIVE for "where is price
right now" questions. A bar-closed backtest cannot use LIVE without look-ahead,
so every LIVE-sourced input is replaced by its CONFIRMED equivalent. This makes
backtest signals strictly fewer and later than the app's live alerts; see
docs/OPEN_QUESTIONS.md Q28.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import context, regime, structure, trend
from .base import IndicatorConfig, validate_ohlcv
from .mathseries import to_tick_volume

# TimeframeAnalyzer.kt:42 and ConfluenceEngine.kt:50 declare this constant twice,
# independently. Both are 40.0. See docs/OPEN_QUESTIONS.md Q6.
ADX_FULL_WEIGHT = 40.0

# core/Candle.kt:49 --- below this, ADX/Bollinger warm-up leaves too little signal
# to analyse honestly.
MIN_CANDLES = 60


def compute_indicators(
    df: pd.DataFrame,
    interval: str,
    config: IndicatorConfig | None = None,
) -> pd.DataFrame:
    """Every indicator for one timeframe, index-aligned with ``df``.

    Returns a frame with the same number of rows as ``df``. Warm-up regions are
    NaN (floats) or the family's UNDEFINED code (ints).
    """
    config = config or IndicatorConfig()
    validate_ohlcv(df)

    open_time = df["open_time"].to_numpy(dtype="int64")
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    tick_volume = to_tick_volume(df["volume"].to_numpy(dtype="float64"))

    out: dict[str, np.ndarray] = {"open_time": open_time}

    # --- ATR --------------------------------------------------------------
    atr = trend.atr(high, low, close, config.atr_period)
    out["atr_14"] = atr
    out["atr_percent"] = trend.atr_percent(atr, close)

    # --- EMA stack --------------------------------------------------------
    ema_fast = trend.ema(close, config.ema_fast)
    ema_mid = trend.ema(close, config.ema_mid)
    ema_slow = trend.ema(close, config.ema_slow)
    out["ema_7"] = ema_fast
    out["ema_14"] = ema_mid
    out["ema_28"] = ema_slow
    out["ema_alignment"] = trend.ema_alignment(ema_fast, ema_mid, ema_slow)

    separation = trend.separation_in_atr(ema_fast, ema_slow, atr)
    out["ema_separation_atr"] = separation
    out["ema_spread_trend"] = trend.spread_trend(separation, config.spread_lookback)
    out["extension_atr"] = trend.extension_from_slow_ema(close, ema_slow, atr)

    # --- momentum / channel ----------------------------------------------
    out["rsi_14"] = context.rsi(close, config.rsi_period)

    # Backtest-only (no app counterpart) — see context.macd.
    macd = context.macd(close, config.macd_fast, config.macd_slow, config.macd_signal)
    out["macd"] = macd.macd
    out["macd_signal"] = macd.signal
    out["macd_hist"] = macd.hist

    bb = context.bollinger(close, config.bollinger_period, config.bollinger_multiplier)
    out["bb_upper"] = bb.upper
    out["bb_middle"] = bb.middle
    out["bb_lower"] = bb.lower
    out["bb_width"] = bb.width
    out["bb_percent_b"] = bb.percent_b
    out["bb_width_percentile"] = context.bb_width_percentile(
        bb.width, config.bb_width_lookback, config.bb_width_min_sample
    )

    adx_result = context.adx(high, low, close, config.adx_period)
    out["adx_14"] = adx_result.adx
    out["plus_di"] = adx_result.plus_di
    out["minus_di"] = adx_result.minus_di
    out["adx_direction"] = context.directional_sign(
        adx_result.plus_di, adx_result.minus_di
    )

    # --- VWAP -------------------------------------------------------------
    # IndicatorSet.kt:126-132 force-nulls VWAP on H4: a session VWAP needs many
    # bars inside one session to mean anything, and on 4H there are six a day.
    if interval in config.vwap_suppressed_intervals:
        out["vwap"] = np.full(close.size, np.nan, dtype="float64")
    else:
        out["vwap"] = context.vwap(
            open_time, high, low, close, tick_volume, config.vwap_session_offset_ms
        )

    # --- volume -----------------------------------------------------------
    out["relative_volume"] = context.relative_volume(tick_volume, config.volume_period)

    # --- UT Bot -----------------------------------------------------------
    ut = trend.ut_bot(high, low, close, config.ut_key_value, config.ut_atr_period)
    out["ut_level"] = ut.trailing_stop
    out["ut_position"] = ut.position
    out["ut_buy"] = ut.buy
    out["ut_sell"] = ut.sell
    out["ut_bias"] = trend.ut_bias(close, ut.trailing_stop)

    # --- structure --------------------------------------------------------
    lookback = structure.swing_lookback_for(interval)
    states, _snapshots = structure.swing_state_series(
        high, low, open_time, atr, lookback, config.swing_min_separation_atr
    )
    struct = structure.market_structure(close, states)
    out["structure_state"] = struct.state
    out["bos"] = struct.bos
    out["choch"] = struct.choch
    out["last_swing_high"] = struct.last_swing_high
    out["last_swing_low"] = struct.last_swing_low
    out["prev_swing_high"] = struct.prev_swing_high
    out["prev_swing_low"] = struct.prev_swing_low

    # --- volatility -------------------------------------------------------
    vol = regime.classify_volatility(
        out["atr_percent"], config.volatility_lookback, config.volatility_min_sample
    )
    out["volatility_band"] = vol.band
    out["volatility_percentile"] = vol.percentile
    out["volatility_measured"] = vol.measured

    # --- per-timeframe directional score (TimeframeAnalyzer.kt:77-98) -----
    out["directional_score"] = directional_score(
        out["ema_alignment"], out["ut_bias"], out["adx_14"],
        out["adx_direction"], out["structure_state"],
    )
    out["confirmed_bias"] = trend.bias_from_score(out["directional_score"])

    # --- usability gate (core/Candle.kt:45-49, MIN_CANDLES = 60) ----------
    # `TimeframeAnalyzer.kt:66` marks a timeframe unusable below 60 closed bars;
    # `MultiTimeframeEngine.kt:58` drops it from the blend and `AlertEngine.kt:83`
    # skips it. The orchestrator itself forgets to check (OPEN_QUESTIONS Q8), which
    # is why a 20-bar anchor can still produce a headline verdict in the app. We
    # expose the flag so downstream consumers can enforce what the app should have.
    usable = np.zeros(close.size, dtype=bool)
    usable[MIN_CANDLES - 1 :] = True
    out["usable"] = usable

    frame = pd.DataFrame(out, index=df.index)
    return frame


def directional_score(
    ema_alignment: np.ndarray,
    ut_bias: np.ndarray,
    adx_values: np.ndarray,
    adx_direction: np.ndarray,
    structure_state: np.ndarray,
) -> np.ndarray:
    """Blends this timeframe's trend evidence into one number in [-1, 1].

    Port of `TimeframeAnalyzer.kt:77-98`. EMA alignment, UT position and ADX
    direction are all trend-following and are combined here rather than counted as
    independent confirmations (`:70-76`); structure is separate because price
    geometry is genuinely different information.

        trend = clamp(ema*0.4 + ut*0.4 + adx*0.2, -1, 1)
        score = clamp(trend*0.65 + structure*0.35, -1, 1)
    """
    ema_vote = np.zeros(np.asarray(ema_alignment).size, dtype="float64")
    ema_vote[np.asarray(ema_alignment) == trend.ALIGN_BULLISH] = 1.0
    ema_vote[np.asarray(ema_alignment) == trend.ALIGN_BEARISH] = -1.0

    ut_vote = np.asarray(ut_bias, dtype="float64")

    adx_values = np.asarray(adx_values, dtype="float64")
    adx_filled = np.where(np.isnan(adx_values), 0.0, adx_values)
    adx_vote = np.asarray(adx_direction, dtype="float64") * np.clip(
        adx_filled / ADX_FULL_WEIGHT, 0.0, 1.0
    )

    trend_component = np.clip(ema_vote * 0.4 + ut_vote * 0.4 + adx_vote * 0.2, -1.0, 1.0)

    struct_vote = np.zeros(trend_component.size, dtype="float64")
    struct_vote[np.asarray(structure_state) == structure.STRUCTURE_BULLISH] = 1.0
    struct_vote[np.asarray(structure_state) == structure.STRUCTURE_BEARISH] = -1.0

    return np.clip(trend_component * 0.65 + struct_vote * 0.35, -1.0, 1.0)


# Columns whose values are integer enum codes rather than measurements.
CATEGORICAL_COLUMNS = frozenset(
    {
        "ema_alignment",
        "ema_spread_trend",
        "ut_position",
        "ut_bias",
        "structure_state",
        "bos",
        "choch",
        "volatility_band",
        "confirmed_bias",
        "adx_direction",
    }
)

BOOLEAN_COLUMNS = frozenset({"ut_buy", "ut_sell", "volatility_measured", "usable"})
