"""Signal 3 v2 backtest — the double-sweep/fake-break + ExpD fallback.

Walks the full history of XAUUSDT (1m→15m) and MT5:GOLD (5m→15m), runs
the sweep state machine over every swing, resolves outcomes, and reports
by year, direction, and mode (sweep vs ExpD fallback).
"""
import sys
import collections
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")

from backtest.data.db import Database, CandleRepository
from backtest.engine.signal3 import (detect_swings,
                                     run_state_machine, sweep_bracket,
                                     nearest_opposing_swing, MIN_RR,
                                     SWING_TAIL_BARS, SWING_LOOKBACK,
                                     MIN_ZONE_WIDTH_USD)
from backtest.tools.validate_rule import walk_forward

MYT = timezone(timedelta(hours=8))
HORIZON = 24 * 3600_000
# Cost disabled at the owner's request (2026-09-23): read the gross edge.
COST = 0.0


def fold(df, step):
    key = ((df["open_time"] // step) * step).astype("int64")
    return df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum")).reset_index(drop=True)


def run_sweep_trades(h15, pt, ph, pl, pc):
    """Walk all swings, run the ENGINE's state machine, resolve outcomes.

    Alignment notes (2026-09-23) — the walker used to be a hand-copied
    variant of the live machine and the two had drifted apart:
    * it now calls `run_state_machine` / `sweep_bracket` /
      `nearest_opposing_swing` from engine/signal3, so the walk IS the
      live logic, one fire per swing (the old re-fire loop produced the
      break_count=3/4 buckets live never emits);
    * the quiet-volume reference is a ROLLING per-bar median (what the
      old walker did) instead of one median taken at the last bar;
    * TP is the nearest opposing swing CONFIRMED by the entry bar — a
      swing that only forms after the entry was lookahead, and the old
      first-match loop took the OLDEST swing, not the nearest.
    """
    t15 = h15["open_time"].to_numpy(dtype="int64")
    h = h15["high"].to_numpy(dtype="float64")
    l = h15["low"].to_numpy(dtype="float64")
    c = h15["close"].to_numpy(dtype="float64")
    v = h15["volume"].to_numpy(dtype="float64")
    n = len(t15)

    sh_idx, sl_idx = detect_swings(h, l, n=SWING_LOOKBACK)
    trades = []

    for side, swings in (("low", sl_idx), ("high", sh_idx)):
        for abs_i in swings:
            if abs_i < SWING_LOOKBACK or abs_i > n - 6:
                continue
            swing_price = float(l[abs_i] if side == "low" else h[abs_i])

            start = abs_i + SWING_LOOKBACK
            end = min(start + SWING_TAIL_BARS, n)
            # One machine pass over the swing's tail, rolling quiet
            # reference per bar — the same call the live loop makes.
            r = run_state_machine(
                swing_price=swing_price, side=side,
                h=h, l=l, c=c, v=v, start=start, end=end)
            if r is None:
                continue
            state, j, reason = r
            if not state.ready or reason is not None:
                # not ready, or ready but stopped by a zone-width floor —
                # the machine names the gate, live shows it on the panel
                continue

            zone_w = state.zone_width
            # The return is only CONFIRMED when the 15m bar CLOSES — entry
            # at that bar's own open would be lookahead (the open predates
            # the confirmation by up to 15 minutes and, for a reclaimed
            # low, sits systematically below the confirm price). Enter at
            # the first 1m open AFTER the bar close.
            entry_t = int(t15[j] + (t15[j + 1] - t15[j]) if j + 1 < n
                          else t15[j] + 900_000)
            k = int(np.searchsorted(pt, entry_t, "left"))
            if k >= len(pt):
                continue
            entry = float(_po[k])
            is_long = side == "low"

            sl, tp = sweep_bracket(state, entry)
            # TP: nearest CONFIRMED opposing swing — confirmed means the
            # fractal had SWING_LOOKBACK bars after it, all at or before
            # the entry bar. Anything later is lookahead.
            opp = sh_idx if is_long else sl_idx
            confirmed = np.array(
                [si for si in opp
                 if abs_i < si <= j - SWING_LOOKBACK], dtype="int64")
            near = nearest_opposing_swing(entry, is_long, confirmed, h, l)
            if near is not None:
                tp = near

            # rr floor, same as the live evaluator: TP must be at least
            # MIN_RR × the stop distance.
            if abs(tp - entry) < MIN_RR * abs(entry - sl):
                continue

            reason_, px, xt, _amb = walk_forward(
                pt, ph, pl, pc, entry_t, entry, tp, sl, is_long, HORIZON)
            if reason_ == "nodata":
                continue
            resolved = reason_ in ("tp", "sl") or \
                int(pt[-1]) >= entry_t + HORIZON
            net = ((float(px) - entry) if is_long
                   else (entry - float(px))) \
                - entry * COST if resolved else 0.0
            trades.append({
                "entry_time": entry_t,
                "day_myt": datetime.fromtimestamp(
                    entry_t / 1000, MYT).strftime("%Y-%m-%d"),
                "event_type": f"double_sweep_{side}",
                "side": "long" if is_long else "short",
                "swing_level": swing_price,
                "zone_width": zone_w,
                "break_count": state.break_count,
                "entry_price": entry, "sl": sl, "tp": tp,
                "reason": reason_ if resolved else "open",
                "resolved": resolved, "net": round(net, 2),
            })
    trades.sort(key=lambda t: t["entry_time"])
    return trades


def stats(rows):
    if not rows:
        return {"n": 0, "win_pct": None, "exp": None, "pf": None,
                "net": 0.0}
    lab = [r for r in rows if r["resolved"]]
    n = len(lab)
    wins = sum(1 for r in lab if r["net"] > 0)
    nets = [r["net"] for r in lab]
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    return {"n": n, "win_pct": round(100 * wins / n, 1) if n else None,
            "exp": round(sum(nets) / n, 3) if n else None,
            "pf": round(gw / gl, 2) if gl > 0 else None,
            "net": round(sum(nets), 2)}


def show(title, rows):
    print(f"\n{'=' * 60}")
    print(f"SIGNAL 3 v2 BACKTEST — {title}")
    print(f"{'=' * 60}")
    s = stats(rows)
    print(f"  OVERALL   n={s['n']:4d} win={s['win_pct']}% "
          f"exp={s['exp']} pf={s['pf']} net={s['net']}")
    for tag, pred in (
            ("LONG (sweep low)", lambda r: r["side"] == "long"),
            ("SHORT (fake high)", lambda r: r["side"] == "short"),
            ("2025", lambda r: r["day_myt"].startswith("2025")),
            ("2026 H1", lambda r: "2026-01" <= r["day_myt"] < "2026-07"),
            ("2026 H2", lambda r: r["day_myt"] >= "2026-07"),
    ):
        sub = [r for r in rows if pred(r)]
        if sub:
            st = stats(sub)
            print(f"  {tag:18s} n={st['n']:4d} win={st['win_pct']}% "
                  f"exp={st['exp']} pf={st['pf']} net={st['net']}")
    # by sweep count
    for bc in (2, 3, 4):
        sub = [r for r in rows if r.get("break_count") == bc]
        if sub:
            st = stats(sub)
            print(f"  sweeps={bc:12d} n={st['n']:4d} win={st['win_pct']}% "
                  f"net={st['net']}")
    # zone width quartiles
    if rows:
        zw = sorted(r["zone_width"] for r in rows if r["resolved"])
        if len(zw) > 8:
            q1, q3 = zw[len(zw) // 4], zw[3 * len(zw) // 4]
            for tag, pred in (
                    (f"narrow (zw<{q1:.1f})", lambda r: r["zone_width"] < q1),
                    (f"mid ({q1:.1f}-{q3:.1f})",
                     lambda r: q1 <= r["zone_width"] <= q3),
                    (f"wide (zw>{q3:.1f})", lambda r: r["zone_width"] > q3)):
                sub = [r for r in rows if pred(r)]
                if sub:
                    st = stats(sub)
                    print(f"  {tag:18s} n={st['n']:4d} "
                          f"win={st['win_pct']}% net={st['net']}")


# Global po for the fold path
_po = None


def main():
    global _po

    with Database() as db:
        repo = CandleRepository(db)

        # Bybit XAUUSDT: 1m → 15m
        print("folding Bybit 1m → 15m…")
        d1 = repo.load("XAUUSDT", "1m").reset_index(drop=True)
        f15 = fold(d1, 900_000)
        pt = d1["open_time"].to_numpy(dtype="int64")
        ph = d1["high"].to_numpy(dtype="float64")
        pl = d1["low"].to_numpy(dtype="float64")
        pc = d1["close"].to_numpy(dtype="float64")
        po = d1["open"].to_numpy(dtype="float64")
        _po = po
        trades = run_sweep_trades(f15, pt, ph, pl, pc)
        show("Bybit XAUUSDT (1m→15m, 166 days)", trades)

        # MT5:GOLD: 5m → 15m
        print("\nfolding MT5:GOLD 5m → 15m…")
        d5 = repo.load("MT5:GOLD", "5m").reset_index(drop=True)
        f15m = fold(d5, 900_000)
        pt5 = d5["open_time"].to_numpy(dtype="int64")
        ph5 = d5["high"].to_numpy(dtype="float64")
        pl5 = d5["low"].to_numpy(dtype="float64")
        pc5 = d5["close"].to_numpy(dtype="float64")
        _po = d5["open"].to_numpy(dtype="float64")
        trades_m = run_sweep_trades(f15m, pt5, ph5, pl5, pc5)
        show("MT5:GOLD (5m→15m, 17 months)", trades_m)

        # PAXG
        print("\nfolding PAXGUSDT 5m → 15m…")
        dp = repo.load("PAXGUSDT", "5m").reset_index(drop=True)
        f15p = fold(dp, 900_000)
        ptp = dp["open_time"].to_numpy(dtype="int64")
        php = dp["high"].to_numpy(dtype="float64")
        plp = dp["low"].to_numpy(dtype="float64")
        pcp = dp["close"].to_numpy(dtype="float64")
        _po = dp["open"].to_numpy(dtype="float64")
        trades_p = run_sweep_trades(f15p, ptp, php, plp, pcp)
        show("PAXGUSDT (5m→15m, 4.4 years)", trades_p)


if __name__ == "__main__":
    main()
