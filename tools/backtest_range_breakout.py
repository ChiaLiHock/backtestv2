"""Two-regime signals: RANGE (mean reversion) and BREAKOUT (continuation).

Design rationale (2026-09-25, after the rule's live drawdown):

The measured rule is long-only with a symmetric $25/$25 bracket — in a
$40-ATR trending August it kept buying a falling market. The lesson is
not "reversal is bad" but "a bracket must match the regime it trades":

RANGE signal  — trades ONLY when the market is demonstrably range-bound
                (low ADX, contained within a structure). Fades the edges,
                symmetric bracket sized to the range.
BREAKOUT signal — trades ONLY when the market is demonstrably breaking
                out (squeeze then expansion with volume). Asymmetric
                bracket: tight stop at the broken level, target = the
                measured move. Low win rate, paid by odds.

Both are evaluated in the honest frame: closed 15m bars, entry next 1m
open, 1m walk (first touch, tie = loss), 24h timeout, 11bp cost.

The regime FILTER is the strategy here — the entries are deliberately
simple, so the thing being tested is regime detection.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")
sys.path.insert(0, r"C:\Users\User\Downloads\backtest\backtest")

from backtest.data.db import Database, CandleRepository   # noqa: E402
from backtest.tools.validate_rule import walk_forward     # noqa: E402

MYT = timezone(timedelta(hours=8))
DAY = 86_400_000
H = 3_600_000
STEP = 900_000
HORIZON = 24 * H
COST = 11e-4

FEEDS = {"XAU": ("XAUUSDT", "1m"), "MT5": ("MT5:GOLD", "5m"),
         "PAXG": ("PAXGUSDT", "5m")}

# --- regime + signal parameters (few, coarse, pre-registered) -----------
RANGE_ADX_MAX = 20          # regime: no trend
RANGE_LOOKBACK = 32         # bars defining the operating range (~8h)
RANGE_EDGE_ATR = 0.25       # entry: within 25% of ATR of an edge
RANGE_SL_PAD_ATR = 0.6      # SL just beyond the edge
RANGE_TP_FRAC = 0.5         # TP = halfway to the other edge (v1 at 0.9
                            # crossed whole trends and died first)
RANGE_MIN_TOUCHES = 3       # the range must have been TESTED this many
                            # times before an edge is fadeable — a fresh
                            # "range" in a trend is just a pause

BRK_SQUEEZE_ATR_PCT = 25    # percentile: ATR now vs trailing 200 bars
BRK_VOL_MULT = 1.5          # breakout bar volume vs rolling median
BRK_CLOSE_ATR = 0.5         # close beyond the range by this much ATR
BRK_HOLD_BARS = 1           # bars the close must STAY outside before
                            # entry — v1 entered on the break bar and
                            # paid for every failure
BRK_SL_ATR = 1.0            # SL back inside the range
BRK_TP_RANGE_MULT = 1.0     # TP = one measured range (v1's 2x never hit)


def ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr14(h, l, c):
    n = len(c)
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]),
                    abs(l[i] - c[i - 1]))
    return ema(tr, 14)


def adx14(h, l, c):
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)),
                                      abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr_ = ema(tr, 14)
    pdi = 100 * ema(plus, 14) / np.maximum(atr_, 1e-9)
    mdi = 100 * ema(minus, 14) / np.maximum(atr_, 1e-9)
    dx = 100 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    return ema(dx, 14)


def fold(df, step):
    key = (df["open_time"] // step) * step
    return df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum")).reset_index(drop=True)


def stats(rows):
    if not rows:
        return "n=0"
    r = np.array([x["r"] for x in rows])
    gl = -r[r < 0].sum()
    pf = r[r > 0].sum() / gl if gl > 0 else float("inf")
    return (f"n={len(r):4d} win={100*(r>0).mean():5.1f}% "
            f"expR={r.mean():+.3f} pf={pf:5.2f} sumR={r.sum():+7.1f}")


def boot_lo(x, nb=2000, seed=0):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    return float(np.percentile(
        rng.choice(x, (nb, len(x))).mean(1), 2.5))


def run(feed):
    sym, tf = FEEDS[feed]
    with Database() as db:
        d = CandleRepository(db).load(sym, tf).reset_index(drop=True)
    d = d.sort_values("open_time").reset_index(drop=True)
    pt = d["open_time"].to_numpy("int64")
    ph = d["high"].to_numpy("float64")
    pl = d["low"].to_numpy("float64")
    pc = d["close"].to_numpy("float64")
    po = d["open"].to_numpy("float64")
    f = fold(d, STEP)
    t = f["open_time"].to_numpy("int64")
    o = f["open"].to_numpy("float64")
    h = f["high"].to_numpy("float64")
    l = f["low"].to_numpy("float64")
    c = f["close"].to_numpy("float64")
    v = f["volume"].to_numpy("float64")
    n = len(t)

    a = atr14(h, l, c)
    adx = adx14(h, l, c)
    vol_med = pd.Series(v).rolling(20, min_periods=5).median().to_numpy()
    # trailing ATR percentile (squeeze detection), causal
    atr_series = pd.Series(a)
    atr_pct = (atr_series.rolling(200, min_periods=50).rank(pct=True)
               .to_numpy())

    range_rows, brk_rows = [], []

    def resolve(entry_t, entry, sl, tp, is_long, kind, extra):
        why, px, xt, _amb = walk_forward(
            pt, ph, pl, pc, entry_t, entry, tp, sl, is_long, HORIZON)
        if why == "nodata":
            return
        resolved = why in ("tp", "sl") or int(pt[-1]) >= entry_t + HORIZON
        if not resolved:
            return
        pnl = (float(px) - entry) if is_long else (entry - float(px))
        row = {"entry_time": entry_t, "kind": kind,
               "r": (pnl - entry * COST) / abs(entry - sl),
               "win": pnl > 0, "why": why, **extra}
        (range_rows if kind == "range" else brk_rows).append(row)

    last_fire = {"range": -1, "brk": -1}
    for j in range(max(RANGE_LOOKBACK, 60), n - 1):
        if j - last_fire["range"] < 4 and j - last_fire["brk"] < 4:
            continue
        entry_t = int(t[j + 1])
        k = int(np.searchsorted(pt, entry_t, "left"))
        if k >= len(pt) or int(pt[k]) != entry_t:
            continue
        entry = float(po[k])
        atr_e = float(a[j])
        if atr_e <= 0 or entry <= 0:
            continue

        hi = float(h[j - RANGE_LOOKBACK:j].max())
        lo = float(l[j - RANGE_LOOKBACK:j].min())
        rng = hi - lo
        c_ = float(c[j])

        # how many times was each edge touched in the lookback? A range
        # worth fading has been TESTED; one touch per side is a pause in
        # a trend, and v1 died exactly there.
        def touches(prices, edge, side):
            w = atr_e * 0.5
            return int(((prices - edge) <= w).sum() if side == "hi"
                       else ((edge - prices) <= w).sum())

        closes_lb = c[j - RANGE_LOOKBACK:j]
        t_hi = touches(closes_lb, hi, "hi")
        t_lo = touches(closes_lb, lo, "lo")

        # ---------------- RANGE: fade the edge in a dead market --------
        if (adx[j] < RANGE_ADX_MAX and rng < 4 * atr_e
                and min(t_hi, t_lo) >= RANGE_MIN_TOUCHES
                and j - last_fire["range"] >= 4):
            near_hi = (hi - c_) <= RANGE_EDGE_ATR * atr_e
            near_lo = (c_ - lo) <= RANGE_EDGE_ATR * atr_e
            if near_hi and not near_lo:          # at the top -> short
                sl = hi + RANGE_SL_PAD_ATR * atr_e
                tp = hi - RANGE_TP_FRAC * rng
                if sl > entry > tp:
                    resolve(entry_t, entry, sl, tp, False, "range",
                            {"adx": float(adx[j])})
                    last_fire["range"] = j
            elif near_lo and not near_hi:        # at the bottom -> long
                sl = lo - RANGE_SL_PAD_ATR * atr_e
                tp = lo + RANGE_TP_FRAC * rng
                if sl < entry < tp:
                    resolve(entry_t, entry, sl, tp, True, "range",
                            {"adx": float(adx[j])})
                    last_fire["range"] = j

        # ---------------- BREAKOUT: squeeze then expansion --------------
        # v2: enter only after the close STAYS outside the range for
        # BRK_HOLD_BARS bars — the failure breakouts pay for the price
        # given up.
        if (not np.isnan(atr_pct[j]) and atr_pct[j - 1] < 0.25
                and v[j] > BRK_VOL_MULT * max(vol_med[j], 1e-9)
                and j - last_fire["brk"] >= 4):
            def held_out(side):
                for b in range(max(0, j - BRK_HOLD_BARS), j + 1):
                    cc = float(c[b])
                    if side == "up" and cc <= hi:
                        return False
                    if side == "dn" and cc >= lo:
                        return False
                return True

            up = (c_ - hi) > BRK_CLOSE_ATR * atr_e and held_out("up")
            dn = (lo - c_) > BRK_CLOSE_ATR * atr_e and held_out("dn")
            if up:
                sl = hi - BRK_SL_ATR * atr_e      # back inside the range
                tp = hi + BRK_TP_RANGE_MULT * rng
                if sl < entry < tp:
                    resolve(entry_t, entry, sl, tp, True, "breakout",
                            {"adx": float(adx[j])})
                    last_fire["brk"] = j
            elif dn:
                sl = lo + BRK_SL_ATR * atr_e
                tp = lo - BRK_TP_RANGE_MULT * rng
                if tp < entry < sl:
                    resolve(entry_t, entry, sl, tp, False, "breakout",
                            {"adx": float(adx[j])})
                    last_fire["brk"] = j

    for kind, rows in (("range", range_rows), ("breakout", brk_rows)):
        rows.sort(key=lambda x: x["entry_time"])
        r = np.array([x["r"] for x in rows]) if rows else np.array([0.0])
        lo95 = boot_lo(r) if len(rows) > 1 else float("nan")
        print(f"  {kind:9s} {stats(rows)}  lo95={lo95:+.3f}")
        # yearly split for stability
        yrs: dict[str, list] = {}
        for x in rows:
            yrs.setdefault(datetime.fromtimestamp(
                x["entry_time"] / 1000, MYT).strftime("%Y"), []).append(x)
        for y in sorted(yrs):
            print(f"     {y}: {stats(yrs[y])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--feeds", default="XAU,MT5,PAXG")
    a = p.parse_args()
    for feed in a.feeds.split(","):
        print(f"\n=== {feed} ===")
        run(feed)