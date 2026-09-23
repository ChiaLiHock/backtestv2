"""Pick the bracket by SURVIVAL at a fixed 0.01 lot, not by net per unit.

net/unit ranks brackets by expectancy. A $200 account at a fixed 0.01 lot is not
ranked by expectancy -- it is ranked by P(the drawdown arrives before the drift).
Bigger TP raises expectancy and lowers win rate; those pull opposite ways on ruin.
Nobody has measured which wins.

Two things this does that a naive sweep would get wrong:

1. Trades are re-expressed in R (net/ATR) and repriced at TODAY's ATR. Gold ran
   1,800 -> 4,300 across the panel, so a 2022 trade's dollar P/L is not a sample
   of what a 2026 trade pays. Ranking brackets on raw 4-year dollars would be
   ranking them on the price of gold in 2022.
2. Block bootstrap alongside i.i.d. Ruin is driven by streaks, and streaks are
   exactly what i.i.d. resampling destroys.
"""
import sys, json
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
from backtest.tools.validate_rule import measure, breakeven_wr
from backtest.data.db import Database

START_EQ = 200.0
FLOOR    = 60.0
N_PATHS  = 40000
RNG      = np.random.default_rng(20260823)

def mc(net_d, n_year, start=START_EQ, block=1):
    """net_d: dollar P/L per trade at 0.01 lot. Returns ruin/floor/median stats."""
    k = len(net_d)
    if k < 30: return None
    if block == 1:
        draws = RNG.integers(0, k, size=(N_PATHS, n_year))
        paths = net_d[draws]
    else:                                   # moving-block bootstrap, keeps streaks
        nb = int(np.ceil(n_year / block))
        starts = RNG.integers(0, k, size=(N_PATHS, nb))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % k
        paths = net_d[idx.reshape(N_PATHS, -1)[:, :n_year]]
    eq = start + np.cumsum(paths, axis=1)
    run_min = np.minimum.accumulate(eq, axis=1)[:, -1]
    peak = np.maximum.accumulate(np.column_stack([np.full(N_PATHS, start), eq]), axis=1)
    dd = (peak - np.column_stack([np.full(N_PATHS, start), eq])).max(axis=1)
    return dict(
        p_zero  = float((run_min <= 0).mean() * 100),
        p_floor = float((run_min <= FLOOR).mean() * 100),
        med_end = float(np.median(eq[:, -1])),
        p10_end = float(np.percentile(eq[:, -1], 10)),
        med_dd  = float(np.median(dd)),
    )

def main():
    grid_tp = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    grid_sl = [2.0, 2.5, 3.0]
    rows = []
    with Database() as db:
        # today's scale: median ATR of trades entered in 2026
        base = measure(db, "MT5:GOLD", "15m", tp_mult=2.5, sl_mult=2.5, cost_bps=0.85,
                       use_5m=False, path_tf="15m", session="gold_session")
        allt = base["long"].trades + base["short"].trades
        atr_now = float(np.median([t.atr for t in allt if t.entry_time >= 1767225600000]))
        px_now  = float(np.median([t.entry_price for t in allt if t.entry_time >= 1767225600000]))
        span_ms = allt[-1].entry_time - allt[0].entry_time
        years = span_ms / (365.25 * 86400 * 1000)
        print(f"repricing scale: 2026 median ATR {atr_now:.2f} on price {px_now:,.0f}"
              f"   panel {years:.2f}y\n")

        for sl in grid_sl:
            for tp in grid_tp:
                p = measure(db, "MT5:GOLD", "15m", tp_mult=tp, sl_mult=sl, cost_bps=0.85,
                            use_5m=False, path_tf="15m", session="gold_session")
                for side in ("long", "pooled"):
                    ts = p["long"].trades if side == "long" else p["long"].trades + p["short"].trades
                    if not ts: continue
                    net_r = np.array([t.net / t.atr for t in ts])
                    net_d = net_r * atr_now                 # dollars at 0.01 lot, today's scale
                    n = len(ts); wins = int((net_r > 0).sum())
                    n_year = int(round(n / years))
                    be = breakeven_wr(tp, sl, atr_now, px_now * 0.85 / 1e4)
                    r = dict(side=side, tp=tp, sl=sl, n=n, n_year=n_year,
                             win=round(wins / n * 100, 1), be=round(be, 1),
                             edge=round(wins / n * 100 - be, 1),
                             exp_d=round(float(net_d.mean()), 3),
                             yr_d=round(float(net_d.mean()) * n_year, 1),
                             med_win=round(float(np.median(net_d[net_d > 0])), 2),
                             med_loss=round(float(np.median(net_d[net_d <= 0])), 2))
                    for tag, blk in (("iid", 1), ("blk", 20)):
                        m = mc(net_d, n_year, block=blk)
                        if m: r.update({f"{tag}_{k}": round(v, 1) for k, v in m.items()})
                    rows.append(r)
                    print(f"{side:6s} TP{tp:<4}/SL{sl:<4} n={n:5d} {n_year:4d}/yr "
                          f"win {r['win']:5.1f} be {r['be']:5.1f} edge {r['edge']:+5.1f} "
                          f"$/yr {r['yr_d']:+8.1f}  ruin0 {r.get('iid_p_zero',float('nan')):5.1f}%"
                          f"/{r.get('blk_p_zero',float('nan')):5.1f}%  "
                          f"p10 ${r.get('iid_p10_end',float('nan')):7.0f}  "
                          f"medDD ${r.get('iid_med_dd',float('nan')):6.0f}")
    json.dump(rows, open(r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\live\bracket_ruin.json", "w"), indent=1)

main()
