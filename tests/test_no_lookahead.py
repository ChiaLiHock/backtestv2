"""No-look-ahead tests.

The contract: a value computed at bar ``t`` must depend only on bars ``<= t``.
The way to prove that is truncation — compute over ``[0..T]``, compute again over
``[0..t]`` for ``t < T``, and require the overlapping region to be identical.

This is the acceptance criterion that catches the class of bug that makes a
backtest look profitable and a live account not.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.indicators import structure
from backtest.indicators.base import IndicatorConfig
from backtest.indicators.registry import (
    BOOLEAN_COLUMNS,
    CATEGORICAL_COLUMNS,
    compute_indicators,
)


def _ohlcv(n: int = 900, seed: int = 5, interval_ms: int = 1_800_000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Regime-switching series so pivots, crosses and band touches all occur.
    drift = np.concatenate(
        [
            np.full(n // 3, 0.6),
            np.full(n // 3, -0.8),
            np.full(n - 2 * (n // 3), 0.1),
        ]
    )
    close = 2000 + np.cumsum(rng.normal(drift, 4.0))
    spread = np.abs(rng.normal(0, 2.5, n)) + 0.8
    return pd.DataFrame(
        {
            "open_time": np.arange(n, dtype="int64") * interval_ms,
            "open": close - rng.normal(0, 1.0, n),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.abs(rng.normal(120, 40, n)),
            "turnover": np.abs(rng.normal(240000, 60000, n)),
        }
    )


# Columns that are legitimately confirmed-late: a fractal pivot at index i is not
# visible until bar i + lookback, so the LAST `lookback` bars of any truncated run
# have not yet seen pivots the fuller run has. The overlap comparison therefore
# trims `lookback` bars off the tail. That trimming is the whole point: it proves
# the value is stable once confirmed, rather than hiding an actual leak.
_STRUCTURE_COLUMNS = (
    "structure_state",
    "bos",
    "choch",
    "last_swing_high",
    "last_swing_low",
    "prev_swing_high",
    "prev_swing_low",
    "directional_score",
    "confirmed_bias",
)


@pytest.mark.parametrize("interval", ["30m", "1h", "4h"])
def test_truncation_reproduces_overlapping_region(interval: str):
    """The core test. Truncating the dataset must not change any earlier value."""
    df = _ohlcv(interval_ms={"30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}[interval])
    full = compute_indicators(df, interval)

    for cut in (600, 700, 850):
        truncated = compute_indicators(df.iloc[:cut].reset_index(drop=True), interval)
        # Structure is confirmed `lookback` bars late; trim that margin.
        margin = structure.swing_lookback_for(interval)
        compare_to = cut - margin

        for col in truncated.columns:
            a = full[col].to_numpy()[:compare_to]
            b = truncated[col].to_numpy()[:compare_to]
            if col in BOOLEAN_COLUMNS:
                np.testing.assert_array_equal(a, b, err_msg=f"{col} @ cut={cut}")
            elif col in CATEGORICAL_COLUMNS or col == "open_time":
                np.testing.assert_array_equal(a, b, err_msg=f"{col} @ cut={cut}")
            else:
                np.testing.assert_allclose(
                    a, b, rtol=1e-9, atol=1e-9, equal_nan=True,
                    err_msg=f"{col} @ cut={cut}",
                )


def test_pivot_is_not_visible_before_its_confirmation_bar():
    """A pivot at index i must be invisible to every bar < i + lookback.

    This is the specific guarantee that makes centered fractals usable at all.
    """
    lookback = 2
    n = 40
    high = np.full(n, 100.0)
    low = np.full(n, 90.0)
    # A single unambiguous swing high at index 20.
    high[20] = 130.0
    open_time = np.arange(n, dtype="int64") * 60_000
    atr = np.full(n, 5.0)

    states, _ = structure.swing_state_series(high, low, open_time, atr, lookback, 0.5)

    for t in range(20 + lookback):
        assert states[t].last_high is None or states[t].last_high.index != 20, (
            f"pivot at 20 leaked into bar {t}"
        )
    assert states[20 + lookback].last_high is not None
    assert states[20 + lookback].last_high.index == 20


def test_structure_state_never_reads_a_future_bar():
    """Mutating bars strictly after t must not change the state at t."""
    df = _ohlcv(400)
    base = compute_indicators(df, "30m")

    tampered = df.copy()
    cut = 300
    # Violently change everything after `cut`.
    tampered.loc[cut:, ["open", "high", "low", "close"]] *= 1.5
    after = compute_indicators(tampered, "30m")

    margin = structure.swing_lookback_for("30m")
    for col in _STRUCTURE_COLUMNS:
        np.testing.assert_allclose(
            base[col].to_numpy()[: cut - margin].astype("float64"),
            after[col].to_numpy()[: cut - margin].astype("float64"),
            rtol=1e-9,
            atol=1e-9,
            equal_nan=True,
            err_msg=f"{col} changed when only future bars were altered",
        )


def test_volatility_percentile_uses_only_trailing_window():
    df = _ohlcv(500)
    base = compute_indicators(df, "30m")
    tampered = df.copy()
    tampered.loc[400:, ["high", "low", "close"]] *= 3.0
    after = compute_indicators(tampered, "30m")
    np.testing.assert_allclose(
        base["volatility_percentile"].to_numpy()[:400],
        after["volatility_percentile"].to_numpy()[:400],
        rtol=1e-9,
        equal_nan=True,
    )
    np.testing.assert_array_equal(
        base["volatility_band"].to_numpy()[:400],
        after["volatility_band"].to_numpy()[:400],
    )


def test_ut_signals_are_stable_under_truncation():
    """UT cross flags are the primary entry trigger; they must never move."""
    df = _ohlcv(700)
    full = compute_indicators(df, "30m")
    for cut in (400, 550, 690):
        part = compute_indicators(df.iloc[:cut].reset_index(drop=True), "30m")
        np.testing.assert_array_equal(
            full["ut_buy"].to_numpy()[:cut], part["ut_buy"].to_numpy()
        )
        np.testing.assert_array_equal(
            full["ut_sell"].to_numpy()[:cut], part["ut_sell"].to_numpy()
        )


def test_bb_width_percentile_is_causal():
    df = _ohlcv(500)
    base = compute_indicators(df, "30m")
    tampered = df.copy()
    tampered.loc[350:, "close"] *= 1.2
    after = compute_indicators(tampered, "30m")
    np.testing.assert_allclose(
        base["bb_width_percentile"].to_numpy()[:350],
        after["bb_width_percentile"].to_numpy()[:350],
        rtol=1e-9,
        equal_nan=True,
    )


def test_warmup_region_is_declared_not_silently_used():
    """Everything recursive must still be NaN inside its documented warm-up."""
    df = _ohlcv(200)
    out = compute_indicators(df, "30m")
    cfg = IndicatorConfig()
    assert out["atr_14"].isna().to_numpy()[: cfg.atr_period - 1].all()
    assert out["rsi_14"].isna().to_numpy()[: cfg.rsi_period].all()
    assert out["adx_14"].isna().to_numpy()[: 2 * cfg.adx_period - 2].all()
    assert out["ema_28"].isna().to_numpy()[: cfg.ema_slow - 1].all()
    assert out["ut_level"].isna().to_numpy()[: cfg.ut_atr_period - 1].all()
