"""Where the trades actually went: 2.7/week is a QUEUE limit, not a rule limit.

`measure()` runs `max_concurrent = 1` -- a candidate is dropped while a position is
open. At TP5.0 positions are held long, so the queue eats most of the signal list.
This asks what the same rule, unchanged, produces at 2/3/4/6 concurrent slots, and
what that does to a $200 account at a fixed 0.01 lot.

Concurrent longs in one instrument are NOT diversification -- three open longs in gold
is a 0.03 lot position with three different stops. So the equity path is walked in
true calendar order on the real sequence, and the bootstrap resamples 20-trade blocks
in calendar order rather than i.i.d., because i.i.d. would break exactly the clustering
that concurrency creates.
"""
import sys
import numpy as np
sys.path.insert(0, r"C:\inetpub\Claude\ITSupport")
from backtest.tools.validate_rule import (
    build_context, leg_masks, walk_forward, breakeven_wr)
from backtest.data.db import Database, CandleRepository, INTERVAL_MS
from backtest.indicators.base import IndicatorConfig
from backtest.engine import features as F
from datetime import datetime, timezone

TP, SL, COST, HORIZON = 5.0, 2.5, 0.85, 96
ATR_NOW, PX_NOW = 10.07, 4556.0
START, FLOOR, N_PATHS = 200.0, 60.0, 40000
RNG = np.random.default_rng(11)


def run(db, max_concurrent, side="long", tp=TP, sl=SL):
    cfg = IndicatorConfig()
    ctx, df, coverage = build_context(db, "MT5:GOLD", "15m", cfg, 3)
    ot = ctx.open_time
    opens = df["open"].to_numpy()
    atr = ctx.ind["atr_14"].to_numpy()
    usable = ctx.ind["usable"].to_numpy().astype(bool)
    repo = CandleRepository(db)
    pdf = repo.load("MT5:GOLD", "15m")
    pt = pdf["open_time"].to_numpy(dtype="int64")
    ph, pl, pc = (pdf[c].to_numpy() for c in ("high", "low", "close"))
    in_sess = np.array([F.is_gold_session(int(t)) for t in ot], dtype=bool)

    fire = np.ones(ot.size, bool)
    for m in leg_masks(ctx, side, False).values():
        fire &= m
    lo_bound = max(coverage.get(tf, ot[0]) for tf in ("4h", "1h", "30m", "15m"))
    fire &= usable & in_sess & (ot >= lo_bound) & (ot <= pt[-1])

    horizon_ms = HORIZON * INTERVAL_MS["15m"]
    open_until: list[int] = []          # exit times of live positions
    trades = []                          # (entry_ms, exit_ms, net_R)
    for i in np.flatnonzero(fire):
        if i + 1 >= ot.size:
            continue
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        t = int(ot[i])
        open_until = [x for x in open_until if x > t]
        if len(open_until) >= max_concurrent:
            continue
        e = float(opens[i + 1])
        if not np.isfinite(e):
            continue
        tpx, slx = (e + tp * a, e - sl * a) if side == "long" else (e - tp * a, e + sl * a)
        r, px, xt, _ = walk_forward(pt, ph, pl, pc, int(ot[i + 1]), e, tpx, slx,
                                    side == "long", horizon_ms)
        if r == "nodata":
            continue
        gross = (px - e) if side == "long" else (e - px)
        trades.append((int(ot[i + 1]), int(xt), (gross - e * COST / 1e4) / a))
        open_until.append(int(xt))
    return int(fire.sum()), trades


def walk(net_d, start=START):
    eq = start + np.cumsum(net_d)
    full = np.concatenate([[start], eq])
    peak = np.maximum.accumulate(full)
    streak = mx = 0
    for x in net_d:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    return float(eq[-1]), float((peak - full).max()), mx, float(full.min())


def block_mc(net_d, n_year, block=20):
    k = len(net_d)
    nb = int(np.ceil(n_year / block))
    st = RNG.integers(0, k, size=(N_PATHS, nb))
    idx = (st[:, :, None] + np.arange(block)[None, None, :]) % k
    eq = START + np.cumsum(net_d[idx.reshape(N_PATHS, -1)[:, :n_year]], axis=1)
    run_min = np.minimum.accumulate(eq, axis=1)[:, -1]
    return (float((run_min <= 0).mean() * 100), float((run_min <= FLOOR).mean() * 100),
            float(np.median(eq[:, -1])), float(np.percentile(eq[:, -1], 10)))


with Database() as db:
    be = breakeven_wr(TP, SL, ATR_NOW, PX_NOW * COST / 1e4)
    print(f"long-only TP{TP}/SL{SL}, break-even {be:.1f}%   dollars at 0.01 lot, ATR {ATR_NOW}\n")
    hdr = (f"{'slots':>5} {'trades':>7} {'/week':>6} {'win%':>6} {'exp$':>6} {'$/yr':>7} "
           f"{'$200 ->':>9} {'min eq':>8} {'maxDD':>7} {'strk':>5} "
           f"{'ruin':>6} {'floor':>6} {'med end':>8} {'p10':>7}")
    print(hdr); print("-" * len(hdr))
    for mc in (1, 2, 3, 4, 6):
        fired, tr = run(db, mc)
        tr.sort(key=lambda x: x[1])          # realised P/L lands at EXIT time
        net_d = np.array([x[2] for x in tr]) * ATR_NOW
        span_y = (tr[-1][0] - tr[0][0]) / (365.25 * 86400 * 1000)
        n_year = int(round(len(tr) / span_y))
        end, dd, strk, lowest = walk(net_d)
        ruin, floor, med, p10 = block_mc(net_d, n_year)
        print(f"{mc:5d} {len(tr):7d} {len(tr)/span_y/52:6.1f} {(net_d>0).mean()*100:6.1f} "
              f"{net_d.mean():6.2f} {net_d.mean()*n_year:7.0f} {end:9,.0f} {lowest:8,.0f} "
              f"{dd:7,.0f} {strk:5d} {ruin:5.1f}% {floor:5.1f}% {med:8,.0f} {p10:7,.0f}")
    print(f"\nlong signals fired in the panel: {fired}"
          f"  ({fired/span_y/52:.1f}/week)  <- the rule's true firing rate")
