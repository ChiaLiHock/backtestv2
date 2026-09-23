"""Signal 3 v2 backtest with the adaptive timeframe + zone-width filter.

Runs the full sweep state machine at each rung of the adaptive ladder
(15/25/30/35m), applying the owner's escalation rule (bar range < $8)
and the zone-width floor (>= 50% of trailing median), to measure the
combined effect on all three feeds.
"""
import sys
import collections
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")

from backtest.data.db import Database, CandleRepository
from backtest.engine.signal3 import (detect_swings, SweepState, MIN_RR,
                                     QUIET_VOL_RATIO_MAX, VOL_MEDIAN_BARS,
                                     SWING_TAIL_BARS, SWING_LOOKBACK,
                                     SL_ZONE_MULT, MIN_RANGE_USD,
                                     TF_LADDER_MS,
                                     CONSEC_BARS_TO_DOWNGRADE,
                                     MIN_ZONE_WIDTH_OF_MEDIAN)
from backtest.tools.validate_rule import walk_forward

MYT = timezone(timedelta(hours=8))
HORIZON = 24 * 3600_000
COST = 0.85e-4


def fold(df, step):
    key = ((df["open_time"] // step) * step).astype("int64")
    return df.groupby(key).agg(
        open_time=("open_time", "min"), open=("open", "first"),
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum")).reset_index(drop=True)


def adaptive_run(path_df, pt, ph, pl, pc, po):
    """Walk history, picking the TF per bar like the live evaluator does,
    running the sweep machine at the chosen TF. Returns trade rows."""
    t = path_df["open_time"].to_numpy(dtype="int64")
    n = len(t)
    trades = []

    # simulate the adaptive TF over time (bar by bar on the 5m grid)
    tf_level = 0
    consec_ok = 0

    # build all TF frames once
    frames = {}
    for lvl, tf_ms in enumerate(TF_LADDER_MS):
        frames[lvl] = fold(path_df, tf_ms)
    f5 = fold(path_df, 300_000)

    # walk 5m bars; at each closed TF bar, check upgrade/downgrade
    # simplified: re-evaluate every 5 bars (= one 25m chunk)
    for chunk_start in range(0, n - SWING_TAIL_BARS * 4, 25):
        chunk_end = min(chunk_start + 25, n)
        sub = path_df.iloc[chunk_start:chunk_end]

        # check the current TF's latest bar range
        tf_ms = TF_LADDER_MS[tf_level]
        # compute this chunk's high-low range at the current TF
        chunk_range = float(sub["high"].max() - sub["low"].min())
        chunk_ms = (chunk_end - chunk_start) * (t[1] - t[0]) if chunk_end > chunk_start + 1 else 300_000

        if chunk_range >= MIN_RANGE_USD:
            consec_ok += 1
            if consec_ok >= CONSEC_BARS_TO_DOWNGRADE and tf_level > 0:
                tf_level = 0
                consec_ok = 0
        else:
            consec_ok = 0
            if tf_level < len(TF_LADDER_MS) - 1:
                # check if the next rung's bar is wide enough
                next_ms = TF_LADDER_MS[tf_level + 1]
                # look at a wider window
                wider_start = max(0, chunk_start - (next_ms // 300_000
                                                    - (chunk_end - chunk_start)))
                wider = path_df.iloc[wider_start:chunk_end]
                wider_range = float(wider["high"].max()
                                    - wider["low"].min())
                if wider_range >= MIN_RANGE_USD:
                    tf_level += 1
                else:
                    tf_level = min(tf_level + 1, len(TF_LADDER_MS) - 1)

        # at each new TF level change, run the sweep machine on the
        # trailing window at this TF
        tf_ms = TF_LADDER_MS[tf_level]
        tf_df = frames[tf_level]
        # only process the last ~SWING_TAIL_BARS bars of this TF frame
        # that fall within the current time range
        tf_t = tf_df["open_time"].to_numpy(dtype="int64")
        tf_h = tf_df["high"].to_numpy(dtype="float64")
        tf_l = tf_df["low"].to_numpy(dtype="float64")
        tf_c = tf_df["close"].to_numpy(dtype="float64")
        tf_v = tf_df["volume"].to_numpy(dtype="float64")
        tf_n = len(tf_t)

        now_ms = t[chunk_end - 1]
        now_bar = int(np.searchsorted(tf_t, now_ms, "right")) - 1
        if now_bar < SWING_TAIL_BARS:
            continue

        vol_med = float(np.median(
            tf_v[max(0, now_bar - VOL_MEDIAN_BARS):now_bar]))
        sh_idx, sl_idx = detect_swings(
            tf_h[max(0, now_bar - SWING_TAIL_BARS):],
            tf_l[max(0, now_bar - SWING_TAIL_BARS):])
        lo_i = max(0, now_bar - SWING_TAIL_BARS)

        for side, swings in (("low", sl_idx), ("high", sh_idx)):
            for si in swings:
                abs_i = int(si) + lo_i
                if abs_i > now_bar - 4:
                    continue
                swing_price = float(tf_l[abs_i] if side == "low"
                                    else tf_h[abs_i])
                state = SweepState(swing_price=swing_price, side=side)
                start = abs_i + SWING_LOOKBACK
                for j in range(start,
                               min(start + SWING_TAIL_BARS, now_bar + 1)):
                    close = float(tf_c[j])
                    breaking = close < swing_price if side == "low" \
                        else close > swing_price
                    returning = close >= swing_price if side == "low" \
                        else close <= swing_price

                    if breaking and state.returned:
                        vol = float(tf_v[j])
                        quiet = vol < vol_med * QUIET_VOL_RATIO_MAX \
                            if vol_med > 0 else False
                        if quiet:
                            state.break_count += 1
                            state.returned = False
                            if side == "low":
                                state.extreme = min(
                                    state.extreme or swing_price,
                                    float(tf_l[j]))
                            else:
                                state.extreme = max(
                                    state.extreme or swing_price,
                                    float(tf_h[j]))
                    elif returning and not state.returned:
                        state.returned = True
                        if state.ready:
                            zone_w = state.zone_width
                            # zone-width floor
                            local_scale = abs(swing_price - float(
                                np.median(
                                    tf_l[j - VOL_MEDIAN_BARS:j]
                                    if side == "low"
                                    else tf_h[j - VOL_MEDIAN_BARS:j]))) \
                                if j >= VOL_MEDIAN_BARS else 0
                            if local_scale > 0 and zone_w \
                                    < MIN_ZONE_WIDTH_OF_MEDIAN * local_scale:
                                break

                            entry_t = int(tf_t[j])
                            k = int(np.searchsorted(pt, entry_t))
                            if k >= len(pt):
                                continue
                            entry = float(po[k])
                            is_long = side == "low"
                            sl = (state.extreme
                                  - SL_ZONE_MULT * zone_w) if is_long \
                                else (state.extreme + SL_ZONE_MULT * zone_w)
                            tp = (swing_price + 3 * zone_w) if is_long \
                                else (swing_price - 3 * zone_w)
                            opp = sh_idx if is_long else sl_idx
                            for osi in opp:
                                oabs = int(osi) + lo_i
                                if is_long and oabs > abs_i \
                                        and float(tf_h[oabs]) > entry:
                                    tp = float(tf_h[oabs])
                                    break
                                elif not is_long and oabs > abs_i \
                                        and float(tf_l[oabs]) < entry:
                                    tp = float(tf_l[oabs])
                                    break

                            # rr floor, same as the live evaluator: TP must
                            # be at least MIN_RR × the stop distance.
                            if abs(tp - entry) < MIN_RR * abs(entry - sl):
                                continue

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
                                    entry_t / 1000,
                                    MYT).strftime("%Y-%m-%d"),
                                "side": "long" if is_long else "short",
                                "zone_width": round(zone_w, 2),
                                "entry_price": round(entry, 4),
                                "break_count": state.break_count,
                                "tf_ms": tf_ms,
                                "tf_label": f"{tf_ms // 60_000}m",
                                "resolved": resolved,
                                "net": round(net, 2),
                            })

    # dedupe by entry_time + side
    seen = set()
    deduped = []
    for tr in sorted(trades, key=lambda x: x["entry_time"]):
        key = (tr["entry_time"], tr["side"])
        if key not in seen:
            seen.add(key)
            deduped.append(tr)
    return deduped


def stats(rows):
    lab = [r for r in rows if r["resolved"]]
    if not lab:
        return {"n": 0, "win_pct": None, "exp": None, "pf": None,
                "net": 0.0}
    n = len(lab)
    wins = sum(1 for r in lab if r["net"] > 0)
    nets = [r["net"] for r in lab]
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    return {"n": n, "win_pct": round(100 * wins / n, 1),
            "exp": round(sum(nets) / n, 3),
            "pf": round(gw / gl, 2) if gl > 0 else None,
            "net": round(sum(nets), 2)}


def show(title, rows):
    print(f"\n{'=' * 60}")
    print(f"SIGNAL 3 v2 ADAPTIVE TF BACKTEST — {title}")
    print(f"{'=' * 60}")
    s = stats(rows)
    print(f"  OVERALL   n={s['n']:5d} win={s['win_pct']}% "
          f"exp={s['exp']} pf={s['pf']} net={s['net']}")
    # by TF used
    tf_counts = collections.Counter(r["tf_label"] for r in rows)
    for tf in sorted(tf_counts):
        sub = [r for r in rows if r["tf_label"] == tf]
        st = stats(sub)
        print(f"  TF={tf:5s}       n={st['n']:5d} win={st['win_pct']}% "
              f"net={st['net']}")
    # by side
    for side in ("long", "short"):
        sub = [r for r in rows if r["side"] == side]
        st = stats(sub)
        if st["n"]:
            print(f"  {side:12s}  n={st['n']:5d} win={st['win_pct']}% "
                  f"net={st['net']}")
    # halves
    rows_s = sorted(rows, key=lambda r: r["entry_time"])
    h = len(rows_s) // 2
    t1, t2 = stats(rows_s[:h]), stats(rows_s[h:])
    print(f"  halves: {t1['win_pct']}%/{t2['win_pct']}%  "
          f"nets {t1['net']}/{t2['net']}")


def main():
    with Database() as db:
        repo = CandleRepository(db)

        for tag, sym, iv in (("Bybit", "XAUUSDT", "1m"),
                             ("MT5", "MT5:GOLD", "5m")):
            print(f"folding {tag}…")
            d = repo.load(sym, iv).reset_index(drop=True)
            if iv == "5m":
                # convert to pseudo-1m for the walk_forward path (the
                # path arrays just need ascending 1m-equivalent bars)
                d_path = d
            else:
                d_path = d
            pt = d["open_time"].to_numpy(dtype="int64")
            ph = d["high"].to_numpy(dtype="float64")
            pl = d["low"].to_numpy(dtype="float64")
            pc = d["close"].to_numpy(dtype="float64")
            po = d["open"].to_numpy(dtype="float64")
            trades = adaptive_run(d, pt, ph, pl, pc, po)
            show(f"{tag} ({sym}, {iv})", trades)


if __name__ == "__main__":
    main()
