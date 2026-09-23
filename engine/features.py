"""Per-trade feature snapshots.

Captured as of the **entry bar t, closed**. The whole point of this table is to
answer "which conditions win", so a single leaked future value here would
poison the analysis in a way that is very hard to notice later.

The trap this module exists to avoid: at the close of a 1H bar, the current 4H
bar has usually **not finished**. Reading "the 4H value at time t" from a
naively-aligned frame hands the engine a bar that closes hours in the future. So
every higher timeframe is resolved with an as-of join onto bars whose *close*
time is <= the anchor bar's close time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS
from ..indicators.regime import REGIME_NAMES, VOL_BAND_NAMES
from ..indicators.structure import STRUCTURE_BEARISH, STRUCTURE_BULLISH, STRUCTURE_RANGE
from ..indicators.trend import (
    ALIGN_BEARISH,
    ALIGN_BULLISH,
    ALIGN_MIXED,
    ALIGN_UNDEFINED,
    BIAS_BEARISH,
    BIAS_BULLISH,
    BIAS_NEUTRAL,
)

MYT = timezone(timedelta(hours=8))

# Columns whose integer codes should be recorded as readable labels.
LABEL_MAPS: dict[str, dict[int, str]] = {
    "ema_alignment": {
        ALIGN_UNDEFINED: "UNDEFINED",
        ALIGN_BULLISH: "BULLISH",
        ALIGN_BEARISH: "BEARISH",
        ALIGN_MIXED: "MIXED",
    },
    "ut_bias": {BIAS_NEUTRAL: "NEUTRAL", BIAS_BULLISH: "BULLISH", BIAS_BEARISH: "BEARISH"},
    "confirmed_bias": {BIAS_NEUTRAL: "NEUTRAL", BIAS_BULLISH: "BULLISH", BIAS_BEARISH: "BEARISH"},
    "structure_state": {
        STRUCTURE_RANGE: "RANGE",
        STRUCTURE_BULLISH: "BULLISH",
        STRUCTURE_BEARISH: "BEARISH",
    },
    "bos": {BIAS_NEUTRAL: "NONE", BIAS_BULLISH: "BULLISH", BIAS_BEARISH: "BEARISH"},
    "choch": {BIAS_NEUTRAL: "NONE", BIAS_BULLISH: "BULLISH", BIAS_BEARISH: "BEARISH"},
    "volatility_band": VOL_BAND_NAMES,
    "regime": REGIME_NAMES,
}


def closed_bar_index(open_times: np.ndarray, interval: str, anchor_close_ms: int) -> int:
    """Index of the newest bar of `interval` that had CLOSED by `anchor_close_ms`.

    A bar opening at ``o`` closes at ``o + step``, so it qualifies when
    ``o + step <= anchor_close_ms``. Returns -1 when none has.
    """
    step = INTERVAL_MS[interval]
    idx = int(np.searchsorted(open_times, anchor_close_ms - step, side="right")) - 1
    return idx


def build_htf_alignment(
    anchor_open_times: np.ndarray,
    anchor_interval: str,
    frames: dict[str, tuple[np.ndarray, pd.DataFrame]],
) -> dict[str, np.ndarray]:
    """For each anchor bar, the index of the last CLOSED bar of each timeframe.

    ``frames`` maps interval -> (open_times, indicator_frame).
    Returns interval -> int array of indices (-1 where nothing has closed yet).
    """
    anchor_step = INTERVAL_MS[anchor_interval]
    anchor_close = anchor_open_times + anchor_step

    out: dict[str, np.ndarray] = {}
    for interval, (open_times, _frame) in frames.items():
        step = INTERVAL_MS[interval]
        # searchsorted over the whole anchor series at once.
        idx = np.searchsorted(open_times, anchor_close - step, side="right") - 1
        out[interval] = idx.astype("int64")
    return out


def aligned_frame(
    frame: pd.DataFrame, indices: np.ndarray, prefix: str | None = None
) -> pd.DataFrame:
    """Re-index `frame` onto the anchor grid using precomputed indices.

    Rows where **nothing of that timeframe had closed yet** are blanked rather
    than borrowing row 0 — a 4H value cannot exist for a 1H bar that predates the
    first completed 4H candle, and silently substituting one would be a small,
    invisible look-ahead.

    Blanking is per dtype: floats become NaN, integer enum codes are widened to
    float so they can hold NaN (comparisons against a code still work, and NaN
    matches nothing, which is the intent), booleans become False.
    """
    safe = np.clip(indices, 0, max(len(frame) - 1, 0))
    out = frame.iloc[safe].reset_index(drop=True)

    invalid = np.asarray(indices) < 0
    if invalid.any():
        for col in out.columns:
            dtype = out[col].dtype
            if dtype == bool:
                out.loc[invalid, col] = False
            elif np.issubdtype(dtype, np.integer):
                out[col] = out[col].astype("float64")
                out.loc[invalid, col] = np.nan
            else:
                out.loc[invalid, col] = np.nan

    if prefix:
        out = out.add_prefix(f"{prefix}_")
    return out


def session_myt(ts_ms: int) -> str:
    """Trading session label.

    ⚠️ **BACKTEST-ONLY.** No asia/london/ny classifier exists anywhere in the app
    (verified by grep over `analysis/` and `core/`); its only session concepts are
    the VWAP UTC-midnight anchor and the weekend `MarketHours` roll. These windows
    are a reasonable convention, not a port. See `docs/OPEN_QUESTIONS.md` Q26.

    London and New York overlap; NY wins in the overlap because that is when the
    larger flow is.
    """
    h = datetime.fromtimestamp(ts_ms / 1000, MYT).hour
    if 20 <= h or h < 5:
        return "ny"
    if 15 <= h < 20:
        return "london"
    if 7 <= h < 15:
        return "asia"
    return "off"


def is_gold_session(ts_ms: int) -> bool:
    """The app's own market hours (`core/MarketHours.kt:25-33`), inverted.

    Saturday shut; Friday from 22:00 UTC shut; Sunday before 21:00 UTC shut.
    """
    d = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    wd = d.weekday()  # Mon=0
    if wd == 5:
        return False
    if wd == 4 and d.hour >= 22:
        return False
    if wd == 6 and d.hour < 21:
        return False
    return True


def is_weekday_myt(ts_ms: int) -> bool:
    return datetime.fromtimestamp(ts_ms / 1000, MYT).weekday() <= 4


def rolling_percentile_at(values: np.ndarray, index: int, lookback: int) -> float:
    """Percentile rank of ``values[index]`` within its own trailing window."""
    if index < 0 or index >= values.size:
        return float("nan")
    current = values[index]
    if np.isnan(current):
        return float("nan")
    start = max(0, index - lookback + 1)
    sample = values[start : index + 1]
    sample = sample[~np.isnan(sample)]
    if sample.size == 0:
        return float("nan")
    return float(np.count_nonzero(sample <= current)) / sample.size


def snapshot(
    trade_id: str,
    entry_index: int,
    entry_open_time: int,
    anchor_interval: str,
    anchor: pd.DataFrame,
    htf_frames: dict[str, tuple[np.ndarray, pd.DataFrame]],
    htf_indices: dict[str, np.ndarray],
    include: list[str],
    derived: list[str],
    extras: dict[str, float | str | None],
) -> list[tuple[str, str, float | None, str | None]]:
    """Build the `trade_features` rows for one trade.

    Returns (trade_id, feature, value_num, value_txt) tuples.
    """
    rows: list[tuple[str, str, float | None, str | None]] = []

    def put(name: str, value) -> None:
        if value is None:
            return
        if isinstance(value, str):
            rows.append((trade_id, name, None, value))
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            rows.append((trade_id, name, None, str(value)))
            return
        if np.isnan(v):
            return
        rows.append((trade_id, name, v, None))

    # --- per-timeframe indicator values ----------------------------------
    for interval, (_ot, frame) in htf_frames.items():
        idx = int(htf_indices[interval][entry_index]) if interval != anchor_interval else entry_index
        if idx < 0 or idx >= len(frame):
            continue
        row = frame.iloc[idx]
        for col in include:
            if col not in frame.columns:
                continue
            raw = row[col]
            label_map = LABEL_MAPS.get(col)
            if label_map is not None:
                try:
                    put(f"{interval}_{col}", label_map.get(int(raw), "UNKNOWN"))
                except (TypeError, ValueError):
                    pass
            else:
                put(f"{interval}_{col}", raw)

    # --- derived context --------------------------------------------------
    a = anchor.iloc[entry_index]
    close = float(extras.get("entry_bar_close", np.nan))

    if "dist_to_ut_pct" in derived and close == close:
        ut = float(a.get("ut_level", np.nan))
        if ut == ut and close:
            put("dist_to_ut_pct", (close - ut) / close * 100.0)
    if "dist_to_ema_fast_pct" in derived and close:
        v = float(a.get("ema_7", np.nan))
        if v == v:
            put("dist_to_ema_fast_pct", (close - v) / close * 100.0)
    if "dist_to_ema_slow_pct" in derived and close:
        v = float(a.get("ema_28", np.nan))
        if v == v:
            put("dist_to_ema_slow_pct", (close - v) / close * 100.0)
    if "atr_percentile_250" in derived:
        put(
            "atr_percentile_250",
            rolling_percentile_at(anchor["atr_14"].to_numpy(), entry_index, 250),
        )

    if "hour_of_day_myt" in derived:
        put("hour_of_day_myt", datetime.fromtimestamp(entry_open_time / 1000, MYT).hour)
    if "day_of_week_myt" in derived:
        d = datetime.fromtimestamp(entry_open_time / 1000, MYT)
        put("day_of_week_myt", d.strftime("%a"))
    if "session" in derived:
        put("session", session_myt(entry_open_time))

    for key, value in extras.items():
        if key == "entry_bar_close":
            continue
        put(key, value)

    return rows
