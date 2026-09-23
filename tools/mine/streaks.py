"""Is `consecutive_loss_halt 5` a safety net or a nuisance trigger?

The halt was specified when the planning win rate was 53.5%. The shipping bracket
wins 42.5%, and a halt threshold is a statement about the LOSS rate, so it does not
carry over. If 5 straight losses is an ordinary week the bot will stop itself
several times a year for no reason, and a guard that cries wolf gets switched off.

Measured on the real trade sequence, not a formula.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
sys.path.insert(0, r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\live")
from backtest.data.db import Database
import concurrency as C


def streak_counts(net_d):
    """How many times a run of >= k losses STARTS, for each k."""
    runs, cur = [], 0
    for x in net_d:
        if x <= 0:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    return runs


with Database() as db:
    for mc in (1, 2, 3):
        fired, tr = C.run(db, mc)
        tr.sort(key=lambda x: x[1])
        net_d = np.array([x[2] for x in tr]) * C.ATR_NOW
        span_y = (tr[-1][0] - tr[0][0]) / (365.25 * 86400 * 1000)
        runs = np.array(streak_counts(net_d))
        loss = float((net_d <= 0).mean())
        print(f"\n=== {mc} slot(s)  n={len(tr)}  {len(tr)/span_y:.0f} trades/yr  "
              f"loss rate {loss*100:.1f}%  worst streak {runs.max()} ===")
        print(f"{'k':>3} {'runs >= k in 4.2y':>18} {'per year':>9} {'once every':>12} "
              f"{'$ at risk (SL 2.5xATR)':>23}")
        for k in range(3, 13):
            c = int((runs >= k).sum())
            per_y = c / span_y
            every = f"{1/per_y:.1f} yr" if per_y else ">4.2 yr"
            print(f"{k:3d} {c:18d} {per_y:9.2f} {every:>12} "
                  f"{'-$' + format(k * 25.8, ',.0f'):>23}")
