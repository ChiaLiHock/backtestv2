"""My own check of the two claims the whole verdict rests on."""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, simulate
pd.set_option("display.width",250)

PANELS=[("XAU 15m",Panel("XAUUSDT","15m")),
        ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
        ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026)))]

print("=== CLAIM 1: divvol is a pure interaction corner with NO main effects ===")
print("    (if B-only and D-only sit BELOW base, there is no mechanism, only a lucky cell)\n")
for lbl,p in PANELS:
    d=p.d; bl,bs=p.base()
    B_L=d["4h_div_macd_hid_bull"].astype(bool); B_S=d["4h_div_macd_hid_bear"].astype(bool)
    D  =(d["4h_relative_volume"]>=1.0).fillna(False)
    cells={"base (all)":(bl,bs),
           "B&D  (the finding)":(bl&B_L&D, bs&B_S&D),
           "B only, not D":(bl&B_L&~D, bs&B_S&~D),
           "D only, not B":(bl&~B_L&D, bs&~B_S&D),
           "neither":(bl&~B_L&~D, bs&~B_S&~D)}
    print(f"--- {lbl}")
    for nm,(L,S) in cells.items():
        r=simulate(p,L,S,2.5,2.5,nm)
        if r: print(f"    {nm:20} n={r.n:5} wr={r.wr:5} L={r.wrL:5} S={r.wrS:5} pf={r.pf:5} be={r.be}")
    print()

print("=== CLAIM 2: the right comparator is base's OWN other trades, not the 49.8 null ===")
print("    two-proportion z of (cell) vs (base minus cell)\n")
from math import sqrt, erfc
for lbl,p in PANELS:
    d=p.d; bl,bs=p.base()
    B_L=d["4h_div_macd_hid_bull"].astype(bool); B_S=d["4h_div_macd_hid_bear"].astype(bool)
    D=(d["4h_relative_volume"]>=1.0).fillna(False)
    inn=simulate(p,bl&B_L&D, bs&B_S&D,2.5,2.5,"in")
    out=simulate(p,bl&~(B_L&D), bs&~(B_S&D),2.5,2.5,"out")
    if not(inn and out): continue
    p1,p2=inn.wr/100,out.wr/100; n1,n2=inn.n,out.n
    pp=(p1*n1+p2*n2)/(n1+n2)
    z=(p1-p2)/sqrt(pp*(1-pp)*(1/n1+1/n2))
    print(f"  {lbl:16} in: n={n1:4} wr={inn.wr:5} | out: n={n2:4} wr={out.wr:5} "
          f"| z_vs_base={z:5.2f}  p={erfc(abs(z)/sqrt(2)):.3f}   (z_vs_null was {inn.z})")
