"""The user's hypothesis: 5m AND 15m flip BEARISH while 1h/4h stay BULLISH -> BUY.

This is counter-trend dip buying and is the mirror image of the base rule on the
fast timeframes, so it is a STANDALONE archetype: it carries its own direction and
the right comparator is the 49.8% pooled null, not the base rule.
"""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate, NULL_WR
pd.set_option("display.width",260); pd.set_option("display.max_columns",40); pd.set_option("display.max_rows",300)

PAN=[("XAU 15m",Panel("XAUUSDT","15m")),
     ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
     ("PAXG 15m 22-24",Panel("PAXGUSDT","15m",(2022,2023,2024))),
     ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026))),
     ("BTC 15m",Panel("BTCUSDT","15m")),
     ("ETH 15m",Panel("ETHUSDT","15m"))]

def variants(d):
    """(name, long_mask, short_mask). LONG = fast bearish, slow bullish."""
    V={}
    fastL = (d.bias_5m==-1)&(d.bias_15m==-1)      # both fast TFs SOLD -> we buy
    fastS = (d.bias_5m== 1)&(d.bias_15m== 1)
    V["1h bull gate"]              = (fastL&(d.bias_1h== 1),            fastS&(d.bias_1h==-1))
    V["4h bull gate"]              = (fastL&(d.bias_4h== 1),            fastS&(d.bias_4h==-1))
    V["1h AND 4h gate"]            = (fastL&(d.bias_1h==1)&(d.bias_4h==1),
                                      fastS&(d.bias_1h==-1)&(d.bias_4h==-1))
    V["1h+4h + 30m still bull"]    = (fastL&(d.bias_1h==1)&(d.bias_4h==1)&(d.bias_30m==1),
                                      fastS&(d.bias_1h==-1)&(d.bias_4h==-1)&(d.bias_30m==-1))
    V["1h+4h + 4H stack"]          = (fastL&(d.bias_1h==1)&(d.bias_4h==1)&(d["4h_ema_alignment"]==1),
                                      fastS&(d.bias_1h==-1)&(d.bias_4h==-1)&(d["4h_ema_alignment"]==-1))
    # freshness of the two fast flips -- "the two red arrows just appeared"
    for k in [15,30,60,120]:
        V[f"1h+4h + both flips <={k}m"] = (
            fastL&(d.bias_1h==1)&(d.bias_4h==1)&(d.age_5m<=k)&(d.age_15m<=k),
            fastS&(d.bias_1h==-1)&(d.bias_4h==-1)&(d.age_5m<=k)&(d.age_15m<=k))
    # does it need a reclaim confirmation like the base rule?
    recL=(d.close>d.close.shift(1)); recS=(d.close<d.close.shift(1))
    V["1h+4h + up-close confirm"]  = (fastL&(d.bias_1h==1)&(d.bias_4h==1)&recL,
                                      fastS&(d.bias_1h==-1)&(d.bias_4h==-1)&recS)
    # the 15m alone bearish (is the 5m leg needed at all?)
    V["15m only bearish +1h4h"]    = ((d.bias_15m==-1)&(d.bias_1h==1)&(d.bias_4h==1),
                                      (d.bias_15m== 1)&(d.bias_1h==-1)&(d.bias_4h==-1))
    V["5m only bearish +1h4h"]     = ((d.bias_5m==-1)&(d.bias_1h==1)&(d.bias_4h==1),
                                      (d.bias_5m== 1)&(d.bias_1h==-1)&(d.bias_4h==-1))
    return V

rows=[]
for lbl,p in PAN:
    d=p.d
    for nm,(L,S) in variants(d).items():
        r=simulate(p,L.fillna(False),S.fillna(False),2.5,2.5,nm)
        if r: rows.append(dict(panel=lbl, rule=nm, **{k:r.row()[k] for k in
                    ["n","wr","ci","wrL","wrS","be","edge","pf","per_day"]}))
    b=simulate(p,*p.base(),2.5,2.5,"BASE for reference")
    if b: rows.append(dict(panel=lbl, rule="== BASE RULE ==", **{k:b.row()[k] for k in
                    ["n","wr","ci","wrL","wrS","be","edge","pf","per_day"]}))
o=pd.DataFrame(rows)
print(f"null = {NULL_WR}%  (standalone archetype: beat the NULL, not the base)")
for lbl,_ in PAN:
    print(f"\n### {lbl}")
    print(o[o.panel==lbl].drop(columns=["panel"]).to_string(index=False))
