"""The ladder from 'buy while the fast TFs are still selling' to 'wait for them to come back'."""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate
pd.set_option("display.width",260); pd.set_option("display.max_columns",40)

PAN=[("XAU 15m",Panel("XAUUSDT","15m")),
     ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
     ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026)))]

print("=== THE LADDER: how much of the fast-timeframe pullback do you wait out? ===")
print("    every rung has the SAME slow-timeframe gate (1h + 4h bullish).\n")
rows=[]
for lbl,p in PAN:
    d=p.d
    slowL=(d.bias_1h==1)&(d.bias_4h==1); slowS=(d.bias_1h==-1)&(d.bias_4h==-1)
    rl=lambda s,n: s.rolling(n,min_periods=1).max().astype(bool)
    rungs={
     "0. both 5m+15m still AGAINST (your idea)": (slowL&(d.bias_5m==-1)&(d.bias_15m==-1),
                                                  slowS&(d.bias_5m== 1)&(d.bias_15m== 1)),
     "1. 5m flipped back, 15m still against":   (slowL&(d.bias_5m== 1)&(d.bias_15m==-1),
                                                  slowS&(d.bias_5m==-1)&(d.bias_15m== 1)),
     "2. 15m flipped back, 5m still against":   (slowL&(d.bias_5m==-1)&(d.bias_15m== 1),
                                                  slowS&(d.bias_5m== 1)&(d.bias_15m==-1)),
     "3. BOTH flipped back (all 4 agree)":      (slowL&(d.bias_5m== 1)&(d.bias_15m== 1),
                                                  slowS&(d.bias_5m==-1)&(d.bias_15m==-1)),
     "4. + 30m agrees too (all 5)":             (slowL&(d.bias_5m==1)&(d.bias_15m==1)&(d.bias_30m==1),
                                                  slowS&(d.bias_5m==-1)&(d.bias_15m==-1)&(d.bias_30m==-1)),
     "5. = THE BASE RULE (all 5 + 4H stack + reclaim)": p.base(),
    }
    for nm,(L,S) in rungs.items():
        r=simulate(p,pd.Series(L).fillna(False),pd.Series(S).fillna(False),2.5,2.5,nm)
        if r: rows.append(dict(panel=lbl, rung=nm, **{k:r.row()[k] for k in
                ["n","wr","ci","wrL","wrS","be","edge","pf","per_day"]}))
o=pd.DataFrame(rows)
for lbl,_ in PAN:
    print(f"### {lbl}")
    print(o[o.panel==lbl].drop(columns=["panel"]).to_string(index=False)); print()

print("=== Is there a fast BOUNCE at all? forward excursion from your setup, in ATR ===")
print("    (if the bounce is real but small, a mean-reversion bracket might catch it)\n")
for lbl,p in PAN[:2]:
    d=p.d
    m=((d.bias_1h==1)&(d.bias_4h==1)&(d.bias_5m==-1)&(d.bias_15m==-1)&d.in_session).fillna(False)
    ms=((d.bias_1h==-1)&(d.bias_4h==-1)&(d.bias_5m==1)&(d.bias_15m==1)&d.in_session).fillna(False)
    print(f"--- {lbl}  (long setups n={int(m.sum())} bars)")
    for h in [60,240,720]:
        up=d.loc[m,f"mfe_{h}"].median(); dn=d.loc[m,f"mae_{h}"].median()
        print(f"    within {h:4}min:  median favourable {up:5.2f} ATR   median adverse {dn:5.2f} ATR"
              f"   -> {'bounce' if up>dn else 'continuation'}")
    print(f"    for comparison, the BASE rule's bars:")
    bl,_=p.base(); bm=(bl&d.in_session).fillna(False)
    for h in [60,240]:
        print(f"    within {h:4}min:  median favourable {d.loc[bm,f'mfe_{h}'].median():5.2f} ATR"
              f"   median adverse {d.loc[bm,f'mae_{h}'].median():5.2f} ATR")
    print()

print("=== does a MEAN-REVERSION bracket rescue it? (small target, wide stop) ===")
for lbl,p in PAN[:2]:
    d=p.d
    L=((d.bias_1h==1)&(d.bias_4h==1)&(d.bias_5m==-1)&(d.bias_15m==-1)).fillna(False)
    S=((d.bias_1h==-1)&(d.bias_4h==-1)&(d.bias_5m==1)&(d.bias_15m==1)).fillna(False)
    print(f"--- {lbl}")
    for tp,sl in [(1.5,3.0),(1.5,2.5),(2.0,3.0),(2.5,2.5),(3.0,2.5)]:
        r=simulate(p,L,S,tp,sl,"")
        if r: print(f"    TP{tp}/SL{sl}  n={r.n:5} wr={r.wr:5} be={r.be:5} edge={round(r.wr-r.be,1):+6} pf={r.pf}")
    print()
