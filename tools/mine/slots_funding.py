"""How much equity each concurrency slot costs, in dollars.

concurrency.py showed the extra trades are just as good (win% flat at ~43 across
1-6 slots) -- so the frequency the owner wants is not blocked by the rule, it is
blocked by capital. This prices that: for each slot count, the starting equity that
buys the SAME survival odds as 1 slot at $200.

Fixed lot means the answer is a pure equity question with no re-optimisation.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
sys.path.insert(0, r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\live")
from backtest.data.db import Database
import concurrency as C
import numpy as np

RNG = np.random.default_rng(23)
N_PATHS = 20000


def ruin_at(net_d, n_year, start, block=20):
    k = len(net_d)
    nb = int(np.ceil(n_year / block))
    st = RNG.integers(0, k, size=(N_PATHS, nb))
    idx = (st[:, :, None] + np.arange(block)[None, None, :]) % k
    eq = start + np.cumsum(net_d[idx.reshape(N_PATHS, -1)[:, :n_year]], axis=1)
    rm = np.minimum.accumulate(eq, axis=1)[:, -1]
    return float((rm <= 0).mean() * 100), float((rm <= 60).mean() * 100), float(np.median(eq[:, -1]))


def find(net_d, n_year, target):
    lo, hi = 100.0, 6000.0
    for _ in range(22):
        mid = (lo + hi) / 2
        if ruin_at(net_d, n_year, mid)[0] > target:
            lo = mid
        else:
            hi = mid
    return round(hi / 25) * 25


with Database() as db:
    print(f"{'slots':>5} {'/week':>6} {'$/yr @0.01':>11} {'need for 15% ruin':>18} "
          f"{'need for 5%':>12} {'ruin @$200':>11} {'ruin @$500':>11} {'ruin @$1000':>12}")
    print("-" * 96)
    for mc in (1, 2, 3, 4, 6):
        fired, tr = C.run(db, mc)
        tr.sort(key=lambda x: x[1])
        net_d = np.array([x[2] for x in tr]) * C.ATR_NOW
        span_y = (tr[-1][0] - tr[0][0]) / (365.25 * 86400 * 1000)
        n_year = int(round(len(tr) / span_y))
        e15, e05 = find(net_d, n_year, 15.0), find(net_d, n_year, 5.0)
        r200 = ruin_at(net_d, n_year, 200)[0]
        r500 = ruin_at(net_d, n_year, 500)[0]
        r1k = ruin_at(net_d, n_year, 1000)[0]
        print(f"{mc:5d} {len(tr)/span_y/52:6.1f} {net_d.mean()*n_year:11,.0f} "
              f"{'$'+format(e15,',.0f'):>18} {'$'+format(e05,',.0f'):>12} "
              f"{r200:10.1f}% {r500:10.1f}% {r1k:11.1f}%")
