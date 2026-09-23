"""The comparator that kills long-only findings: a RANDOM long entry in a bull market.

Gold went 1,800 -> 4,300 across this panel. At an asymmetric bracket a random long
clears break-even on drift alone, so "long-only beats break-even by +8.7" is not
evidence of anything until the random long's number is on the table next to it.

Same protocol as the rule: next-bar open, non-overlapping sequential, gold_session
only, ties -> loss, same 96-bar horizon, same 0.85 bps. Only the entry is random.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
from backtest.tools.validate_rule import measure, walk_forward, breakeven_wr
from backtest.data.db import Database, CandleRepository, INTERVAL_MS
from backtest.engine import features as F

K = 300
RNG = np.random.default_rng(7)
COST = 0.85

def wilder_atr(h, l, c, n=14):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a = np.full(tr.size, np.nan); a[n] = tr[1:n+1].mean()
    for i in range(n+1, tr.size):
        a[i] = (a[i-1] * (n-1) + tr[i]) / n
    return a

def null_dist(ot, op, hi, lo, cl, atr, ok, lo_i, hi_i, n_target, tp, sl, horizon_ms, side):
    """K replicates of a random-entry sequential simulation. Returns win% array."""
    elig = np.flatnonzero(ok[lo_i:hi_i]) + lo_i
    q = min(1.0, n_target / max(len(elig), 1) * 6.0)   # oversample; skips thin it out
    out = np.empty(K)
    for k in range(K):
        take = elig[RNG.random(elig.size) < q]
        busy = -1; wins = 0; n = 0
        for i in take:
            if ot[i] < busy or i + 1 >= ot.size: continue
            e = op[i+1]; a = atr[i]
            if not np.isfinite(e) or not np.isfinite(a): continue
            tpx, slx = (e + tp*a, e - sl*a) if side == "long" else (e - tp*a, e + sl*a)
            r, px, xt, _ = walk_forward(ot, hi, lo, cl, int(ot[i+1]), e, tpx, slx,
                                        side == "long", horizon_ms)
            if r == "nodata": continue
            gross = (px - e) if side == "long" else (e - px)
            if gross - e * COST / 1e4 > 0: wins += 1
            n += 1; busy = xt
        out[k] = wins / n * 100 if n else np.nan
    return out

with Database() as db:
    repo = CandleRepository(db)
    df = repo.load("MT5:GOLD", "15m")
    ot = df["open_time"].to_numpy(dtype="int64")
    op, hi, lo, cl = (df[c].to_numpy() for c in ("open", "high", "low", "close"))
    atr = wilder_atr(hi, lo, cl)
    sess = np.array([F.is_gold_session(int(t)) for t in ot], dtype=bool)
    ok = sess & np.isfinite(atr)
    horizon_ms = 96 * INTERVAL_MS["15m"]

    print(f"{'bracket':>12} {'side':6} {'rule n':>7} {'rule%':>6} {'be':>6} "
          f"{'NULL mean%':>10} {'null sd':>7} {'drift':>7} {'rule-null':>9} {'pctile':>7}")
    print("-" * 92)
    for tp, sl in [(2.5, 2.5), (3.0, 2.5), (3.5, 2.5), (5.0, 2.5), (5.0, 2.0), (2.0, 2.0)]:
        p = measure(db, "MT5:GOLD", "15m", tp_mult=tp, sl_mult=sl, cost_bps=COST,
                    use_5m=False, path_tf="15m", session="gold_session")
        for side in ("long", "short"):
            ts = p[side].trades
            if not ts: continue
            lo_i = int(np.searchsorted(ot, ts[0].entry_time))
            hi_i = int(np.searchsorted(ot, ts[-1].entry_time)) + 1
            rule = sum(1 for t in ts if t.net > 0) / len(ts) * 100
            med_atr = float(np.median([t.atr for t in ts]))
            med_px = float(np.median([t.entry_price for t in ts]))
            be = breakeven_wr(tp, sl, med_atr, med_px * COST / 1e4)
            d = null_dist(ot, op, hi, lo, cl, atr, ok, lo_i, hi_i,
                          len(ts), tp, sl, horizon_ms, side)
            d = d[np.isfinite(d)]
            pct = (d < rule).mean() * 100
            print(f"{'TP%.1f/SL%.1f' % (tp, sl):>12} {side:6} {len(ts):7d} {rule:6.1f} "
                  f"{be:6.1f} {d.mean():10.1f} {d.std():7.2f} {d.mean()-be:+7.1f} "
                  f"{rule-d.mean():+9.1f} {pct:6.1f}%")
