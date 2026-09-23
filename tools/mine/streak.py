"""What does 'run 150 up to 1000 on a streak' actually imply? Monte Carlo, with
real lot granularity, real MT5 costs, and the rule's measured win rate."""
import numpy as np
rng=np.random.default_rng(7)

ATR=7.0; SL_ATR=2.5; TP_ATR=2.5
SPREAD=0.38          # MT5 GOLD measured median
CONTRACT=100.0; LOT_MIN=0.01; LOT_STEP=0.01
stop_px = SL_ATR*ATR                      # 17.5 per oz
risk_per_lot = stop_px*CONTRACT           # 1750 per lot
win_px  = TP_ATR*ATR - SPREAD             # 17.12
loss_px = SL_ATR*ATR + SPREAD             # 17.88

def lots_for(equity,f):
    raw=(equity*f)/risk_per_lot
    return np.floor(raw/LOT_STEP)*LOT_STEP

def run(start,f,wr,target,max_trades=400):
    eq=start
    for t in range(max_trades):
        L=lots_for(eq,f)
        if L<LOT_MIN-1e-9: return "stuck",eq,t
        pnl=(win_px if rng.random()<wr else -loss_px)*L*CONTRACT
        eq+=pnl
        if eq>=target: return "target",eq,t+1
        if eq< LOT_MIN*risk_per_lot: return "bust",eq,t+1
    return "timeout",eq,max_trades

print("P(10 consecutive wins) -- the spreadsheet's plan")
for wr in [0.50,0.566,0.62,0.682]:
    print(f"   win rate {wr:.1%}  ->  {wr**10:7.4%}   = 1 in {1/wr**10:,.0f}")
print()
print("A win at 2.5/2.5 pays +0.98R, not +1.5R. To grow +50% per trade at 1/3 risk")
print("you need +1.5R per win, i.e. TP = 1.5 x SL, where measured WR drops to ~46-52%.")
print(f"   at 50% win rate, 10 straight = {0.5**10:.4%} = 1 in {1/0.5**10:,.0f}")
print()

print("Monte Carlo: start $150, target $1000, MT5 GOLD costs, real 0.01 lot steps")
print(f"{'risk/trade':>11} {'reach $1000':>12} {'busted':>9} {'stuck':>8} {'median end':>11} {'med trades':>11}")
for f in [0.02,0.05,0.10,0.15,0.20,0.25,1/3,0.50]:
    outs=[run(150.0,f,0.566,1000.0) for _ in range(20000)]
    kinds=np.array([o[0] for o in outs]); eqs=np.array([o[1] for o in outs]); ts=np.array([o[2] for o in outs])
    print(f"{f:>10.0%} {np.mean(kinds=='target'):>12.1%} {np.mean(kinds=='bust'):>9.1%}"
          f" {np.mean(kinds=='stuck'):>8.1%} {np.median(eqs):>11.0f} {np.median(ts):>11.0f}")
print()
print("Same, but with the optimistic 68.2% (XAU 166-day) win rate:")
print(f"{'risk/trade':>11} {'reach $1000':>12} {'busted':>9} {'stuck':>8} {'median end':>11}")
for f in [0.05,0.10,0.15,0.20,0.25,1/3]:
    outs=[run(150.0,f,0.682,1000.0) for _ in range(20000)]
    kinds=np.array([o[0] for o in outs]); eqs=np.array([o[1] for o in outs])
    print(f"{f:>10.0%} {np.mean(kinds=='target'):>12.1%} {np.mean(kinds=='bust'):>9.1%}"
          f" {np.mean(kinds=='stuck'):>8.1%} {np.median(eqs):>11.0f}")
