"""Signal 3 research harness v2.

Replaces backtest_signal3.py. Same engine functions (single source of
truth: backtest.engine.signal3), but:

* every result is in R (net of cost / stop distance), not dollars;
* one position at a time (the old walker stacked overlapping trades from
  neighbouring swings; live has one account, not one per swing);
* gap-safe entry: skip when the next 15m bar is not contiguous, or the
  entry minute does not exist, instead of entering on stale data;
* folded bars are dropped when incomplete (a bar with missing minutes has
  an under-counted volume and looks falsely "quiet");
* TP/SL sanity: the fallback TP (swing +/- 3x zone) can land on the wrong
  side of a late entry, and abs() then let it pass the rr floor;
* parameters are injected by patching engine module constants, so the
  walk is still the live machine;
* the optimizer is a rolling walk-forward: pick params on the TRAIN
  window by the bootstrap lower bound of expectancy, score them once on
  the next TEST window, report only the stitched out-of-sample result.

Usage
  python backtest_signal3_v2.py baseline [--synthetic]
  python backtest_signal3_v2.py ab        --symbol PAXG
  python backtest_signal3_v2.py optimize  --symbol PAXG --train 365 --test 90 --step 90
  --synthetic runs everything on a random walk: a NULL test. A sound
  pipeline must show ~ -cost there. If it shows an edge, it has a leak.
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import sys

import numpy as np

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")

import backtest.engine.signal3 as s3                      # noqa: E402
from backtest.tools.validate_rule import walk_forward     # noqa: E402

HORIZON = 24 * 3600_000
STEP = 900_000                       # 15m
COST = 11e-4                         # round trip, Bybit non-VIP taker x2
MIN_TRADES = 30                      # a train window below this is not scored
SYMBOLS = {                          # key -> (db symbol, timeframe, ms/bar)
    "XAU": ("XAUUSDT", "1m", 60_000),
    "MT5": ("MT5:GOLD", "5m", 300_000),
    "PAXG": ("PAXGUSDT", "5m", 300_000),
}
# Small on purpose: every extra axis is another way to overfit.
GRID = {
    "QUIET_VOL_RATIO_MAX": [0.6, 0.8, 1.0],
    "SL_ZONE_MULT": [0.75, 1.0, 1.5],
    "MIN_RR": [0.5, 1.0],
}
AB_VARIANTS = {
    "legacy": {},
    "extreme_full": {"EXTREME_TRACK_FULL": True},
    "loud_invalidates": {"INVALIDATE_ON_LOUD_BREAK": True},
    "both": {"EXTREME_TRACK_FULL": True, "INVALIDATE_ON_LOUD_BREAK": True},
}


# --------------------------------------------------------------- data ----
def fold(df, step, base):
    """Fold to `step`, keeping only COMPLETE bars (all base bars present)."""
    need = step // base
    key = ((df["open_time"] // step) * step).astype("int64")
    g = df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), n=("open_time", "size"))
    return g[g["n"] == need].reset_index(drop=True)


def synthetic_1m(days=700, seed=7):
    import pandas as pd
    rng = np.random.default_rng(seed)
    n = days * 1440
    close = 3000 + np.cumsum(rng.normal(0, 1.6, n))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.5, (n, 2)))
    return pd.DataFrame({
        "open_time": 1_700_000_000_000 // 60_000 * 60_000
                     + np.arange(n) * 60_000,
        "open": open_,
        "high": np.maximum(open_, close) + wick[:, 0],
        "low": np.minimum(open_, close) - wick[:, 1],
        "close": close,
        "volume": rng.lognormal(3.0, 0.6, n)})


def load(key, synthetic=False):
    sym, tf, base = SYMBOLS[key]
    if synthetic:
        d = synthetic_1m()
        base = 60_000
    else:
        from backtest.data.db import Database, CandleRepository
        with Database() as db:
            d = CandleRepository(db).load(sym, tf).reset_index(drop=True)
    d = d.sort_values("open_time").reset_index(drop=True)
    return {
        "f15": fold(d, STEP, base),
        "pt": d["open_time"].to_numpy("int64"),
        "ph": d["high"].to_numpy("float64"),
        "pl": d["low"].to_numpy("float64"),
        "pc": d["close"].to_numpy("float64"),
        "po": d["open"].to_numpy("float64"),
    }


# ------------------------------------------------------------- params ----
@contextlib.contextmanager
def patched(**kw):
    """Temporarily set engine module constants."""
    missing = [k for k in kw if not hasattr(s3, k)]
    if missing:
        raise SystemExit(f"engine has no {missing}: apply the patched "
                         f"signal3.py first")
    old = {k: getattr(s3, k) for k in kw}
    try:
        for k, v in kw.items():
            setattr(s3, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(s3, k, v)


# ------------------------------------------------------------- walker ----
def collect_trades(D, cost=COST):
    f = D["f15"]
    pt, ph, pl, pc, po = (D[k] for k in ("pt", "ph", "pl", "pc", "po"))
    t = f["open_time"].to_numpy("int64")
    h = f["high"].to_numpy("float64")
    l = f["low"].to_numpy("float64")
    c = f["close"].to_numpy("float64")
    v = f["volume"].to_numpy("float64")
    n = len(t)
    L = s3.SWING_LOOKBACK
    sh_idx, sl_idx = s3.detect_swings(h, l, n=L)
    rows = []
    for side, swings, opp in (("low", sl_idx, sh_idx),
                              ("high", sh_idx, sl_idx)):
        is_long = side == "low"
        for abs_i in swings:
            abs_i = int(abs_i)
            if abs_i < L or abs_i > n - 6:
                continue
            sp = float(l[abs_i] if is_long else h[abs_i])
            start = abs_i + L
            r = s3.run_state_machine(
                swing_price=sp, side=side, h=h, l=l, c=c, v=v,
                start=start, end=min(start + s3.SWING_TAIL_BARS, n))
            if r is None:
                continue
            state, j, reason = r
            if not state.ready or reason is not None:
                continue
            # entry = first minute after the return bar CLOSES; skip gaps
            if j + 1 >= n or t[j + 1] != t[j] + STEP:
                continue
            entry_t = int(t[j + 1])
            k = int(np.searchsorted(pt, entry_t, "left"))
            if k >= len(pt) or int(pt[k]) != entry_t:
                continue
            entry = float(po[k])
            sl, tp = s3.sweep_bracket(state, entry)
            conf = opp[(opp > abs_i) & (opp <= j - L)]
            near = s3.nearest_opposing_swing(entry, is_long, conf, h, l)
            if near is not None:
                tp = near
            risk = abs(entry - sl)
            # both legs must sit on the correct side of entry
            if risk <= 0 or (sl >= entry if is_long else sl <= entry):
                continue
            if tp <= entry if is_long else tp >= entry:
                continue
            if abs(tp - entry) < s3.MIN_RR * risk:
                continue
            why, px, xt, _amb = walk_forward(
                pt, ph, pl, pc, entry_t, entry, tp, sl, is_long, HORIZON)
            if why == "nodata":
                continue
            if not (why in ("tp", "sl")
                    or int(pt[-1]) >= entry_t + HORIZON):
                continue                      # still open at end of data
            pnl = (float(px) - entry) if is_long else (entry - float(px))
            net = pnl - entry * cost
            try:
                exit_t = int(xt)
                if exit_t < entry_t:
                    exit_t = None
            except (TypeError, ValueError):
                exit_t = None
            rows.append({"entry_time": entry_t, "exit_time": exit_t,
                         "side": "long" if is_long else "short",
                         "zone_width": state.zone_width, "risk": risk,
                         "r": net / risk, "why": why})
    rows.sort(key=lambda x: x["entry_time"])
    return rows


def one_position(rows):
    if any(x["exit_time"] is None for x in rows):
        print("  ! walk_forward's 3rd return value is not an epoch-ms exit "
              "time: one-position filter DISABLED (results overlap)")
        return rows
    out, busy = [], -1
    for x in rows:
        if x["entry_time"] < busy:
            continue
        out.append(x)
        busy = max(x["exit_time"], x["entry_time"])
    return out


# ------------------------------------------------------------ metrics ----
def boot_lo(x, nb=2000, seed=0):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    return float(np.percentile(rng.choice(x, (nb, len(x))).mean(1), 2.5))


def metrics(rows):
    if not rows:
        return {"n": 0, "win": float("nan"), "expR": float("nan"),
                "pf": float("nan"), "sumR": 0.0, "dd": 0.0,
                "lo95": float("nan")}
    r = np.array([x["r"] for x in sorted(rows,
                                         key=lambda x: x["entry_time"])])
    gl = -r[r < 0].sum()
    eq = np.cumsum(r)
    return {"n": len(r), "win": 100 * float((r > 0).mean()),
            "expR": float(r.mean()),
            "pf": float(r[r > 0].sum() / gl) if gl > 0 else float("inf"),
            "sumR": float(r.sum()),
            "dd": float((np.maximum.accumulate(eq) - eq).max()),
            "lo95": boot_lo(r)}


def line(tag, m):
    return (f"  {tag:<24s} n={m['n']:4d} win={m['win']:5.1f}% "
            f"expR={m['expR']:+.3f} pf={m['pf']:5.2f} sumR={m['sumR']:+8.1f} "
            f"maxDD={m['dd']:6.1f}R lo95={m['lo95']:+.3f}")


def report(title, rows):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(line("ALL", metrics(rows)))
    print(line("LONG", metrics([x for x in rows if x["side"] == "long"])))
    print(line("SHORT", metrics([x for x in rows if x["side"] == "short"])))
    if len(rows) >= 8:
        mid = rows[len(rows) // 2]["entry_time"]
        print(line("first half", metrics([x for x in rows
                                          if x["entry_time"] < mid])))
        print(line("second half", metrics([x for x in rows
                                           if x["entry_time"] >= mid])))


# ----------------------------------------------------------- commands ----
def cmd_baseline(a):
    for key in (["XAU", "MT5", "PAXG"] if not a.synthetic else ["XAU"]):
        D = load(key, a.synthetic)
        raw = collect_trades(D)
        report(f"{key} baseline (one position, R, cost "
               f"{COST * 1e4:.0f}bp)  raw={len(raw)}", one_position(raw))


def cmd_ab(a):
    D = load(a.symbol, a.synthetic)
    print(f"A/B of engine switches on {a.symbol} (default params)")
    for name, kw in AB_VARIANTS.items():
        with patched(**kw):
            print(line(name, metrics(one_position(collect_trades(D)))))


def cmd_optimize(a):
    D = load(a.symbol, a.synthetic)
    fixed = dict(AB_VARIANTS[a.variant])
    names = list(GRID)
    combos = list(itertools.product(*GRID.values()))
    default = tuple(getattr(s3, k) for k in names)
    if default not in combos:
        raise SystemExit(f"engine defaults {default} not in GRID")
    print(f"{a.symbol}: {len(combos)} combos, engine variant "
          f"'{a.variant}', train/test/step = {a.train}/{a.test}/{a.step}d")

    book = {}
    for cb in combos:
        with patched(**fixed, **dict(zip(names, cb))):
            book[cb] = one_position(collect_trades(D))
    day = 86_400_000
    t0 = int(D["pt"][0])
    tN = int(D["pt"][-1])

    def win(rows, lo, hi):
        return [x for x in rows if lo <= x["entry_time"] < hi]

    if a.table:
        print("\nIN-SAMPLE full-history table (a picture of the plateau, "
              "NOT a result):")
        for cb in sorted(combos, key=lambda c: -metrics(book[c])["lo95"]
                         if book[c] else 9):
            print(line(str(cb), metrics(book[cb])))

    oos, oos_default, chosen = [], [], []
    s = t0
    while s + (a.train + a.test) * day <= tN:
        tr_hi = s + a.train * day
        te_hi = tr_hi + a.test * day
        scores = {}
        for cb in combos:
            m = metrics(win(book[cb], s, tr_hi))
            scores[cb] = m["lo95"] if m["n"] >= MIN_TRADES else -9.0
        best = max(scores, key=scores.get)
        if scores[best] == -9.0:
            print(f"  fold {len(chosen) + 1}: no combo has "
                  f">={MIN_TRADES} train trades — skipped")
        else:
            te = win(book[best], tr_hi, te_hi)
            rank = 1 + sum(
                1 for cb in combos
                if metrics(win(book[cb], tr_hi, te_hi))["expR"]
                > metrics(te)["expR"])
            mt = metrics(te)
            print(f"  fold {len(chosen) + 1}: pick {best} train lo95="
                  f"{scores[best]:+.3f} -> TEST n={mt['n']} "
                  f"expR={mt['expR']:+.3f} (rank {rank}/{len(combos)})")
            oos += te
            oos_default += win(book[default], tr_hi, te_hi)
            chosen.append(best)
        s += a.step * day

    if not chosen:
        print("no scorable folds — not enough data/trades for this grid")
        return
    print()
    print(line("OOS optimized", metrics(oos)))
    print(line("OOS engine-default", metrics(oos_default)))
    print(f"  distinct picks across {len(chosen)} folds: "
          f"{len(set(chosen))} — if this is close to the fold count, "
          f"there is no stable optimum")
    if metrics(oos)["n"] < 100:
        print("  ! fewer than 100 OOS trades: read nothing into this")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["baseline", "ab", "optimize"])
    p.add_argument("--symbol", default="PAXG", choices=list(SYMBOLS))
    p.add_argument("--train", type=int, default=365)
    p.add_argument("--test", type=int, default=90)
    p.add_argument("--step", type=int, default=90)
    p.add_argument("--variant", default="legacy", choices=list(AB_VARIANTS))
    p.add_argument("--table", action="store_true")
    p.add_argument("--synthetic", action="store_true")
    a = p.parse_args()
    {"baseline": cmd_baseline, "ab": cmd_ab,
     "optimize": cmd_optimize}[a.cmd](a)


if __name__ == "__main__":
    main()
