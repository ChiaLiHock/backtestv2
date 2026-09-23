"""Re-mine Signal 4 against the last N trading days. Run this weekly.

    python -m backtest.tools.mine_signal4
    python -m backtest.tools.mine_signal4 --days 5 --min-win 0.85 --min-per-day 3
    python -m backtest.tools.mine_signal4 --dry-run          # search, write nothing

Writes `configs/signal4.json`. The live watcher notices the file changed and
reloads it without a restart, so a refit takes effect on the next cycle.

## What it searches

Features come from `engine.signal4.build_features` -- imported, not copied, so
the search and the live evaluator cannot drift apart. Quantile-binned single
conditions, then pairs among whatever survived, both sides.

## What it will not do

**It will not write a config that fails the bar.** If nothing reaches the
requested win rate at the requested frequency, it says so and leaves the
previous config in place. A weekly job that always produces an answer would
produce a worse one every time the week had no pattern in it, and the failure
mode of "signal4 quietly got worse" is much harder to notice than "signal4 did
not update this week".

## The numbers this produces are small

Five trading days at three trades a day is fifteen trades. Thirteen of fifteen
is 86.7% and has a raw p near 0.05 against the 63.5% break-even -- before
dividing by the thousands of conditions tried. The report prints the search size
next to the win rate for that reason, and `fitted` in the JSON carries both so
the page can show them together.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..engine.signal4 import (BREAKEVEN, COST_BPS, CONFIG_PATH, SL_USD,
                              TIME_STOP_BARS, TP_USD, build_features,
                              entry_is_weekday, bracket)
from ..indicators.base import IndicatorConfig
from ..tools.validate_rule import walk_forward

MYT = timezone(timedelta(hours=8))
SKIP = {"open_time", "usable", "open", "high", "low", "close", "volume",
        "dow_myt"}


def binom_sf(k: int, n: int, p: float) -> float:
    import math
    if n <= 0:
        return 1.0
    k = max(0, int(k))
    if k > n:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    return float(min(1.0, sum(
        math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                 + i * lp + (n - i) * lq) for i in range(k, n + 1))))


def label(db: Database, symbol: str, anchor: str, feat: pd.DataFrame) -> pd.DataFrame:
    """Walk every bar forward to TP15/SL25 from the NEXT bar's open."""
    repo = CandleRepository(db)
    path = repo.load(symbol, "1m").reset_index(drop=True)
    pt = path["open_time"].to_numpy(dtype="int64")
    ph = path["high"].to_numpy(dtype="float64")
    pl = path["low"].to_numpy(dtype="float64")
    pc = path["close"].to_numpy(dtype="float64")
    step = INTERVAL_MS[anchor]
    horizon = TIME_STOP_BARS * step
    ot = feat["open_time"].to_numpy(dtype="int64")
    o = feat["open"].to_numpy(dtype="float64")
    n = len(feat)

    for side in ("long", "short"):
        res = np.full(n, "", dtype=object)
        net = np.full(n, np.nan)
        ex = np.full(n, np.nan)
        long = side == "long"
        for i in range(n - 1):
            entry = o[i + 1]
            if not np.isfinite(entry):
                continue
            sl, tp = bracket(entry, side)
            reason, px, xt, _ = walk_forward(
                pt, ph, pl, pc, int(ot[i + 1]), entry, tp, sl, long, horizon)
            if reason == "nodata":
                continue
            gross = (px - entry) if long else (entry - px)
            res[i] = reason
            net[i] = gross - entry * COST_BPS / 1e4
            ex[i] = xt
        feat[f"{side}_reason"] = res
        feat[f"{side}_net"] = net
        feat[f"{side}_exit_ms"] = ex
    return feat


def deoverlap(open_time: np.ndarray, exit_ms: np.ndarray) -> np.ndarray:
    keep = np.zeros(open_time.size, dtype=bool)
    busy = -np.inf
    for i in range(open_time.size):
        if not np.isfinite(exit_ms[i]):
            continue
        if open_time[i] >= busy:
            keep[i] = True
            busy = exit_ms[i]
    return keep


def evaluate(w: pd.DataFrame, mask: np.ndarray, side: str) -> dict:
    r = w[f"{side}_reason"].to_numpy()
    net = w[f"{side}_net"].to_numpy(dtype="float64")
    ot = w["open_time"].to_numpy(dtype="int64")
    ex = w[f"{side}_exit_ms"].to_numpy(dtype="float64")
    m = np.asarray(mask, bool) & np.isin(r, ("tp", "sl", "timeout"))
    if not m.any():
        return {"n_eff": 0, "wins": 0, "losses": 0, "timeouts": 0,
                "win_rate": None, "net": 0.0, "p": None}
    sub = np.flatnonzero(m)
    idx = sub[deoverlap(ot[sub], ex[sub])]
    rr = r[idx]
    wins = int((rr == "tp").sum())
    loss = int((rr == "sl").sum())
    to = int((rr == "timeout").sum())
    resolved = wins + loss
    nn = net[idx]
    nn = nn[np.isfinite(nn)]
    return {"n_eff": int(idx.size), "wins": wins, "losses": loss, "timeouts": to,
            "resolved": resolved,
            "win_rate": wins / resolved if resolved else None,
            "net": float(nn.sum()),
            "p": binom_sf(wins, resolved, BREAKEVEN) if resolved else None}


def bins_for(s: pd.Series):
    v = s.to_numpy(dtype="float64")
    ok = np.isfinite(v)
    if ok.sum() < 60:
        return []
    uniq = np.unique(v[ok])
    out = []
    if uniq.size <= 6:
        return [("==", float(u), (v == u) & ok) for u in uniq]
    for q in (0.10, 0.20, 0.30, 0.40, 0.60, 0.70, 0.80, 0.90):
        t = float(np.nanquantile(v, q))
        if q <= 0.40:
            out.append(("<", t, (v < t) & ok))
        else:
            out.append((">", t, (v > t) & ok))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.tools.mine_signal4")
    ap.add_argument("--db", default=None)
    ap.add_argument("--symbol", default="XAUUSDT")
    ap.add_argument("--anchor", default="15m")
    ap.add_argument("--days", type=int, default=5, help="trading days to fit on")
    ap.add_argument("--min-win", type=float, default=0.85)
    ap.add_argument("--min-per-day", type=float, default=3.0)
    ap.add_argument("--out", default=str(CONFIG_PATH))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    with Database(a.db) if a.db else Database() as db:
        print(f"building features for {a.symbol} {a.anchor} ...", flush=True)
        feat = build_features(db, a.symbol, a.anchor, IndicatorConfig())
        if feat.empty:
            print("no candles"); return 1
        step = INTERVAL_MS[a.anchor]

        # WEEKDAYS ONLY, decided by when the trade would OPEN.
        feat = feat[entry_is_weekday(feat["open_time"].to_numpy(), step)] \
                 .reset_index(drop=True)
        entry_day = pd.to_datetime(feat["open_time"].to_numpy() + step, unit="ms",
                                   utc=True).tz_convert("Etc/GMT-8") \
                      .strftime("%Y-%m-%d")
        feat["entry_day"] = entry_day
        days = sorted(feat["entry_day"].unique())[-a.days:]
        feat = feat[feat["entry_day"].isin(days)].reset_index(drop=True)
        print(f"fitting on {len(days)} trading days: {', '.join(days)}")
        print(f"bars: {len(feat)}   break-even {BREAKEVEN*100:.2f}% "
              f"(TP {TP_USD:g} / SL {SL_USD:g})")

        print("labelling ...", flush=True)
        feat = label(db, a.symbol, a.anchor, feat)

    ndays = len(days)
    need = a.min_per_day * ndays
    for side in ("long", "short"):
        b = evaluate(feat, np.ones(len(feat), bool), side)
        if b["win_rate"] is not None:
            print(f"  baseline {side:5}: n_eff={b['n_eff']:3} "
                  f"({b['n_eff']/ndays:.1f}/day) win={b['win_rate']*100:5.1f}% "
                  f"net={b['net']:+8.1f}")

    feats = [c for c in feat.columns
             if c not in SKIP and not c.startswith(("long_", "short_"))
             and c != "entry_day" and pd.api.types.is_numeric_dtype(feat[c])]
    print(f"searching {len(feats)} features ...", flush=True)

    tested = 0
    singles, hits = [], []
    for col in feats:
        for op, thr, m in bins_for(feat[col]):
            if m.sum() < need:
                continue
            for side in ("long", "short"):
                tested += 1
                s = evaluate(feat, m, side)
                if not s["n_eff"] or s["win_rate"] is None:
                    continue
                rec = {"conditions": [{"feature": col, "op": op, "value": thr}],
                       "side": side, "per_day": s["n_eff"] / ndays, **s}
                if s["n_eff"] >= need * 0.7 and s["win_rate"] >= 0.60:
                    singles.append({**rec, "mask": m})
                if s["n_eff"] / ndays >= a.min_per_day and s["win_rate"] >= a.min_win:
                    hits.append(rec)

    seeds = sorted(singles, key=lambda d: -d["win_rate"])[:120]
    for x, y in itertools.combinations(seeds, 2):
        if x["side"] != y["side"]:
            continue
        if x["conditions"][0]["feature"] == y["conditions"][0]["feature"]:
            continue
        m = x["mask"] & y["mask"]
        if m.sum() < need:
            continue
        tested += 1
        s = evaluate(feat, m, x["side"])
        if not s["n_eff"] or s["win_rate"] is None:
            continue
        if s["n_eff"] / ndays >= a.min_per_day and s["win_rate"] >= a.min_win:
            hits.append({"conditions": x["conditions"] + y["conditions"],
                         "side": x["side"], "per_day": s["n_eff"] / ndays, **s})

    hits.sort(key=lambda r: (-r["win_rate"], -r["n_eff"]))
    print(f"\ntested {tested} conditions; "
          f"meeting win>={a.min_win:.0%} AND >={a.min_per_day:g}/day: {len(hits)}")
    for r in hits[:10]:
        expr = " AND ".join(f"{c['feature']} {c['op']} {c['value']:g}"
                            for c in r["conditions"])
        print(f"  {r['side']:5} n={r['n_eff']:3} {r['per_day']:.1f}/day "
              f"W{r['wins']}/L{r['losses']}/T{r['timeouts']} "
              f"win={r['win_rate']*100:5.1f}% net={r['net']:+7.1f} "
              f"p={r['p']:.4f}   {expr}")

    if not hits:
        print("\nNOTHING met the bar. The previous config is left untouched --")
        print("a weekly job that always writes something writes a worse pattern")
        print("every week the data has none. Loosen --min-win / --min-per-day")
        print("to see what the week actually offered.")
        return 2

    best = hits[0]
    blob = {
        "symbol": a.symbol, "broker_symbol": "GOLD", "anchor": a.anchor,
        "side": best["side"], "conditions": best["conditions"], "lots": 0.01,
        "fitted": {
            "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "days": days, "n_days": ndays,
            "trades": best["n_eff"], "per_day": round(best["per_day"], 2),
            "wins": best["wins"], "losses": best["losses"],
            "timeouts": best["timeouts"],
            "win_rate": round(best["win_rate"], 4),
            "net": round(best["net"], 2),
            "p_value": round(best["p"], 6) if best["p"] is not None else None,
            "conditions_tested": tested,
            "breakeven": round(BREAKEVEN, 5),
            "bracket": f"TP +{TP_USD:g} / SL -{SL_USD:g}",
        },
    }
    if a.dry_run:
        print("\n--dry-run: not written. Would have been:")
        print(json.dumps(blob, indent=1))
        return 0
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    print("the running watcher reloads it on its next cycle -- no restart needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
