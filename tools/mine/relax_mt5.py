"""The relaxation verdict, recomputed at MT5 GOLD cost instead of Bybit cost.

Win rates do not change with the fee. Break-even does. On Bybit the marginal
trades a relaxation buys lost by 13-17 points; the whole question is whether a
0.83 bps venue moves that verdict.
"""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
import screen
from screen import Panel, simulate
pd.set_option("display.width",250)

def be_at(atr, px, bps, tp=2.5, sl=2.5, slip=0.02):
    c = px*bps/10_000 + slip
    return (sl*atr + c)/((tp+sl)*atr)*100

PAN=[("XAU 15m",Panel("XAUUSDT","15m")),
     ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026)))]

print("=== break-even at 2.5/2.5, same bars, different venue ===")
for lbl,p in PAN:
    a=float(p.d.atr.median()); px=float(p.d.close.median())
    print(f"  {lbl:16} ATR={a:5.2f} px={px:7.0f}   Bybit(11bps)={be_at(a,px,11.0):5.1f}%"
          f"   MT5 GOLD(0.83bps)={be_at(a,px,0.83):5.1f}%")
print()

def build(p, n_min=5, use_event=True, use_4h_stack=True):
    d=p.d
    nb=sum((d[f"bias_{tf}"]==1).astype(int) for tf in ["5m","15m","30m","1h","4h"])
    ns=sum((d[f"bias_{tf}"]==-1).astype(int) for tf in ["5m","15m","30m","1h","4h"])
    L=(nb>=n_min); S=(ns>=n_min)
    if use_4h_stack: L&=(d["4h_ema_alignment"]==1); S&=(d["4h_ema_alignment"]==-1)
    if use_event:
        rl=lambda s,n: s.rolling(n,min_periods=1).max().astype(bool)
        L&=rl(d.low<d.ema_14,3)&(d.close>d.ema_14)&(d.close>d.close.shift(1))
        S&=rl(d.high>d.ema_14,3)&(d.close<d.ema_14)&(d.close<d.close.shift(1))
    return L.fillna(False), S.fillna(False)

VAR=[("base 5of5 + event + 4H stack",5,True,True),
     ("4 of 5 + event + 4H stack",4,True,True),
     ("3 of 5 + event + 4H stack",3,True,True),
     ("5of5 + 4H stack, NO event",5,False,True),
     ("4of5 + 4H stack, NO event",4,False,True),
     ("5of5 + event, no 4H stack",5,True,False)]

for lbl,p in PAN:
    a=float(p.d.atr.median()); px=float(p.d.close.median())
    be_mt5=be_at(a,px,0.83); be_byb=be_at(a,px,11.0)
    print(f"### {lbl}   break-even: Bybit {be_byb:.1f}%  ->  MT5 {be_mt5:.1f}%")
    rows=[]
    for nm,n,ev,st in VAR:
        for wn,wmask in [("all day",None),("A+B windows",p.window("AB"))]:
            L,S=build(p,n,ev,st)
            if wmask is not None: L=L&wmask; S=S&wmask
            r=simulate(p,L,S,2.5,2.5,nm)
            if not r: continue
            rows.append(dict(variant=nm, window=wn, n=r.n, wr=r.wr,
                             edge_bybit=round(r.wr-be_byb,1), edge_mt5=round(r.wr-be_mt5,1),
                             per_day=r.per_day, per_week=round(r.per_day*7,2)))
    o=pd.DataFrame(rows)
    print(o.to_string(index=False)); print()
