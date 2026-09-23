"""Signal 6 retraining pipeline — implements SIGNAL6_RETRAIN_SPEC.md.

The spec's rule: the production rules (entry/SL/TP/label) are FIXED as the
baseline; retraining adds REGIME features and validates walk-forward,
cross-year and cross-feed. Nothing here touches `engine/signal6.py` — this
module only builds samples, fits P(win) models and prints the spec §25
report. Deployment is a separate decision behind the spec §30 gates.

## Sample population

Every `break_*` event the session map produces, tradable or not — the
model's job is to learn WHEN the sweep→break sequence pays, so the
baseline filter's ingredients (prior_sweep, vol_ok, hour window, range
floor) ride along as FEATURES, and Model 0 applies them as the fixed
baseline to prove the pipeline reproduces the shipped numbers.

## No-lookahead contract

Every feature is computed as-of the event's confirmation bar close: HTF
values are last-Closed-bar joins, percentiles are trailing-window ranks,
and the label fields (win/mfe/mae/realized_R/minutes_to_outcome) live in
their own sub-dict so a model matrix can never see them by construction
(whitelisted feature keys, spec §13).

## Macro features

`SIGNAL6_RETRAIN_SPEC.md` §10 asks for CPI/PPI/NFP/FOMC fields. This
project has NO economic calendar (the brief's ABSENT note) — those fields
are deliberately absent and Model 3 is skipped, stated in the report
rather than fabricated (a made-up calendar would be worse than none).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .session_map import DAY_MS, build_map, myt_day_ms
from .signal6 import Signal6Config, tradable_events
from ..data.db import CandleRepository, Database
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators
from ..tools.validate_rule import walk_forward

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))
HORIZON_MS = DAY_MS                    # spec §2.1: 24h
COST_BPS = 0.85e-4                     # spec §2.1: 0.85 bps

# The spec §23 minimal feature set, minus macro (absent in this project),
# plus the Model-2 additions (Q3 spec §3): regime-change and quality-velocity
# features. All computed as-of the confirmation bar.
FEATURE_KEYS = (
    # regime
    "adx_1h", "atr_5m_pct", "atr_1h_pct", "vol_percentile_20d",
    "trend_slope",
    # regime CHANGE (Model 2 §3)
    "adx_slope", "ema_slope_change", "atr_pctile_change",
    "vol_change_ratio",
    # asia
    "asia_range_pct", "asia_range_atr", "asia_range_percentile_20d",
    "asia_direction",
    # sweep
    "sweep_exists", "sweep_side", "sweep_depth_R", "sweep_depth_ATR",
    "sweep_reclaim_speed", "bars_sweep_to_break",
    # break
    "break_distance_R", "break_distance_ATR", "break_volume_ratio",
    "break_volume_percentile", "break_body_R", "break_velocity",
    # session
    "confirm_hour_myt", "minutes_from_europe_open", "minutes_to_us_open",
    # liquidity
    "dist_prev_day_high_R", "dist_prev_day_low_R",
    "dist_prev_us_high_R", "dist_prev_us_low_R",
    # LEG STATE (year-gap analysis 2026-09-20): the regime separation the
    # gates exploit existed only in strong trending LEGS — these features
    # name that state directly, as-of the confirmation bar.
    "dist_20d_high", "dist_20d_low", "ret_20d", "slope_200_1h",
    # baseline-filter ingredients as features (Model 0's conditions)
    "prior_sweep", "vol_ok", "in_window", "min_range_ok",
)

# Experiment ladders (Q3 spec §"Model 2 实验顺序"): A adds regime-change,
# B adds sweep/break quality, C adds liquidity, D is C + regime thresholds.
EXP_A_EXTRA = ("adx_slope", "ema_slope_change", "atr_pctile_change",
               "vol_change_ratio", "asia_range_atr", "vol_percentile_20d")
EXP_B_EXTRA = EXP_A_EXTRA + ("sweep_depth_R", "sweep_depth_ATR",
                             "sweep_reclaim_speed", "break_volume_ratio",
                             "break_velocity")
EXP_C_EXTRA = EXP_B_EXTRA + ("dist_prev_day_high_R", "dist_prev_day_low_R",
                             "dist_prev_us_high_R", "dist_prev_us_low_R")
EXP_LEG = ("dist_20d_high", "dist_20d_low", "ret_20d", "slope_200_1h")


def _fold(df: pd.DataFrame, step_ms: int) -> pd.DataFrame:
    """Exact OHLCV fold of the path frame onto a coarser grid."""
    key = ((df["open_time"] // step_ms) * step_ms).astype("int64")
    return df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)


def _atr_series(frame: pd.DataFrame) -> np.ndarray:
    """Wilder ATR(14) over a folded frame, matching the registry's atr_14."""
    h = frame["high"].to_numpy(dtype="float64")
    l = frame["low"].to_numpy(dtype="float64")
    c = frame["close"].to_numpy(dtype="float64")
    prev = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    atr = np.full_like(tr, np.nan)
    if len(tr) < 15:
        return atr
    atr[13] = tr[:14].mean()
    for i in range(14, len(tr)):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14.0
    return atr


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    a = 2.0 / (n + 1.0)
    seen = False
    for i, v in enumerate(x):
        if not np.isfinite(v):
            continue
        out[i] = v if not seen else out[i - 1] + a * (v - out[i - 1])
        seen = True
    return out


def _trailing_rank(series: np.ndarray, i: int, window: int) -> float:
    """Percentile rank of series[i] within its own trailing window."""
    if i < 1 or not np.isfinite(series[i]):
        return np.nan
    w = series[max(0, i - window):i]
    w = w[np.isfinite(w)]
    if not len(w):
        return np.nan
    return float(np.count_nonzero(w <= series[i])) / len(w)


class _FrameCtx:
    """Precomputed series over a path frame — the ONE definition of every
    array both training and live feature computation read. Wilder/EMA
    recursions converge, so a 60-day tail yields the same values as full
    history to float noise."""

    __slots__ = ("t5", "t1h", "h1h", "l1h", "atr5", "atr1h", "adx1h",
                 "ema14_1h", "c1h", "v5", "c5", "o5")

    def __init__(self, f5: pd.DataFrame, f1h: pd.DataFrame,
                 cfg: IndicatorConfig) -> None:
        self.t5 = f5["open_time"].to_numpy(dtype="int64")
        self.t1h = f1h["open_time"].to_numpy(dtype="int64")
        self.h1h = f1h["high"].to_numpy(dtype="float64")
        self.l1h = f1h["low"].to_numpy(dtype="float64")
        self.atr5 = _atr_series(f5)
        self.atr1h = _atr_series(f1h)
        self.adx1h = compute_indicators(f1h, "1h", cfg)["adx_14"].to_numpy(
            dtype="float64")
        self.ema14_1h = _ema(f1h["close"].to_numpy(dtype="float64"), 14)
        self.c1h = f1h["close"].to_numpy(dtype="float64")
        self.v5 = f5["volume"].to_numpy(dtype="float64")
        self.c5 = f5["close"].to_numpy(dtype="float64")
        self.o5 = f5["open"].to_numpy(dtype="float64")


def features_for_break(ctx: _FrameCtx, ev: dict, lv: dict, r: float,
                       rp: float, day0: int, dir_ref_open: float,
                       day_events: list):
    """Every feature for one break event, as-of its confirmation bar.

    THE shared definition: `build_samples` (training history) and Signal 7's
    live evaluation both call this, so a feature can never mean something
    different at runtime than it meant in training. Returns
    ``(features, aux)`` or None when the event lacks the history the
    features need. ``aux`` carries the bracket anchors (level/side) so the
    caller can build its own entry price.

    Note: ``asia_direction`` is measured against ``dir_ref_open`` — the
    open of the first bar of the PREVIOUS day in the historical builder
    (so effectively a ~24h drift sign). Kept exactly as measured; the name
    is aspirational, the number is what the models were trained on.
    """
    kind = str(ev.get("type") or "")
    side = kind.removeprefix("break_")
    confirm = int(ev.get("confirm_ms") or ev.get("ts_ms") or 0)
    close_ms = confirm + 300_000
    i5 = int(np.searchsorted(ctx.t5, close_ms, "right")) - 1
    i1 = int(np.searchsorted(ctx.t1h + 3_600_000, close_ms, "right")) - 1
    if i5 < 20 or i1 < 20:
        return None
    hi, lo = lv.get("asia_high"), lv.get("asia_low")
    a1 = ctx.atr1h[i1] if np.isfinite(ctx.atr1h[i1]) else r / 20.0
    a5 = ctx.atr5[i5] if np.isfinite(ctx.atr5[i5]) else a1 / 6.0
    price = ctx.c5[i5]

    sweep = None
    for e2 in day_events:
        if e2.get("type") == f"sweep_{side}" and \
                int(e2.get("ts_ms") or 0) <= int(ev.get("ts_ms") or 0):
            sweep = e2
    level = float(hi if side == "high" else lo)
    base = float((price - level) * (1 if side == "high" else -1))

    brk_i = int(np.searchsorted(ctx.t5, confirm, "right")) - 1
    run_first = max(brk_i - 1, 1)
    med2 = float(np.median(ctx.v5[run_first:run_first + 2]))
    med20 = float(np.median(ctx.v5[max(0, run_first - 20):run_first]))
    vol_ratio = med2 / med20 if med20 > 0 else np.nan

    adx_now = float(ctx.adx1h[i1]) if np.isfinite(ctx.adx1h[i1]) else 0.0
    adx_then = float(ctx.adx1h[i1 - 6]) \
        if np.isfinite(ctx.adx1h[i1 - 6]) else adx_now
    atr_pct = _trailing_rank(
        ctx.atr1h / np.where(ctx.c1h > 0, ctx.c1h, np.nan), i1, 480)
    atr_pct_prev = _trailing_rank(
        ctx.atr1h / np.where(ctx.c1h > 0, ctx.c1h, np.nan), i1 - 24, 480)
    slope_now = (ctx.ema14_1h[i1] - ctx.ema14_1h[i1 - 6]) / a1 \
        if np.isfinite(ctx.ema14_1h[i1 - 6]) else 0.0
    slope_then = (ctx.ema14_1h[i1 - 6] - ctx.ema14_1h[i1 - 12]) / a1 \
        if np.isfinite(ctx.ema14_1h[i1 - 12]) else slope_now
    atr_pct_now = ctx.atr1h[i1] / price
    atr_pct_then = ctx.atr1h[i1 - 24] / ctx.c1h[i1 - 24] \
        if np.isfinite(ctx.atr1h[i1 - 24]) and ctx.c1h[i1 - 24] > 0 \
        else atr_pct_now

    i_lo = max(0, i1 - 479)
    leg_hi = float(ctx.h1h[i_lo:i1 + 1].max())
    leg_lo = float(ctx.l1h[i_lo:i1 + 1].min())

    feat = {
        "adx_1h": adx_now,
        "atr_5m_pct": a5 / price,
        "atr_1h_pct": a1 / price,
        "vol_percentile_20d": atr_pct,
        "trend_slope": slope_now,
        "adx_slope": adx_now - adx_then,
        "ema_slope_change": slope_now - slope_then,
        "atr_pctile_change":
            (atr_pct - atr_pct_prev
             if np.isfinite(atr_pct) and np.isfinite(atr_pct_prev) else 0.0),
        "vol_change_ratio":
            (atr_pct_now / atr_pct_then if atr_pct_then > 0 else 1.0),
        "asia_range_pct": r / price,
        "asia_range_atr": r / a1 if a1 > 0 else np.nan,
        "asia_range_percentile_20d": rp,
        "asia_direction": float(np.sign(ctx.c5[i5] - dir_ref_open)),
        "sweep_exists": 1.0 if sweep else 0.0,
        "sweep_side": (1.0 if side == "high" else -1.0),
        "sweep_depth_R": (abs(float(sweep["price"]) - level) / r
                          if sweep else 0.0),
        "sweep_depth_ATR": (abs(float(sweep["price"]) - level) / a1
                            if sweep else 0.0),
        "sweep_reclaim_speed": (
            (int(sweep.get("confirm_ms") or sweep["ts_ms"])
             - int(sweep["ts_ms"])) / 300_000 if sweep else 0.0),
        "bars_sweep_to_break": (
            (confirm - int(sweep["ts_ms"])) / 300_000 if sweep else 999.0),
        "break_distance_R": base / r,
        "break_distance_ATR": base / a1 if a1 > 0 else np.nan,
        "break_volume_ratio": vol_ratio,
        "break_volume_percentile": _trailing_rank(
            ctx.v5 / np.where(
                np.median(ctx.v5[max(0, i5 - 96):i5]) > 0,
                np.median(ctx.v5[max(0, i5 - 96):i5]), np.nan), i5, 96),
        "break_body_R": abs(ctx.c5[brk_i] - ctx.o5[brk_i]) / r,
        "break_velocity":
            base / r / max(1.0, (confirm - int(sweep["ts_ms"]))
                           / 300_000) if sweep else 0.0,
        "confirm_hour_myt": ((confirm // 3_600_000) + 8) % 24,
        "minutes_from_europe_open": (close_ms - (day0 + 15 * 3_600_000))
                                    / 60_000,
        "minutes_to_us_open": ((day0 + 20 * 3_600_000) - close_ms) / 60_000,
        "dist_prev_day_high_R":
            (price - float(lv.get("prev_day_high") or price)) / r,
        "dist_prev_day_low_R":
            (price - float(lv.get("prev_day_low") or price)) / r,
        "dist_prev_us_high_R":
            (price - float(lv.get("prev_us_high") or price)) / r,
        "dist_prev_us_low_R":
            (price - float(lv.get("prev_us_low") or price)) / r,
        "dist_20d_high": (price - leg_hi) / price,
        "dist_20d_low": (price - leg_lo) / price,
        "ret_20d": (price / ctx.c1h[i_lo] - 1.0) if ctx.c1h[i_lo] > 0 else 0.0,
        "slope_200_1h": (ctx.c1h[i1] - ctx.c1h[max(0, i1 - 200)]) / a1
        if a1 > 0 else 0.0,
        "prior_sweep": 1.0 if sweep else 0.0,
        "vol_ok": 1.0 if vol_ratio > 1.2 else 0.0,
        "in_window": 1.0 if 15 <= ((confirm // 3_600_000) + 8) % 24 < 22
        else 0.0,
        "min_range_ok": 1.0 if r >= 0.001 * price else 0.0,
    }
    aux = {"confirm": confirm, "close_ms": close_ms, "i5": i5, "i1": i1,
           "price": price, "r": r, "level": level, "side": side,
           "is_long": side == "high"}
    return feat, aux


def build_samples(db: Database, symbol: str,
                  ind_cfg: IndicatorConfig | None = None,
                  data_interval: str = "1m",
                  sig_cfg: Signal6Config | None = None) -> list[dict]:
    """One row per break event: features as-of confirmation + labels.

    Labels (spec §14): production `win` (TP touched AND net>0; SL/24h/tie
    are losses) plus the research labels MFE/MAE in R, realized_R and
    minutes_to_outcome.
    """
    cfg = ind_cfg or IndicatorConfig()
    s6 = sig_cfg or Signal6Config()
    repo = CandleRepository(db)
    path = repo.load(symbol, data_interval).reset_index(drop=True)
    if path.empty:
        return []

    f5 = _fold(path, 300_000)
    f1h = _fold(path, 3_600_000)
    # The ONE frame context: training history and Signal 7's live path
    # share every series through it, so features cannot drift.
    ctx = _FrameCtx(f5, f1h, cfg)
    pt = path["open_time"].to_numpy(dtype="int64")
    ph = path["high"].to_numpy(dtype="float64")
    pl = path["low"].to_numpy(dtype="float64")
    pc = path["close"].to_numpy(dtype="float64")
    po = path["open"].to_numpy(dtype="float64")

    samples: list[dict] = []
    range_hist: list[float] = []           # trailing Asia ranges (percentile)
    now_ms = int(pt[-1]) + (3_600_000 if data_interval != "1m" else 60_000)
    day = myt_day_ms(int(pt[0]))
    last_range_day = None

    while day <= now_ms:
        sub = path[(pt >= day - DAY_MS) & (pt <= day + 2 * DAY_MS)]
        if not sub.empty:
            block = build_map(sub, as_of_ms=min(day + DAY_MS - 60_000, now_ms))
            block["symbol"] = symbol
            lv = block.get("levels") or {}
            hi, lo = lv.get("asia_high"), lv.get("asia_low")
            if hi is not None and lo is not None:
                r = float(hi) - float(lo)
                # trailing (exclusive of today) range percentile
                rp = (float(np.count_nonzero(
                    np.array(range_hist[-20:]) < r)) / len(range_hist[-20:])
                    if range_hist else np.nan)
                if last_range_day != day:
                    range_hist.append(r)
                    last_range_day = day

                for ev in block.get("events") or []:
                    kind = str(ev.get("type") or "")
                    if not kind.startswith("break_"):
                        continue
                    out = features_for_break(
                        ctx, ev, lv, r, rp, day,
                        float(sub["open"].iloc[0]),
                        block.get("events") or [])
                    if out is None:
                        continue
                    feat, aux = out
                    level, r2 = aux["level"], aux["r"]
                    is_long = aux["is_long"]

                    # baseline entry + brackets (FIXED, spec §2.1)
                    entry_t = aux["close_ms"]
                    j = int(np.searchsorted(pt, entry_t))
                    if j >= pt.size or int(pt[j]) - entry_t > 300_000:
                        continue
                    entry = float(po[j])
                    sl = level - 0.50 * r2 if is_long else level + 0.50 * r2
                    tp = level + 1.00 * r2 if is_long else level - 1.00 * r2

                    reason, px, xt, _amb = walk_forward(
                        pt, ph, pl, pc, entry_t, entry, tp, sl, is_long,
                        HORIZON_MS)
                    if reason == "nodata":
                        continue
                    resolved = reason in ("tp", "sl") or \
                        int(pt[-1]) >= entry_t + HORIZON_MS
                    seg = (pt >= entry_t) & (pt < (xt if resolved
                                                    else entry_t + HORIZON_MS))
                    if is_long:
                        mfe = (float(ph[seg].max() - entry)
                               if seg.any() else 0.0) / r2
                        mae = (float(entry - pl[seg].min())
                               if seg.any() else 0.0) / r2
                    else:
                        mfe = (float(entry - pl[seg].min())
                               if seg.any() else 0.0) / r2
                        mae = (float(ph[seg].max() - entry)
                               if seg.any() else 0.0) / r2
                    net = ((float(px) - entry) if is_long
                           else (entry - float(px))) \
                        - entry * COST_BPS if resolved else 0.0

                    f: dict[str, Any] = {
                        "symbol": symbol,
                        "day_myt": datetime.fromtimestamp(
                            day / 1000, MYT).strftime("%Y-%m-%d"),
                        "entry_time": entry_t,
                        "entry_price": round(entry, 4),
                        "sl": round(sl, 4),
                        "tp": round(tp, 4),
                        "event_type": kind,
                        "side": 1 if is_long else -1,
                        **feat,
                        # --- labels (spec §14) ---------------------------
                        "label": {
                            "resolved": bool(resolved),
                            "reason": reason if resolved else "open",
                            "win": bool(resolved and net > 0),
                            "net": round(net, 4),
                            "exit_price": round(float(px), 4)
                            if resolved else None,
                            "realized_R": round(net / r2, 4),
                            "mfe_R": round(mfe, 4),
                            "mae_R": round(mae, 4),
                            "minutes_to_outcome":
                                round(((xt if resolved
                                        else entry_t + HORIZON_MS)
                                       - entry_t) / 60_000, 1),
                        },
                    }
                    samples.append(f)
        day += DAY_MS
    samples.sort(key=lambda s: s["entry_time"])
    return samples


def model0_baseline(samples: list[dict]) -> list[dict]:
    """Model 0 (spec §24): the shipped rules as a fixed filter."""
    return [s for s in samples
            if s["prior_sweep"] and s["vol_ok"] and s["in_window"]
            and s["min_range_ok"]
            and datetime.fromtimestamp(s["entry_time"] / 1000, MYT).weekday()
            < 5]


def _stats(rows: list[dict]) -> dict:
    lab = [r["label"] for r in rows if r["label"]["resolved"]]
    n = len(lab)
    wins = sum(1 for l in lab if l["win"])
    nets = [l["net"] for l in lab]
    gross_w = sum(x for x in nets if x > 0)
    gross_l = -sum(x for x in nets if x < 0)
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win_pct": round(100 * wins / n, 1) if n else None,
            "expectancy": round(sum(nets) / n, 3) if n else None,
            "pf": round(gross_w / gross_l, 2) if gross_l > 0 else
            (float("inf") if gross_w > 0 else None),
            "net": round(sum(nets), 2), "maxdd": round(dd, 2)}


class LogisticPwin:
    """Ridge logistic regression in numpy — P(win), never BUY/SELL (§19)."""

    def __init__(self, l2: float = 1e-2) -> None:
        self.l2 = l2
        self.mu = self.sd = None
        self.w = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticPwin":
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu) / self.sd])
        w = np.zeros(Z.shape[1])
        for _ in range(200):
            p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w, -30, 30)))
            g = Z.T @ (p - y) / len(y) + self.l2 * w
            Wm = np.diag(p * (1 - p)) + 1e-9
            H = (Z.T @ Wm @ Z) / len(y) + self.l2 * np.eye(Z.shape[1])
            try:
                step = np.linalg.solve(H, g)
            except np.linalg.LinAlgError:
                break
            w -= step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.w = w
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu) / self.sd])
        return 1.0 / (1.0 + np.exp(-np.clip(Z @ self.w, -30, 30)))


def matrix(samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """The feature matrix + win labels — whitelist keeps labels out (§13)."""
    X = np.array([[float(s.get(k) if s.get(k) is not None else 0.0)
                   for k in FEATURE_KEYS] for s in samples],
                 dtype="float64")
    X[~np.isfinite(X)] = 0.0
    y = np.array([1.0 if s["label"]["win"] else 0.0 for s in samples])
    return X, y
