"""Does breadth solve the frequency problem? Run the same rule on every instrument."""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate, NULL_WR
from exits import two_stage
pd.set_option("display.width",250); pd.set_option("display.max_columns",40)

rows=[]
for sym in ["XAUUSDT","BTCUSDT","ETHUSDT"]:
    for anch in ["15m"]:
        p=Panel(sym,anch); b=p.base()
        r=simulate(p,*b,2.5,2.5,name=f"{sym} {anch}")
        t=two_stage(p,*b,name=f"{sym} {anch}")
        if r: rows.append(dict(inst=f"{sym.replace('USDT','')} {anch}", **{k:r.row()[k] for k in
             ["n","wr","ci","wrL","wrS","be","edge","pf","per_day"]},
             ts_wr=t["wr"] if t else None, ts_pf=t["pf"] if t else None, ts_evR=t["evR"] if t else None))
print("=== base rule, symmetric 2.5/2.5, per instrument (166 days, same window) ===")
print(f"null WR = {NULL_WR}%   be = break-even WR after cost   edge = wr - be")
print(pd.DataFrame(rows).to_string(index=False))
print()
print("ts_* = the two-stage exit (SL2.5 / TP1 2.5 half / chandelier 2.5)")

# pooled portfolio view
tot_n = sum(r["n"] for r in rows); tot_day = sum(r["per_day"] for r in rows)
wr = sum(r["n"]*r["wr"] for r in rows)/tot_n
print(f"\nRunning all three together: {tot_n} trades, {tot_day:.2f}/day, "
      f"weighted pooled WR {wr:.1f}%  (null 49.8%)")
