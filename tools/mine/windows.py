"""Scout: how much sample survives inside the two MYT windows, and what they ARE."""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate
pd.set_option("display.width",250); pd.set_option("display.max_columns",40)

# MYT = UTC+8.  06:00-09:00 MYT = 22:00-01:00 UTC ; 19:00-22:00 MYT = 11:00-14:00 UTC
def winA(d): return d.hour_myt.isin([6,7,8])          # NY close -> Asia open
def winB(d): return d.hour_myt.isin([19,20,21])       # London PM -> NY open
def both(d): return winA(d)|winB(d)

PAN=[("XAU 15m",Panel("XAUUSDT","15m")),
     ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
     ("PAXG 15m 22-24",Panel("PAXGUSDT","15m",(2022,2023,2024))),
     ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026)))]

print("=== what the windows are (in-session bars only) ===")
p=PAN[0][1]; d=p.d[p.d.in_session]
allrows=[]
for nm,m in [("A 06-09 MYT (22-01 UTC)",winA(d)),("B 19-22 MYT (11-14 UTC)",winB(d)),
             ("A+B",both(d)),("rest of day",~both(d))]:
    g=d[m]
    allrows.append(dict(window=nm, bars=len(g), pct=round(100*len(g)/len(d),1),
                        atr_pct=round((g.atr/g.close).median()*100,4),
                        turnover_med=int(g.turnover.median()),
                        rel_vol=round(g.relative_volume.median(),2)))
print(pd.DataFrame(allrows).to_string(index=False))

print("\n=== does the BASE rule survive inside the windows? (symmetric 2.5/2.5, null 49.8) ===")
rows=[]
for lbl,p in PAN:
    bl,bs=p.base(); d=p.d
    for wn,wf in [("all day",lambda x: pd.Series(True,index=x.index)),
                  ("A only",winA),("B only",winB),("A+B",both)]:
        w=wf(d)
        r=simulate(p,bl&w,bs&w,2.5,2.5,f"{lbl} | {wn}")
        if r: rows.append(dict(panel=lbl, window=wn, **{k:r.row()[k] for k in
                    ["n","wr","ci","wrL","wrS","be","edge","pf","per_day"]}))
print(pd.DataFrame(rows).to_string(index=False))
