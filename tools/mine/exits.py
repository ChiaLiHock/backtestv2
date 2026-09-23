"""Two-stage exit evaluator, and the frequency/edge frontier.

The mine measures ENTRIES against a symmetric bracket because that is the only
bracket with a drift-free null. But the recommended exit is not symmetric: it is
TP1-partial -> breakeven -> chandelier trail. A rule has to be re-measured under
the exit it will actually be traded with before anything is claimed about it.

Path is walked at the panel's own resolution (1m for XAU/BTC/ETH, 5m for PAXG).
Ordering inside each path bar is pessimistic: the stop is tested before the target.
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np
import pandas as pd

SP = r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0, SP)
from screen import Panel, cost_of, PATH_MIN, NULL_WR  # noqa: E402

DB = r"C:\inetpub\Claude\ITSupport\backtest\data\market.db"
_PATH_CACHE: dict[str, tuple] = {}


def path_of(symbol: str):
    if symbol not in _PATH_CACHE:
        tf = "1m" if PATH_MIN[symbol] == 1 else "5m"
        con = sqlite3.connect(DB)
        p = pd.read_sql_query(
            "select open_time,high,low,close from candles where symbol=? and interval=? "
            "order by open_time", con, params=(symbol, tf))
        _PATH_CACHE[symbol] = (p.open_time.to_numpy("int64"), p.high.to_numpy(),
                               p.low.to_numpy(), p.close.to_numpy())
    return _PATH_CACHE[symbol]


def _walk(HI, LO, CL, side, e, A, s, horizon, sl, tp1, frac, k):
    """-> (gross P/L per unit, path bars held, did TP1 fill)."""
    j = min(s + horizon, HI.size)
    stop = e - side * sl * A
    t1 = e + side * tp1 * A
    peak = e
    got = False
    pnl = 0.0
    rem = 1.0
    for x in range(s, j):
        hi, lo = HI[x], LO[x]
        if (side == 1 and lo <= stop) or (side == -1 and hi >= stop):
            return pnl + rem * side * (stop - e), x - s, got
        if not got and ((side == 1 and hi >= t1) or (side == -1 and lo <= t1)):
            pnl += frac * side * (t1 - e)
            rem -= frac
            got = True
            stop = e                      # breakeven
            peak = t1
        if got:
            peak = max(peak, hi) if side == 1 else min(peak, lo)
            trail = peak - side * k * A
            stop = max(stop, trail) if side == 1 else min(stop, trail)
    x = min(j - 1, HI.size - 1)
    return pnl + rem * side * (CL[x] - e), x - s, got


def two_stage(p: Panel, ml, ms, sl=2.5, tp1=2.5, frac=0.5, k=2.5, horizon_min=2880,
              name=""):
    d = p.d
    OT, HI, LO, CL = path_of(p.symbol)
    ot = d.open_time.to_numpy("int64")
    atr = d.atr.to_numpy(); ent = d.entry.to_numpy()
    sess = d.in_session.to_numpy(bool)
    ml = np.asarray(pd.Series(ml).fillna(False), dtype=bool) & sess
    ms = np.asarray(pd.Series(ms).fillna(False), dtype=bool) & sess
    start = np.searchsorted(OT, np.r_[ot[1:], ot[-1]], side="left")
    horizon = horizon_min // PATH_MIN[p.symbol]
    free = -1
    rec = []
    for i in range(len(d) - 1):
        if ot[i] < free or (ml[i] and ms[i]):
            continue
        side = 1 if ml[i] else (-1 if ms[i] else 0)
        if side == 0 or not np.isfinite(ent[i]) or not np.isfinite(atr[i]):
            continue
        s = start[i]
        if s >= OT.size:
            continue
        g, b, got = _walk(HI, LO, CL, side, ent[i], atr[i], s, horizon, sl, tp1, frac, k)
        net = g - cost_of(ent[i])
        rec.append((side, net, net / (sl * atr[i]), b, got))
        free = ot[i] + p.anchor_ms + b * p.path_ms
    if len(rec) < 8:
        return None
    t = pd.DataFrame(rec, columns=["side", "pnl", "R", "pb", "tp1"])
    days = (d.open_time.iloc[-1] - d.open_time.iloc[0]) / 86_400_000
    wr = (t.pnl > 0).mean() * 100
    eq = t.R.cumsum(); dd = float((eq.cummax() - eq).max())
    return dict(rule=name, n=len(t), wr=round(wr, 1),
                ci=round(1.96 * np.sqrt(max(wr * (100 - wr), 1) / len(t)), 1),
                wrL=round((t.pnl[t.side == 1] > 0).mean() * 100, 1) if (t.side == 1).any() else np.nan,
                wrS=round((t.pnl[t.side == -1] > 0).mean() * 100, 1) if (t.side == -1).any() else np.nan,
                tp1=round(t.tp1.mean() * 100, 1),
                pf=round(t.pnl[t.pnl > 0].sum() / max(-t.pnl[t.pnl <= 0].sum(), 1e-9), 2),
                evR=round(t.R.mean(), 3), netR=round(t.R.sum(), 1), ddR=round(dd, 1),
                bestR=round(t.R.max(), 1),
                hrs=round(float(t.pb.mean()) * PATH_MIN[p.symbol] / 60, 1),
                per_day=round(len(t) / days, 2))


def frontier(p: Panel, variants: list[tuple[str, object, object]], sl=2.5, tp1=2.5, k=2.5):
    """Frequency/edge frontier: trades per day against pooled win rate."""
    rows = []
    for nm, L, S in variants:
        r = two_stage(p, L, S, sl=sl, tp1=tp1, k=k, name=nm)
        if r:
            rows.append(r)
    out = pd.DataFrame(rows)
    if len(out):
        out["wr_vs_null"] = (out.wr - NULL_WR).round(1)
    return out


if __name__ == "__main__":
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
    from screen import _load
    p = _load(sys.argv[1])
    b = p.base()
    print(f"# {p.label} — two-stage exit (SL 2.5 / TP1 2.5 half / chandelier 2.5)")
    print(pd.DataFrame([two_stage(p, *b, name="base rule")]).to_string(index=False))
