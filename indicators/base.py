"""Indicator interface and the single tunables object.

`IndicatorConfig` is the port of `analysis/indicators/IndicatorSet.kt:9-24` plus
the constants the Kotlin buried inside engines. Everything is exposed here so a
strategy YAML can override any of it — the shipping app only lets you change
`ut_key_value` and `ut_atr_period` (docs/OPEN_QUESTIONS.md Q35).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel, Field


class IndicatorConfig(BaseModel):
    """Every tunable in one place, so nothing is a magic number inside an engine."""

    model_config = {"frozen": True, "extra": "forbid"}

    # --- UT Bot -----------------------------------------------------------
    # NOTE: the Kotlin has TWO conflicting defaults --- UtBot.DEFAULT_KEY_VALUE
    # is 3.0 (UtBot.kt:70) while UtSettings.DEFAULT_KEY_VALUE is 2.0
    # (SettingsStore.kt:30). Every production path builds IndicatorConfig from
    # UtSettings, so 2.0 is what the app actually displays. We default to 2.0.
    # See docs/OPEN_QUESTIONS.md Q2.
    ut_key_value: float = Field(default=2.0, gt=0.0)
    ut_atr_period: int = Field(default=10, ge=1)

    # --- context indicators ----------------------------------------------
    atr_period: int = Field(default=14, ge=1)
    rsi_period: int = Field(default=14, ge=1)
    adx_period: int = Field(default=14, ge=1)
    bollinger_period: int = Field(default=20, ge=1)
    bollinger_multiplier: float = Field(default=2.0, gt=0.0)
    volume_period: int = Field(default=20, ge=1)

    # MACD is NOT in the app — backtest-only, added for rule-building, and so
    # NOT covered by the confirmed indicator parity.
    macd_fast: int = Field(default=12, ge=1)
    macd_slow: int = Field(default=26, ge=1)
    macd_signal: int = Field(default=9, ge=1)

    # --- EMA stack --------------------------------------------------------
    ema_fast: int = Field(default=7, ge=1)
    ema_mid: int = Field(default=14, ge=1)
    ema_slow: int = Field(default=28, ge=1)
    spread_lookback: int = Field(default=10, ge=1)

    # --- VWAP -------------------------------------------------------------
    # 0 = anchored to 00:00 UTC. The Kotlin documents 21h as the FX day roll but
    # no call site ever sets it (OPEN_QUESTIONS Q17).
    vwap_session_offset_ms: int = 0
    # IndicatorSet.kt:126-132 force-nulls VWAP on H4. Replicated.
    vwap_suppressed_intervals: tuple[str, ...] = ("4h", "1d")

    # --- Bollinger width percentile (Bollinger.kt:18-21) ------------------
    bb_width_lookback: int = Field(default=100, ge=1)
    bb_width_min_sample: int = Field(default=20, ge=1)

    # --- volatility classifier (VolatilityClassifier.kt:27-28) ------------
    volatility_lookback: int = Field(default=150, ge=1)
    volatility_min_sample: int = Field(default=40, ge=1)

    # --- structure (SwingDetector.kt:20, :24-27) --------------------------
    swing_min_separation_atr: float = 0.5
    # lookback is per-timeframe; see structure.swing_lookback_for()

    # --- history window ---------------------------------------------------
    # Bars discarded at the start of every run. The app seeds every recursive
    # indicator at the start of a rolling 500-bar window (MarketDataManager.kt:250),
    # so 500 is the point past which full-history and windowed computation have
    # converged for EMA/ATR/RSI/ADX/UT. See docs/OPEN_QUESTIONS.md Q4.
    warmup_bars: int = Field(default=500, ge=0)

    # The app never holds more than this many bars per timeframe
    # (`MarketDataManager.kt:250` DEFAULT_CANDLE_COUNT = 500, applied by
    # `takeLast(maxBars)` at :283).
    #
    # ⚠️ This is NOT just a warm-up concern for support/resistance. Recursive
    # indicators converge as history grows; the swing chain does the opposite —
    # it accumulates. Measured on XAUUSDT: full history yields 1633/590/133
    # accepted swings on 30m/1h/4h against 104/85/61 for a 500-bar window. Because
    # `SupportResistanceEngine.cluster` merges transitively, 9x the levels collapse
    # into a handful of enormous bands that look nothing like the app's.
    #
    # Zone construction is therefore ALWAYS windowed to this value, independently
    # of the full-history choice for everything else. See docs/OPEN_QUESTIONS.md Q4.
    app_window_bars: int = Field(default=500, ge=1)


@runtime_checkable
class Indicator(Protocol):
    """Uniform interface for every indicator family.

    ``compute`` takes an OHLCV frame indexed 0..n-1 with columns
    ``open_time, open, high, low, close, volume`` and returns a frame of the same
    length whose columns are all prefixed with ``name``.
    """

    name: str
    params: BaseModel
    warmup_bars: int

    def compute(self, df: pd.DataFrame) -> pd.DataFrame: ...


REQUIRED_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")


def validate_ohlcv(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing columns: {missing}")
    if not df["open_time"].is_monotonic_increasing:
        raise ValueError("OHLCV frame must be sorted ascending by open_time")
    if df["open_time"].duplicated().any():
        raise ValueError("OHLCV frame contains duplicate open_time values")
