"""Which variant gets $150 to $1000 fastest, and with what probability?

More trades at a thinner edge is not automatically worse for a TARGET goal --
it is a different point on a real trade-off curve. This prices it.
"""
import numpy as np
rng=np.random.default_rng(11)
ATR=6.89; SPREAD=0.38; CONTRACT=100.0; LOT_MIN=0.01; LOT_STEP=0.01
SL=2.5; TP=2.5
risk_per_lot=SL*ATR*CONTRACT
win_px=TP*ATR-SPREAD; loss_px=SL*ATR+SPREAD

# (name, PAXG win rate = the honest planning number, trades per week all-day, per week in windows)
VAR=[("base 5of5+event+stack",      0.566, 5.60, 2.87),
     ("5of5, NO event",             0.554,10.92, 5.25),
     ("4of5+event+stack",           0.536,11.06, 5.74),
     ("4of5, NO event",             0.540,17.15, 9.17)]

def run(start,f,wr,target,max_tr=600):
    eq=start
    for t in range(max_tr):
        L=np.floor(((eq*f)/risk_per_lot)/LOT_STEP)*LOT_STEP
        if L<LOT_MIN-1e-9: return "stuck",eq,t
        eq+=(win_px if rng.random()<wr else -loss_px)*L*CONTRACT
        if eq>=target: return "target",eq,t+1
        if eq<LOT_MIN*risk_per_lot: return "bust",eq,t+1
    return "timeout",eq,max_tr

for start in [150.0,200.0]:
    print(f"\n{'='*96}\nSTART ${start:.0f}  ->  TARGET $1000   (win rate = the honest PAXG number, MT5 costs)")
    print(f"{'variant':26} {'risk':>5} {'P(reach 1k)':>12} {'P(stuck/bust)':>14} {'med trades':>11} {'wks all-day':>12} {'wks windows':>12}")
    for nm,wr,tpw_all,tpw_win in VAR:
        for f in [0.15,0.20,0.25]:
            outs=[run(start,f,wr,1000.0) for _ in range(20000)]
            k=np.array([o[0] for o in outs]); ts=np.array([o[2] for o in outs])
            hit=np.mean(k=="target")
            med=np.median(ts[k=="target"]) if hit>0 else np.nan
            print(f"{nm:26} {f:>5.0%} {hit:>12.1%} {np.mean((k=='stuck')|(k=='bust')):>14.1%}"
                  f" {med:>11.0f} {med/tpw_all:>12.1f} {med/tpw_win:>12.1f}")
