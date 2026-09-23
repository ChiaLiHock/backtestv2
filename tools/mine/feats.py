"""Feature engineering for the mine — everything the app does not already compute.

EVERY function here is causal. Where a definition needs a confirmed pivot, the
confirmation delay is subtracted explicitly, so a value at bar t only ever uses
bars <= t.

That is a claim about the code, so it is machine-checked rather than asserted:
`python build.py --check-causal SYMBOL TF` truncates the input at several cut
points, recomputes, and requires every earlier value to be bit-identical.
Run it after changing anything in this file.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# UT flip timing — "how long has this timeframe been on this side, and did the
# fast timeframes flip before or after the slow ones"
# ---------------------------------------------------------------------------

def flip_age_bars(bias: np.ndarray) -> np.ndarray:
    """Bars since `bias` last changed value. NaN until the first change."""
    b = np.asarray(bias, dtype="float64")
    out = np.full(b.size, np.nan)
    last = np.nan
    since = -1
    for i in range(b.size):
        v = b[i]
        if not np.isfinite(v) or v == 0:
            if since >= 0:
                since += 1
                out[i] = since
            continue
        if not np.isfinite(last):
            last = v
            since = -1
            continue
        if v != last:
            last = v
            since = 0
        else:
            since = since + 1 if since >= 0 else -1
        if since >= 0:
            out[i] = since
    return out


# ---------------------------------------------------------------------------
# Fractal pivots, confirmed
# ---------------------------------------------------------------------------

def pivots(series: np.ndarray, left: int, right: int, high: bool = True):
    """Indices of confirmed local extrema, and the bar each is KNOWN at.

    A pivot at index p is only knowable at p + right. Returns (idx, known_at).
    """
    s = np.asarray(series, dtype="float64")
    n = s.size
    idx, known = [], []
    for p in range(left, n - right):
        w = s[p - left: p + right + 1]
        if not np.isfinite(w).all():
            continue
        c = s[p]
        if high:
            if c >= w.max() and (w[:left] < c).all() and (w[left + 1:] < c).all():
                idx.append(p); known.append(p + right)
        else:
            if c <= w.min() and (w[:left] > c).all() and (w[left + 1:] > c).all():
                idx.append(p); known.append(p + right)
    return np.array(idx, dtype="int64"), np.array(known, dtype="int64")


def divergence(price: np.ndarray, osc: np.ndarray, left: int, right: int,
               kind: str, max_age: int = 20) -> np.ndarray:
    """Boolean series: is a `kind` divergence live at bar t?

    kind:
      'reg_bear'    price higher HIGH, oscillator lower HIGH   -> reversal down
      'reg_bull'    price lower  LOW , oscillator higher LOW   -> reversal up
      'hid_bear'    price lower  HIGH, oscillator higher HIGH  -> continuation down
      'hid_bull'    price higher LOW , oscillator lower  LOW   -> continuation up

    True from the bar the second pivot is CONFIRMED until `max_age` bars later.
    """
    use_high = kind in ("reg_bear", "hid_bear")
    pi, pk = pivots(price, left, right, high=use_high)
    out = np.zeros(price.size, dtype=bool)
    if pi.size < 2:
        return out
    p = np.asarray(price, dtype="float64")
    o = np.asarray(osc, dtype="float64")
    for a, b in zip(range(pi.size - 1), range(1, pi.size)):
        i0, i1 = pi[a], pi[b]
        if not (np.isfinite(o[i0]) and np.isfinite(o[i1])):
            continue
        if kind == "reg_bear":
            ok = p[i1] > p[i0] and o[i1] < o[i0]
        elif kind == "reg_bull":
            ok = p[i1] < p[i0] and o[i1] > o[i0]
        elif kind == "hid_bear":
            ok = p[i1] < p[i0] and o[i1] > o[i0]
        else:  # hid_bull
            ok = p[i1] > p[i0] and o[i1] < o[i0]
        if ok:
            s = pk[b]
            out[s: min(s + max_age, out.size)] = True
    return out


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def pct_rank(series: np.ndarray, window: int, min_sample: int = 30) -> np.ndarray:
    """Rolling percentile rank of the CURRENT value inside the trailing window."""
    s = pd.Series(series, dtype="float64")
    return (s.rolling(window, min_periods=min_sample)
             .apply(lambda w: (w[:-1] < w[-1]).mean() if np.isfinite(w[-1]) else np.nan,
                    raw=True)
             .to_numpy())


# ---------------------------------------------------------------------------
# Candle geometry
# ---------------------------------------------------------------------------

def body_ratio(o, h, l, c) -> np.ndarray:
    rng = np.asarray(h, dtype="float64") - np.asarray(l, dtype="float64")
    body = np.abs(np.asarray(c, dtype="float64") - np.asarray(o, dtype="float64"))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(rng > 0, body / rng, np.nan)


def close_position(o, h, l, c) -> np.ndarray:
    """Where in the bar's range the close sits. 1 = at the high, 0 = at the low."""
    hi = np.asarray(h, dtype="float64"); lo = np.asarray(l, dtype="float64")
    rng = hi - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(rng > 0, (np.asarray(c, dtype="float64") - lo) / rng, np.nan)


def slope(series: np.ndarray, n: int) -> np.ndarray:
    s = pd.Series(series, dtype="float64")
    return (s - s.shift(n)).to_numpy()


def rolling_min(series: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(series, dtype="float64").rolling(n, min_periods=1).min().to_numpy()


def rolling_max(series: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(series, dtype="float64").rolling(n, min_periods=1).max().to_numpy()
