"""Two checks I am not willing to take on trust.

1. FILL LEAKAGE. The engine fills at the NEXT bar's open. A signal at 21:45 MYT
   fills at 22:00 MYT -- outside a 19:00-22:00 window. How much of the window
   result is trades the user cannot legally open?
2. The volume leg's quintile response, which is the cleanest claimed kill.
"""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate
pd.set_option("display.width",250); pd.set_option("display.max_columns",40)

PAN=[("XAU 15m",Panel("XAUUSDT","15m")),
     ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
     ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026)))]

print("=== 1. FILL LEAKAGE: how many window trades actually OPEN outside the window? ===")
print("    signal-in-window = the current definition;  fill-in-window = shift the mask one bar\n")
rows=[]
for lbl,p in PAN:
    bl,bs=p.base(); d=p.d
    for wn in ["A","B"]:
        w=p.window(wn)
        wf=w.shift(-1).fillna(False).astype(bool)   # this bar's FILL lands in the window
        leak=int((w & ~wf).sum())
        for defn,m in [("signal-in-window",w),("fill-in-window",wf)]:
            r=simulate(p,bl&m,bs&m,2.5,2.5,"")
            if r: rows.append(dict(panel=lbl,window=wn,definition=defn,
                        **{k:r.row()[k] for k in ["n","wr","wrL","wrS","be","edge","pf","per_day"]}))
o=pd.DataFrame(rows)
print(o.to_string(index=False))
print("\n  fill-hour distribution of signal-in-window base trades (XAU 15m, window B):")
p=PAN[0][1]; d=p.d; bl,bs=p.base(); w=p.window("B")
sig=(bl|bs)&w&d.in_session
fill_hour=d.hour_myt.shift(-1)[sig]
print("   ", fill_hour.value_counts().sort_index().to_dict())

print("\n=== 2. the 4H-volume leg: full quintile response inside window B ===")
print("    the claimed mechanism ('high participation carries the pullback') predicts")
print("    the TOP bin is best. If the top bin is cold, the >0.5 threshold is a pooling trick.\n")
for lbl,p in PAN[1:]:
    d=p.d; bl,bs=p.base(); w=p.window("B")
    v=d["4h_vol_pctile_100"]
    qs=v.quantile([0,.25,.5,.75,1.0]).to_numpy()
    base=simulate(p,bl&w,bs&w,2.5,2.5,"base-in-B")
    print(f"--- {lbl}   base-in-B: n={base.n} wr={base.wr} be={base.be}")
    for i in range(4):
        lo,hi=(-np.inf if i==0 else qs[i]),(np.inf if i==3 else qs[i+1])
        m=(v>=lo)&(v<hi)
        r=simulate(p,bl&w&m,bs&w&m,2.5,2.5,"")
        if r: print(f"    vol pctile [{lo:.2f},{hi:.2f})  n={r.n:4} wr={r.wr:5} vs base {base.wr:5}  "
                    f"diff={round(r.wr-base.wr,1):+6}  pf={r.pf}")
