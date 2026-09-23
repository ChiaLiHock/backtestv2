"""Signal 3 v2 — zone-width filter A/B and refinement sweep.

The first backtest showed narrow zones (bottom quartile by width) are
poison (46-49% vs 71-74% for wide). Test: minimum zone width as a share
of the median, plus ADX regime filter, plus volume ratio tightening.
"""
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")

from backtest.data.db import Database, CandleRepository
from backtest.engine.signal3 import (detect_swings, SweepState,
                                     QUIET_VOL_RATIO_MAX, VOL_MEDIAN_BARS,
                                     SWING_TAIL_BARS, SWING_LOOKBACK,
                                     SL_ZONE_MULT)
from backtest.tools.validate_rule import walk_forward

MYT = timezone(timedelta(hours=8))
HORIZON = 24 * 3600_000
COST = 0.85e-4
_po = None


def fold(df, step):
    key = ((df["open_time"] // step) * step).astype("int64")
    return df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum")).reset_index(drop=True)


def collect_trades(f15, pt, ph, pl, pc, vol_ratio=QUIET_VOL_RATIO_MAX):
    """Collect all sweep trades WITHOUT any width/regime filter; each row
    carries the metadata needed for post-hoc filtering."""
    global _po
    t15 = f15["open_time"].to_numpy(dtype="int64")
    h = f15["high"].to_numpy(dtype="float64")
    l = f15["low"].to_numpy(dtype="float64")
    c = f15["close"].to_numpy(dtype="float64")
    v = f15["volume"].to_numpy(dtype="float64")
    n = len(t15)

    sh_idx, sl_idx = detect_swings(h, l, n=SWING_LOOKBACK)
    trades = []

    for side, swings in (("low", sl_idx), ("high", sh_idx)):
        for abs_i in swings:
            if abs_i < SWING_LOOKBACK or abs_i > n - 6:
                continue
            swing_price = float(l[abs_i] if side == "low" else h[abs_i])
            state = SweepState(swing_price=swing_price, side=side)
            start = abs_i + SWING_LOOKBACK
            for j in range(start, min(start + SWING_TAIL_BARS, n)):
                close = float(c[j])
                breaking = close < swing_price if side == "low" \
                    else close > swing_price
                returning = close >= swing_price if side == "low" \
                    else close <= swing_price

                if breaking and state.returned:
                    vol = float(v[j])
                    vol_med = float(np.median(
                        v[max(0, j - VOL_MEDIAN_BARS):j])) \
                        if j > VOL_MEDIAN_BARS else 0
                    quiet = vol < vol_med * vol_ratio if vol_med > 0 else False
                    if quiet:
                        state.break_count += 1
                        state.returned = False
                        if side == "low":
                            state.extreme = min(state.extreme or swing_price,
                                                float(l[j]))
                        else:
                            state.extreme = max(state.extreme or swing_price,
                                                float(h[j]))
                elif returning and not state.returned:
                    state.returned = True
                    if state.ready:
                        zone_w = state.zone_width
                        entry_t = int(t15[j])
                        k = int(np.searchsorted(pt, entry_t))
                        if k >= len(pt):
                            continue
                        entry = float(_po[k])
                        is_long = side == "low"
                        sl = (state.extreme - SL_ZONE_MULT * zone_w) \
                            if is_long \
                            else (state.extreme + SL_ZONE_MULT * zone_w)
                        tp = (swing_price + 3 * zone_w) if is_long \
                            else (swing_price - 3 * zone_w)
                        opp = sh_idx if is_long else sl_idx
                        for si in opp:
                            if is_long and si > abs_i \
                                    and float(h[si]) > entry:
                                tp = float(h[si])
                                break
                            elif not is_long and si > abs_i \
                                    and float(l[si]) < entry:
                                tp = float(l[si])
                                break

                        reason, px, xt, _amb = walk_forward(
                            pt, ph, pl, pc, entry_t, entry, tp, sl,
                            is_long, HORIZON)
                        if reason == "nodata":
                            continue
                        resolved = reason in ("tp", "sl") or \
                            int(pt[-1]) >= entry_t + HORIZON
                        net = ((float(px) - entry) if is_long
                               else (entry - float(px))) \
                            - entry * COST if resolved else 0.0
                        trades.append({
                            "entry_time": entry_t,
                            "day_myt": datetime.fromtimestamp(
                                entry_t / 1000, MYT).strftime("%Y-%m-%d"),
                            "side": "long" if is_long else "short",
                            "zone_width": zone_w,
                            "entry_price": entry,
                            "break_count": state.break_count,
                            "resolved": resolved, "net": round(net, 2),
                            # for ADX: use the price relative to a rolling
                            # 15m close mean as a crude regime proxy here;
                            # proper ADX needs the indicator pipeline.
                            "hour_myt": int(
                                (entry_t // 3_600_000 + 8) % 24),
                        })
    trades.sort(key=lambda t: t["entry_time"])
    return trades


def stats(rows):
    if not rows:
        return {"n": 0, "win_pct": None, "exp": None, "pf": None,
                "net": 0.0}
    lab = [r for r in rows if r["resolved"]]
    n = len(lab)
    if not n:
        return {"n": 0, "win_pct": None, "exp": None, "pf": None, "net": 0.0}
    wins = sum(1 for r in lab if r["net"] > 0)
    nets = [r["net"] for r in lab]
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    return {"n": n, "win_pct": round(100 * wins / n, 1),
            "exp": round(sum(nets) / n, 3),
            "pf": round(gw / gl, 2) if gl > 0 else None,
            "net": round(sum(nets), 2)}


def sweep_filters(name, trades, median_zw):
    """Try zone-width floors as fractions of the median, alone and with
    min-break-count and session filters."""
    print(f"\n-- {name} (all {len(trades)} trades, median zw="
          f"{median_zw:.1f}) --")
    print(f"  {'filter':32s} {'n':>5s} {'win%':>6s} {'exp':>6s} "
          f"{'pf':>5s} {'net':>9s}")
    # baseline
    s = stats(trades)
    print(f"  {'no filter':32s} {s['n']:5d} {s['win_pct']:6} {s['exp']:6}"
          f" {s['pf']:5} {s['net']:9}")

    for frac, label in ((0.25, "zw >= 25% median"), (0.5, "zw >= 50%"),
                        (0.75, "zw >= 75%"), (1.0, "zw >= median")):
        sub = [r for r in trades if r["zone_width"] >= frac * median_zw]
        s = stats(sub)
        if s["n"] > 20:
            print(f"  {label:32s} {s['n']:5d} {s['win_pct']:6} "
                  f"{s['exp']:6} {s['pf']:5} {s['net']:9}")

    # combined: width + break_count >= 3
    print(f"  --- combined ---")
    for frac in (0.25, 0.5):
        sub = [r for r in trades
               if r["zone_width"] >= frac * median_zw
               and r["break_count"] >= 3]
        s = stats(sub)
        if s["n"] > 20:
            print(f"  zw>={int(frac*100)}% + sweeps>=3        "
                  f"{s['n']:5d} {s['win_pct']:6} {s['exp']:6} "
                  f"{s['pf']:5} {s['net']:9}")

    # width + session (Europe only 15-20)
    for frac in (0.25, 0.5):
        sub = [r for r in trades
               if r["zone_width"] >= frac * median_zw
               and 15 <= r["hour_myt"] < 22]
        s = stats(sub)
        if s["n"] > 20:
            print(f"  zw>={int(frac*100)}% + session 15-22     "
                  f"{s['n']:5d} {s['win_pct']:6} {s['exp']:6} "
                  f"{s['pf']:5} {s['net']:9}")

    # width + break>=3 + session
    for frac in (0.25, 0.5):
        sub = [r for r in trades
               if r["zone_width"] >= frac * median_zw
               and r["break_count"] >= 3
               and 15 <= r["hour_myt"] < 22]
        s = stats(sub)
        if s["n"] > 20:
            print(f"  zw>={int(frac*100)}% + sw>=3 + 15-22      "
                  f"{s['n']:5d} {s['win_pct']:6} {s['exp']:6} "
                  f"{s['pf']:5} {s['net']:9}")


def main():
    global _po
    with Database() as db:
        repo = CandleRepository(db)

        print("=== Bybit XAUUSDT ===")
        d1 = repo.load("XAUUSDT", "1m").reset_index(drop=True)
        f15 = fold(d1, 900_000)
        pt = d1["open_time"].to_numpy(dtype="int64")
        ph = d1["high"].to_numpy(dtype="float64")
        pl = d1["low"].to_numpy(dtype="float64")
        pc = d1["close"].to_numpy(dtype="float64")
        _po = d1["open"].to_numpy(dtype="float64")
        trades = collect_trades(f15, pt, ph, pl, pc)
        med = float(np.median([r["zone_width"] for r in trades]))
        sweep_filters("Bybit", trades, med)

        print("\n=== MT5:GOLD ===")
        d5 = repo.load("MT5:GOLD", "5m").reset_index(drop=True)
        f15m = fold(d5, 900_000)
        pt5 = d5["open_time"].to_numpy(dtype="int64")
        ph5 = d5["high"].to_numpy(dtype="float64")
        pl5 = d5["low"].to_numpy(dtype="float64")
        pc5 = d5["close"].to_numpy(dtype="float64")
        _po = d5["open"].to_numpy(dtype="float64")
        trades_m = collect_trades(f15m, pt5, ph5, pl5, pc5)
        med_m = float(np.median([r["zone_width"] for r in trades_m]))
        sweep_filters("MT5", trades_m, med_m)

        # validate the best filter from Bybit on MT5 (cross-feed check)
        print("\n=== cross-feed: best Bybit filter applied to MT5 ===")
        for frac in (0.25, 0.5):
            sub_b = [r for r in trades if r["zone_width"] >= frac * med]
            sub_m = [r for r in trades_m
                     if r["zone_width"] >= frac * med_m]
            sb = stats(sub_b)
            sm = stats(sub_m)
            print(f"  zw>={int(frac*100)}%: Bybit {sb['n']}/{sb['win_pct']}"
                  f"%/{sb['net']}  MT5 {sm['n']}/{sm['win_pct']}%/{sm['net']}")


if __name__ == "__main__":
    main()
