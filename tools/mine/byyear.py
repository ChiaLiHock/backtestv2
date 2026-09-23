"""Per-year, long-only, in DOLLARS at a fixed 0.01 lot -- and on the real sequence.

The bootstrap in bracket_ruin.py destroys the actual order of trades. The actual
order is where 2023 lives. A bracket that wins on average but hands a $200 account
its real 2023 is not tradable, so this walks the trades as they happened.

Dollars are R * today's ATR: gold ran 1,800 -> 4,300, so a 2022 trade's raw dollar
P/L is a sample of 2022's price, not of what the account will see next year.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
from backtest.tools.validate_rule import measure, breakeven_wr
from backtest.data.db import Database
from datetime import datetime, timezone

ATR_NOW = 10.07          # 2026 median signal-bar ATR, from bracket_ruin.py
START, FLOOR = 200.0, 60.0

def yr(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).year

def walk(net_d, start=START):
    eq = start + np.cumsum(net_d)
    peak = np.maximum.accumulate(np.concatenate([[start], eq]))
    dd = float((peak - np.concatenate([[start], eq])).max())
    streak = mx = 0
    for x in net_d:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    return float(eq[-1]), dd, mx, float(eq.min())

with Database() as db:
    for tp, sl in [(2.5,2.5), (3.0,2.5), (3.5,2.5), (5.0,2.5), (5.0,2.0)]:
        p = measure(db, "MT5:GOLD", "15m", tp_mult=tp, sl_mult=sl, cost_bps=0.85,
                    use_5m=False, path_tf="15m", session="gold_session")
        ts = p["long"].trades
        net_d = np.array([t.net / t.atr for t in ts]) * ATR_NOW
        years = np.array([yr(t.entry_time) for t in ts])
        be = breakeven_wr(tp, sl, ATR_NOW, 4556 * 0.85/1e4)
        end, dd, mx, lowest = walk(net_d)
        print(f"\n=== LONG-ONLY TP{tp}/SL{sl}  n={len(ts)}  be={be:.1f}%  "
              f"real run: $200 -> ${end:,.0f}   maxDD ${dd:,.0f}   "
              f"worst streak {mx}   lowest equity ${lowest:,.0f}"
              f"{'   *** WOULD HAVE BLOWN UP ***' if lowest <= 0 else ''}")
        print(f"{'year':>6} {'n':>5} {'win%':>6} {'$ P/L':>9} {'maxDD$':>8} {'streak':>7}")
        for y in sorted(set(years)):
            m = years == y; d = net_d[m]
            _, ydd, ymx, _ = walk(d, START)
            print(f"{y:>6} {m.sum():5d} {(d>0).mean()*100:6.1f} {d.sum():+9.1f} "
                  f"{ydd:8.0f} {ymx:7d}")
