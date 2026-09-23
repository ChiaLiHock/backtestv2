"""My own independent read of the user's specific question, to cross-check the miner."""
import sys
import numpy as np, pandas as pd
SP=r"C:\Users\User\AppData\Local\Temp\claude\C--inetpub-Claude-ITSupport\528ea622-2ee0-450a-85de-5fec246bcb72\scratchpad\mine"
sys.path.insert(0,SP)
from screen import Panel, screen, simulate
pd.set_option("display.width",260); pd.set_option("display.max_columns",40)

PANELS=[("XAU 15m",Panel("XAUUSDT","15m")),
        ("PAXG 15m 25-26",Panel("PAXGUSDT","15m",(2025,2026))),
        ("PAXG 1h 25-26",Panel("PAXGUSDT","1h",(2025,2026))),
        ("PAXG 15m 22-24 [hostile]",Panel("PAXGUSDT","15m",(2022,2023,2024)))]

C=[]
for t in [5,10,15,20,30,45,60,90]:
    C.append((f"age_5m<={t}m", f"d.age_5m<={t}", f"d.age_5m<={t}"))
for t in [15,30,60,120]:
    C.append((f"age_15m<={t}m", f"d.age_15m<={t}", f"d.age_15m<={t}"))
C.append(("age_5m<=10 & age_4h>=1440","(d.age_5m<=10)&(d.age_4h>=1440)","(d.age_5m<=10)&(d.age_4h>=1440)"))
C.append(("age_5m<=10 & age_15m>=60","(d.age_5m<=10)&(d.age_15m>=60)","(d.age_5m<=10)&(d.age_15m>=60)"))
C.append(("lag_5m_15m<=-60","d.lag_5m_15m<=-60","d.lag_5m_15m<=-60"))
C.append(("lag_5m_15m in [-60,0]","(d.lag_5m_15m>=-60)&(d.lag_5m_15m<=0)","(d.lag_5m_15m>=-60)&(d.lag_5m_15m<=0)"))
C.append(("cascade_fast_first","d.cascade_fast_first","d.cascade_fast_first"))
C.append(("(none: base only)","d.age_5m==d.age_5m","d.age_5m==d.age_5m"))

for lbl,p in PANELS:
    o=screen(p,C,p.base(),2.5,2.5)
    o=o[["rule","n","wr","ci","wrL","wrS","z","be","pf","per_day"]]
    print(f"\n### {lbl}  (null 49.8%,  {len(C)} tests)")
    print(o.to_string(index=False))
