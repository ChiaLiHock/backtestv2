"""Set daily_loss_limit from the real daily P/L distribution, not from a round number.

Same standard applied to consecutive_loss_halt in §9.3: a guard that fires on ordinary
variance gets switched off by its owner. The limit should sit past the ordinary bad day
and short of the catastrophic one.

P/L is attributed to the day the position CLOSED, which is when the account actually
sees it and when a live daily-loss guard would trip.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
sys.path.insert(0, r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\live")
from backtest.data.db import Database
from datetime import datetime, timezone
import concurrency as C

with Database() as db:
    for mc in (1, 2):
        fired, tr = C.run(db, mc)
        span_y = (max(t[0] for t in tr) - min(t[0] for t in tr)) / (365.25*86400*1000)
        day = {}
        for _, xt, r in tr:
            d = datetime.fromtimestamp(xt/1000, timezone.utc).date()
            day[d] = day.get(d, 0.0) + r * C.ATR_NOW
        v = np.array(sorted(day.values()))
        trading_days = len(day)
        print(f"\n=== {mc} slot(s): {trading_days} days with a close over {span_y:.2f}y "
              f"({trading_days/span_y:.0f}/yr)   worst day -${-v.min():,.0f} ===")
        print(f"{'limit':>7} {'days worse':>11} {'per year':>9} {'once every':>12} "
              f"{'% of losing days':>17}")
        losing = (v < 0).sum()
        for lim in (60, 80, 100, 130, 150, 180, 220, 260):
            c = int((v <= -lim).sum())
            per_y = c / span_y
            every = f"{1/per_y:.1f} yr" if per_y else ">4.2 yr"
            print(f"{'-$'+str(lim):>7} {c:11d} {per_y:9.2f} {every:>12} "
                  f"{c/losing*100:16.1f}%")
        print(f"  percentiles of daily P/L: "
              f"p1 ${np.percentile(v,1):,.0f}  p5 ${np.percentile(v,5):,.0f}  "
              f"median ${np.median(v):,.0f}  p95 ${np.percentile(v,95):,.0f}")
