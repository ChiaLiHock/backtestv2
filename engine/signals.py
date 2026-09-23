"""Signal evaluation — `signal_id` -> boolean array aligned with the bar index.

Every signal here is one the app can already express; the catalogue and the
`file:line` for each condition live in `docs/SIGNAL_MAP.md`. Nothing is invented
except the handful explicitly marked BACKTEST-ADDED, which are edge detectors over
states the app latches rather than edges.

All inputs are CONFIRMED (closed-bar) values. See `docs/OPEN_QUESTIONS.md` Q28 for
the LIVE->CONFIRMED substitution and what it costs.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from ..indicators import structure as st
from ..indicators import trend as T
from ..indicators.regime import (
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    VOL_EXTREME,
    VOL_HIGH,
    VOL_LOW,
    VOL_NORMAL,
)


class SignalContext:
    """Everything a signal function may read.

    ``thresholds`` carries the config's ATR-distance constants so a strategy can
    widen "on the line" without editing code.
    """

    __slots__ = ("ind", "close", "high", "low", "open_time", "thresholds", "htf")

    def __init__(
        self,
        ind: pd.DataFrame,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        open_time: np.ndarray,
        thresholds: dict[str, float],
        htf: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self.ind = ind
        self.close = close
        self.high = high
        self.low = low
        self.open_time = open_time
        self.thresholds = thresholds
        self.htf = htf or {}

    def col(self, name: str) -> np.ndarray:
        return self.ind[name].to_numpy()


SignalFn = Callable[[SignalContext], np.ndarray]
REGISTRY: dict[str, SignalFn] = {}


def signal(name: str) -> Callable[[SignalFn], SignalFn]:
    def wrap(fn: SignalFn) -> SignalFn:
        REGISTRY[name] = fn
        return fn
    return wrap


def _rising_edge(state: np.ndarray) -> np.ndarray:
    """True on the bar a latched state becomes true. BACKTEST-ADDED.

    The app latches BOS/CHoCH/setup states and works around the repetition with
    alert cooldowns rather than edge detection (`AlertEngine.kt:269-273`). A
    backtest needs the edge.
    """
    state = np.asarray(state, dtype=bool)
    out = np.zeros(state.size, dtype=bool)
    if state.size:
        out[0] = state[0]
        out[1:] = state[1:] & ~state[:-1]
    return out


# ---------------------------------------------------------------------------
# A. UT Dynamic Level
# ---------------------------------------------------------------------------

@signal("ut_cross_up")
def _ut_cross_up(c: SignalContext) -> np.ndarray:
    return c.col("ut_buy").astype(bool)


@signal("ut_cross_down")
def _ut_cross_down(c: SignalContext) -> np.ndarray:
    return c.col("ut_sell").astype(bool)


@signal("ut_bias_bullish")
def _ut_bias_bullish(c: SignalContext) -> np.ndarray:
    return c.col("ut_bias") == T.BIAS_BULLISH


@signal("ut_bias_bearish")
def _ut_bias_bearish(c: SignalContext) -> np.ndarray:
    return c.col("ut_bias") == T.BIAS_BEARISH


@signal("ut_position_long")
def _ut_position_long(c: SignalContext) -> np.ndarray:
    return c.col("ut_position") == 1


@signal("ut_position_short")
def _ut_position_short(c: SignalContext) -> np.ndarray:
    return c.col("ut_position") == -1


@signal("ut_level_near")
def _ut_level_near(c: SignalContext) -> np.ndarray:
    """Price is sitting on the UT Dynamic Level, within UT_NEAR_ATR of it.

    `AlertEngine.kt:182` — ``abs(close - level) / atr > UT_NEAR_ATR`` rejects,
    so this is the inclusive complement. Default 0.6 (`AlertEngine.kt:47`).
    """
    atr = c.col("atr_14")
    ut = c.col("ut_level")
    near = c.thresholds.get("ut_near_atr", 0.6)
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = np.abs(c.close - ut) / atr
    return np.nan_to_num(dist, nan=np.inf) <= near


# ---------------------------------------------------------------------------
# D. EMA stack
# ---------------------------------------------------------------------------

@signal("ema_stack_bullish")
def _ema_stack_bullish(c: SignalContext) -> np.ndarray:
    return c.col("ema_alignment") == T.ALIGN_BULLISH


@signal("ema_stack_bearish")
def _ema_stack_bearish(c: SignalContext) -> np.ndarray:
    return c.col("ema_alignment") == T.ALIGN_BEARISH


@signal("ema_stack_mixed")
def _ema_stack_mixed(c: SignalContext) -> np.ndarray:
    return c.col("ema_alignment") == T.ALIGN_MIXED


@signal("ema_stack_flip_bullish")
def _ema_flip_bull(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("ema_alignment") == T.ALIGN_BULLISH)


@signal("ema_stack_flip_bearish")
def _ema_flip_bear(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("ema_alignment") == T.ALIGN_BEARISH)


@signal("price_above_all_emas")
def _price_above_all_emas(c: SignalContext) -> np.ndarray:
    """⚠️ Measured to fire ~once in 166 days when combined with ut_level_near —
    a pullback deep enough to touch the UT line is essentially never still above
    EMA7. Use `ema_stack_bullish` for "trend intact" instead."""
    return (
        (c.close > c.col("ema_7"))
        & (c.close > c.col("ema_14"))
        & (c.close > c.col("ema_28"))
    )


@signal("price_below_all_emas")
def _price_below_all_emas(c: SignalContext) -> np.ndarray:
    return (
        (c.close < c.col("ema_7"))
        & (c.close < c.col("ema_14"))
        & (c.close < c.col("ema_28"))
    )


@signal("ema_spread_expanding")
def _spread_expanding(c: SignalContext) -> np.ndarray:
    return c.col("ema_spread_trend") == T.SPREAD_EXPANDING


@signal("ema_spread_compressing")
def _spread_compressing(c: SignalContext) -> np.ndarray:
    return c.col("ema_spread_trend") == T.SPREAD_COMPRESSING


@signal("price_extended_above_ema28")
def _ext_above(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("extension_atr"), nan=0.0) > 1.5


@signal("price_extended_below_ema28")
def _ext_below(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("extension_atr"), nan=0.0) < -1.5


@signal("price_near_mean")
def _near_mean(c: SignalContext) -> np.ndarray:
    return np.abs(np.nan_to_num(c.col("extension_atr"), nan=np.inf)) <= 0.75


@signal("dynamic_level_touch")
def _dynamic_touch(c: SignalContext) -> np.ndarray:
    """Price retraced onto EMA14 / EMA28 / VWAP. `AlertEngine.kt:234-238`."""
    atr = c.col("atr_14")
    near = c.thresholds.get("dynamic_near_atr", 0.5)
    hit = np.zeros(c.close.size, dtype=bool)
    for name in ("ema_14", "ema_28", "vwap"):
        v = c.col(name)
        with np.errstate(invalid="ignore", divide="ignore"):
            d = np.abs(c.close - v) / atr
        hit |= np.nan_to_num(d, nan=np.inf) <= near
    return hit


# ---------------------------------------------------------------------------
# E. Bollinger
# ---------------------------------------------------------------------------

def _pb(c: SignalContext) -> np.ndarray:
    return c.col("bb_percent_b")


@signal("bb_at_upper_band")
def _bb_upper(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(_pb(c), nan=-np.inf) > 0.95


@signal("bb_upper_band_area")
def _bb_upper_area(c: SignalContext) -> np.ndarray:
    v = _pb(c)
    return (np.nan_to_num(v, nan=-np.inf) > 0.85) & (np.nan_to_num(v, nan=np.inf) <= 0.95)


@signal("bb_at_lower_band")
def _bb_lower(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(_pb(c), nan=np.inf) < 0.05


@signal("bb_lower_band_area")
def _bb_lower_area(c: SignalContext) -> np.ndarray:
    v = _pb(c)
    return (np.nan_to_num(v, nan=np.inf) < 0.15) & (np.nan_to_num(v, nan=-np.inf) >= 0.05)


@signal("bb_squeeze")
def _bb_squeeze(c: SignalContext) -> np.ndarray:
    """BACKTEST-ADDED — the app computes bb_width_percentile and reads it nowhere."""
    return np.nan_to_num(c.col("bb_width_percentile"), nan=np.inf) < 0.25


@signal("bb_expansion")
def _bb_expansion(c: SignalContext) -> np.ndarray:
    """BACKTEST-ADDED, as above."""
    return np.nan_to_num(c.col("bb_width_percentile"), nan=-np.inf) > 0.75


# ---------------------------------------------------------------------------
# F. Momentum
# ---------------------------------------------------------------------------

@signal("rsi_above_70")
def _rsi_70(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("rsi_14"), nan=-np.inf) > 70


@signal("rsi_above_50")
def _rsi_50(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("rsi_14"), nan=-np.inf) > 50


@signal("rsi_below_50")
def _rsi_b50(c: SignalContext) -> np.ndarray:
    v = c.col("rsi_14")
    return ~np.isnan(v) & (v <= 50)


@signal("rsi_below_30")
def _rsi_30(c: SignalContext) -> np.ndarray:
    v = c.col("rsi_14")
    return ~np.isnan(v) & (v <= 30)


@signal("macd_above_signal")
def _macd_above(c: SignalContext) -> np.ndarray:
    """⚠️ BACKTEST-ADDED — MACD does not exist in the app, so these are outside
    the confirmed parity."""
    return c.col("macd") > c.col("macd_signal")


@signal("macd_below_signal")
def _macd_below(c: SignalContext) -> np.ndarray:
    return c.col("macd") < c.col("macd_signal")


@signal("macd_cross_up")
def _macd_cross_up(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("macd") > c.col("macd_signal"))


@signal("macd_cross_down")
def _macd_cross_down(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("macd") < c.col("macd_signal"))


@signal("macd_hist_rising")
def _macd_hist_rising(c: SignalContext) -> np.ndarray:
    h = c.col("macd_hist")
    out = np.zeros(h.size, dtype=bool)
    out[1:] = h[1:] > h[:-1]
    return out & ~np.isnan(h)


@signal("macd_above_zero")
def _macd_above_zero(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("macd"), nan=-np.inf) > 0.0


@signal("macd_below_zero")
def _macd_below_zero(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("macd"), nan=np.inf) < 0.0


@signal("price_above_vwap")
def _above_vwap(c: SignalContext) -> np.ndarray:
    v = c.col("vwap")
    return ~np.isnan(v) & (c.close > v)


@signal("price_below_vwap")
def _below_vwap(c: SignalContext) -> np.ndarray:
    v = c.col("vwap")
    return ~np.isnan(v) & (c.close < v)


@signal("adx_trending")
def _adx_trending(c: SignalContext) -> np.ndarray:
    """The only ADX cut point that changes a label. `MarketRegimeEngine.kt:36`."""
    return np.nan_to_num(c.col("adx_14"), nan=0.0) >= 20.0


@signal("adx_very_weak")
def _adx_weak(c: SignalContext) -> np.ndarray:
    v = c.col("adx_14")
    return ~np.isnan(v) & (v < 15)


@signal("adx_meaningful")
def _adx_meaningful(c: SignalContext) -> np.ndarray:
    v = np.nan_to_num(c.col("adx_14"), nan=0.0)
    return (v >= 25) & (v < 40)


@signal("adx_strong")
def _adx_strong(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("adx_14"), nan=0.0) >= 40


@signal("di_bullish")
def _di_bull(c: SignalContext) -> np.ndarray:
    return c.col("adx_direction") == 1


@signal("di_bearish")
def _di_bear(c: SignalContext) -> np.ndarray:
    return c.col("adx_direction") == -1


# ---------------------------------------------------------------------------
# G. Structure
# ---------------------------------------------------------------------------

@signal("structure_bullish")
def _struct_bull(c: SignalContext) -> np.ndarray:
    return c.col("structure_state") == st.STRUCTURE_BULLISH


@signal("structure_bearish")
def _struct_bear(c: SignalContext) -> np.ndarray:
    return c.col("structure_state") == st.STRUCTURE_BEARISH


@signal("structure_range")
def _struct_range(c: SignalContext) -> np.ndarray:
    return c.col("structure_state") == st.STRUCTURE_RANGE


@signal("bos_bullish")
def _bos_bull(c: SignalContext) -> np.ndarray:
    return c.col("bos") == st.BIAS_BULLISH


@signal("bos_bearish")
def _bos_bear(c: SignalContext) -> np.ndarray:
    return c.col("bos") == st.BIAS_BEARISH


@signal("choch_bullish")
def _choch_bull(c: SignalContext) -> np.ndarray:
    return c.col("choch") == st.BIAS_BULLISH


@signal("choch_bearish")
def _choch_bear(c: SignalContext) -> np.ndarray:
    return c.col("choch") == st.BIAS_BEARISH


@signal("bos_bullish_event")
def _bos_bull_ev(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("bos") == st.BIAS_BULLISH)


@signal("bos_bearish_event")
def _bos_bear_ev(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("bos") == st.BIAS_BEARISH)


@signal("choch_bullish_event")
def _choch_bull_ev(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("choch") == st.BIAS_BULLISH)


@signal("choch_bearish_event")
def _choch_bear_ev(c: SignalContext) -> np.ndarray:
    return _rising_edge(c.col("choch") == st.BIAS_BEARISH)


# ---------------------------------------------------------------------------
# H. Regime & volatility
# ---------------------------------------------------------------------------

@signal("volatility_low")
def _vol_low(c: SignalContext) -> np.ndarray:
    return (c.col("volatility_band") == VOL_LOW) & c.col("volatility_measured").astype(bool)


@signal("volatility_normal")
def _vol_normal(c: SignalContext) -> np.ndarray:
    return c.col("volatility_band") == VOL_NORMAL


@signal("volatility_high")
def _vol_high(c: SignalContext) -> np.ndarray:
    return (c.col("volatility_band") == VOL_HIGH) & c.col("volatility_measured").astype(bool)


@signal("volatility_extreme")
def _vol_extreme(c: SignalContext) -> np.ndarray:
    return (c.col("volatility_band") == VOL_EXTREME) & c.col("volatility_measured").astype(bool)


@signal("volatility_measured")
def _vol_measured(c: SignalContext) -> np.ndarray:
    """False while the ATR% sample is under 40 — the app reports NORMAL there and
    cannot distinguish it from a real NORMAL (`OPEN_QUESTIONS` Q7)."""
    return c.col("volatility_measured").astype(bool)


# --- volume (thresholds from ConfluenceEngine.kt:273-279) ------------------

@signal("volume_surge")
def _vol_surge(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("relative_volume"), nan=-np.inf) >= 1.5


@signal("volume_above_average")
def _vol_above(c: SignalContext) -> np.ndarray:
    return np.nan_to_num(c.col("relative_volume"), nan=-np.inf) >= 1.2


@signal("volume_normal")
def _vol_norm(c: SignalContext) -> np.ndarray:
    v = c.col("relative_volume")
    return ~np.isnan(v) & (v >= 0.8) & (v < 1.2)


@signal("volume_thin")
def _vol_thin(c: SignalContext) -> np.ndarray:
    v = c.col("relative_volume")
    return ~np.isnan(v) & (v < 0.8)


@signal("volume_dead")
def _vol_dead(c: SignalContext) -> np.ndarray:
    v = c.col("relative_volume")
    return ~np.isnan(v) & (v < 0.5)


# --- regime (MarketRegimeEngine.kt:47-68), computed on THIS timeframe ------
# NOTE: the app classifies regime on the H1 direction anchor only. Evaluated
# per-timeframe here, so on a 15m config these describe the 15m regime.

def _regime_primary(c: SignalContext) -> np.ndarray:
    from ..indicators.regime import classify_regime
    r = classify_regime(
        c.col("adx_14"), c.col("ema_alignment"), c.col("adx_direction"),
        c.col("structure_state"), c.col("volatility_band"),
    )
    return r.primary


@signal("regime_trend_up")
def _regime_up(c: SignalContext) -> np.ndarray:
    from ..indicators.regime import REGIME_TREND_UP
    return _regime_primary(c) == REGIME_TREND_UP


@signal("regime_trend_down")
def _regime_down(c: SignalContext) -> np.ndarray:
    from ..indicators.regime import REGIME_TREND_DOWN
    return _regime_primary(c) == REGIME_TREND_DOWN


@signal("regime_range_bound")
def _regime_range(c: SignalContext) -> np.ndarray:
    from ..indicators.regime import REGIME_RANGE
    return _regime_primary(c) == REGIME_RANGE


# --- sessions --- ⚠️ BACKTEST-ONLY. No asia/london/ny classifier exists in the
# app (OPEN_QUESTIONS Q26); these windows are a convention, not a port.

def _session_mask(c: SignalContext, name: str) -> np.ndarray:
    from .features import session_myt
    return np.array([session_myt(int(t)) == name for t in c.open_time])


@signal("session_asia")
def _sess_asia(c: SignalContext) -> np.ndarray:
    return _session_mask(c, "asia")


@signal("session_london")
def _sess_london(c: SignalContext) -> np.ndarray:
    return _session_mask(c, "london")


@signal("session_ny")
def _sess_ny(c: SignalContext) -> np.ndarray:
    return _session_mask(c, "ny")


@signal("market_closed")
def _mkt_closed(c: SignalContext) -> np.ndarray:
    from .features import is_gold_session
    return np.array([not is_gold_session(int(t)) for t in c.open_time])


@signal("usable")
def _usable(c: SignalContext) -> np.ndarray:
    """>= 60 closed bars, the app's own gate (`core/Candle.kt:49`)."""
    return c.col("usable").astype(bool)


# ---------------------------------------------------------------------------
# Higher-timeframe signals (resolved against as-of-closed HTF frames)
# ---------------------------------------------------------------------------

def _htf_bias(c: SignalContext, tf: str) -> np.ndarray:
    frame = c.htf.get(tf)
    if frame is None:
        return np.zeros(c.close.size, dtype="int8")
    return frame["confirmed_bias"].to_numpy()


@signal("htf_4h_bullish")
def _h4_bull(c: SignalContext) -> np.ndarray:
    return _htf_bias(c, "4h") == T.BIAS_BULLISH


@signal("htf_4h_bearish")
def _h4_bear(c: SignalContext) -> np.ndarray:
    return _htf_bias(c, "4h") == T.BIAS_BEARISH


@signal("htf_4h_not_bearish")
def _h4_not_bear(c: SignalContext) -> np.ndarray:
    return _htf_bias(c, "4h") != T.BIAS_BEARISH


@signal("htf_1h_bullish")
def _h1_bull(c: SignalContext) -> np.ndarray:
    return _htf_bias(c, "1h") == T.BIAS_BULLISH


@signal("htf_1h_bearish")
def _h1_bear(c: SignalContext) -> np.ndarray:
    return _htf_bias(c, "1h") == T.BIAS_BEARISH


def _htf_col(c: SignalContext, tf: str, col: str, default: float = 0.0) -> np.ndarray:
    frame = c.htf.get(tf)
    if frame is None or col not in frame.columns:
        return np.full(c.close.size, default)
    return frame[col].to_numpy()


# --- higher-timeframe UT state ---------------------------------------------
# The gate for a multi-timeframe pullback entry: the HIGHER timeframe says which
# way, the entry timeframe says where. These read the same as-of-closed frames as
# every other HTF signal, so a 1H value is only visible to a 15m bar once that 1H
# bar has actually closed.

@signal("htf_1h_ut_bullish")
def _h1_ut_bull(c: SignalContext) -> np.ndarray:
    """1H close is above the 1H UT Dynamic Level — "1H is in buy mode"."""
    return _htf_col(c, "1h", "ut_bias") == T.BIAS_BULLISH


@signal("htf_1h_ut_bearish")
def _h1_ut_bear(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "1h", "ut_bias") == T.BIAS_BEARISH


@signal("htf_1h_ut_cross_up")
def _h1_ut_cross_up(c: SignalContext) -> np.ndarray:
    """The most recently CLOSED 1H bar was a UT buy cross.

    Because the as-of alignment repeats the 1H row until the next 1H bar closes,
    this stays true for the whole hour that follows the cross — which is the
    intended "the 1H just flipped" window, not a single-bar spike.
    """
    return _htf_col(c, "1h", "ut_buy").astype(bool)


@signal("htf_1h_ut_cross_down")
def _h1_ut_cross_down(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "1h", "ut_sell").astype(bool)


@signal("htf_4h_ut_bullish")
def _h4_ut_bull(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "4h", "ut_bias") == T.BIAS_BULLISH


@signal("htf_4h_ut_bearish")
def _h4_ut_bear(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "4h", "ut_bias") == T.BIAS_BEARISH


@signal("htf_1h_ema_stack_bullish")
def _h1_stack_bull(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "1h", "ema_alignment") == T.ALIGN_BULLISH


@signal("htf_1h_ema_stack_bearish")
def _h1_stack_bear(c: SignalContext) -> np.ndarray:
    return _htf_col(c, "1h", "ema_alignment") == T.ALIGN_BEARISH


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_signal(name: str, ctx: SignalContext) -> np.ndarray:
    fn = REGISTRY.get(name)
    if fn is None:
        raise KeyError(
            f"unknown signal_id {name!r}. Known: {', '.join(sorted(REGISTRY))}"
        )
    return np.asarray(fn(ctx), dtype=bool)


def evaluate_expr(expr: str, ctx: SignalContext) -> np.ndarray:
    """Evaluate a simple column expression, e.g. ``"close > ema_28"``.

    Restricted namespace: indicator columns plus open/high/low/close. No builtins,
    no attribute access — this is config, not a plugin API.
    """
    env: dict[str, np.ndarray] = {c: ctx.ind[c].to_numpy() for c in ctx.ind.columns}
    env.update(close=ctx.close, high=ctx.high, low=ctx.low, open_time=ctx.open_time)
    try:
        result = eval(expr, {"__builtins__": {}}, env)  # noqa: S307 - sandboxed namespace
    except Exception as exc:  # pragma: no cover - config error path
        raise ValueError(f"cannot evaluate expr {expr!r}: {exc}") from exc
    return np.asarray(result, dtype=bool)


def evaluate_condition(node, ctx: SignalContext) -> np.ndarray:
    """Recursively resolve a Condition tree into a boolean array."""
    if node.signal is not None:
        return evaluate_signal(node.signal, ctx)
    if node.expr is not None:
        return evaluate_expr(node.expr, ctx)
    if node.all_of is not None:
        out = np.ones(ctx.close.size, dtype=bool)
        for child in node.all_of:
            out &= evaluate_condition(child, ctx)
        return out
    if node.any_of is not None:
        out = np.zeros(ctx.close.size, dtype=bool)
        for child in node.any_of:
            out |= evaluate_condition(child, ctx)
        return out
    raise ValueError("empty condition node")


def evaluate_entry(entry, ctx: SignalContext) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Return ``(fired, per_branch)``.

    ``per_branch`` labels which top-level branch was responsible, so a trade can
    record the ``signal_id`` that produced it rather than an anonymous boolean.
    """
    mask = np.ones(ctx.close.size, dtype=bool)
    branches: dict[str, np.ndarray] = {}

    if entry.all_of:
        for node in entry.all_of:
            mask &= evaluate_condition(node, ctx)

    if entry.any_of:
        any_mask = np.zeros(ctx.close.size, dtype=bool)
        for i, node in enumerate(entry.any_of):
            got = evaluate_condition(node, ctx)
            branches[_branch_label(node, i)] = got
            any_mask |= got
        mask &= any_mask
    elif entry.all_of:
        branches[_branch_label(entry.all_of[0], 0) if entry.all_of else "entry"] = mask

    return mask, branches


def _branch_label(node, index: int) -> str:
    if node.signal:
        return node.signal
    if node.expr:
        return f"expr[{index}]"
    if node.all_of:
        names = [n.signal for n in node.all_of if n.signal]
        return "+".join(names) if names else f"all_of[{index}]"
    if node.any_of:
        return f"any_of[{index}]"
    return f"branch[{index}]"


# ---------------------------------------------------------------------------
# ENTRY_RULES.md §4 — the multi-timeframe pullback trigger
# ---------------------------------------------------------------------------
# These complete the vocabulary that rule needs. The HTF UT-bias legs for 1H and
# 4H already existed above; 30m and 5m did not, nor did a 4H EMA stack, nor the
# dip/reclaim event. Added here rather than re-implemented in a measurement
# script so the validation harness and the live signal engine cannot drift —
# `docs/AUTOMATION.md` §B is explicit that the rule has exactly one definition.


def _htf_ut(tf: str, bullish: bool):
    def fn(c: SignalContext) -> np.ndarray:
        want = T.BIAS_BULLISH if bullish else T.BIAS_BEARISH
        return _htf_col(c, tf, "ut_bias") == want
    return fn


for _tf in ("5m", "15m", "30m"):
    signal(f"htf_{_tf}_ut_bullish")(_htf_ut(_tf, True))
    signal(f"htf_{_tf}_ut_bearish")(_htf_ut(_tf, False))
del _tf


@signal("htf_4h_ema_stack_bullish")
def _htf_4h_stack_bull(c: SignalContext) -> np.ndarray:
    """4H `ema7 > ema14 > ema28`. ENTRY_RULES §4.1 leg 6.

    Worth +9.0 points on XAUUSDT and +0.8 on PAXG 15m when removed (§4.3), which
    makes it one of the two load-bearing legs alongside the UT agreement.
    """
    return (_htf_col(c, "4h", "ema_alignment", T.ALIGN_UNDEFINED) == T.ALIGN_BULLISH)


@signal("htf_4h_ema_stack_bearish")
def _htf_4h_stack_bear(c: SignalContext) -> np.ndarray:
    return (_htf_col(c, "4h", "ema_alignment", T.ALIGN_UNDEFINED) == T.ALIGN_BEARISH)


def _dip_within(c: SignalContext, long: bool) -> np.ndarray:
    """Price pierced EMA-14 on any of the last N closed bars. §4.1 leg 7.

    N comes from `thresholds['dip_lookback']`, default 3. The lookback INCLUDES
    the current bar: a bar that dips and reclaims within itself is a valid setup,
    and the app's own alert layer treats it that way.
    """
    n = int(c.thresholds.get("dip_lookback", 3))
    ema = c.ind["ema_14"].to_numpy()
    pierced = (c.low < ema) if long else (c.high > ema)
    out = np.zeros(pierced.size, dtype=bool)
    for k in range(n):
        if k == 0:
            out |= pierced
        else:
            out[k:] |= pierced[:-k]
    return out


@signal("mtf_dip_long")
def _mtf_dip_long(c: SignalContext) -> np.ndarray:
    return _dip_within(c, True)


@signal("mtf_dip_short")
def _mtf_dip_short(c: SignalContext) -> np.ndarray:
    return _dip_within(c, False)


@signal("mtf_reclaim_long")
def _mtf_reclaim_long(c: SignalContext) -> np.ndarray:
    """Closes back above EMA-14 AND above the previous close. §4.1 legs 8-9.

    Both halves matter: closing above the EMA says the dip is over, closing above
    the previous bar says it is over *now* rather than having drifted there.
    """
    ema = c.ind["ema_14"].to_numpy()
    prev = np.roll(c.close, 1)
    prev[0] = np.nan
    with np.errstate(invalid="ignore"):
        return (c.close > ema) & (c.close > prev)


@signal("mtf_reclaim_short")
def _mtf_reclaim_short(c: SignalContext) -> np.ndarray:
    ema = c.ind["ema_14"].to_numpy()
    prev = np.roll(c.close, 1)
    prev[0] = np.nan
    with np.errstate(invalid="ignore"):
        return (c.close < ema) & (c.close < prev)
