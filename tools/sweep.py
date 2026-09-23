"""Parameter sweep — reports the whole surface, never just the winner.

The master spec is explicit that optimisation must show parameter *sensitivity*
rather than a single best value, and this prints every combination sorted by net
P/L precisely so the shape is visible. A result that is good at one setting and
terrible at both neighbours is noise; a broad plateau is at least a candidate.

Nothing here selects a config for you. It is a diagnostic.

    python -m backtest.tools.sweep configs/ut_mtf_ride_long.yaml --trail-sweep
"""

from __future__ import annotations

import argparse
import itertools
import time
from pathlib import Path

import numpy as np

from ..analysis.metrics import wilson_interval
from ..data.db import Database
from ..engine.backtester import Backtester
from ..engine.config import StrategyConfig


def _stats(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pnl = np.array([t.net_pnl for t in trades])
    wins = int((pnl > 0).sum())
    lo, hi = wilson_interval(wins, n)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return {
        "n": n,
        "win": wins / n * 100,
        "lo": lo * 100,
        "hi": hi * 100,
        "net": float(pnl.sum()),
        "avg_w": float(pnl[pnl > 0].mean()) if wins else 0.0,
        "avg_l": float(pnl[pnl < 0].mean()) if (pnl < 0).any() else 0.0,
        "bars": float(np.mean([t.bars_held for t in trades])),
        "mfe": float(np.mean([t.mfe for t in trades])),
        "trail": reasons.get("trail", 0),
        "sl": reasons.get("sl", 0),
        "tp": reasons.get("tp", 0),
    }


def run_trail_sweep(db: Database, base: StrategyConfig,
                    timeframes, emas, closes_list) -> list[tuple]:
    if base.exit.trailing is None:
        raise SystemExit("--trail-sweep needs a config with exit.trailing set")

    rows = []
    for tf, ema, closes in itertools.product(timeframes, emas, closes_list):
        trail = base.exit.trailing.model_copy(
            update={"timeframe": tf, "ema": ema, "closes": closes}
        )
        cfg = base.model_copy(
            update={"exit": base.exit.model_copy(update={"trailing": trail})}
        )
        t0 = time.time()
        result = Backtester(db, cfg).run(run_id=f"sweep_{tf}_{ema}_{closes}")
        s = _stats(result.trades)
        s.update(trail_tf=tf, ema=ema, closes=closes, secs=time.time() - t0)
        rows.append(s)
        print(f"  {tf}/{ema}/closes={closes}: n={s['n']} net={s.get('net', 0):.0f} "
              f"({s['secs']:.0f}s)", flush=True)
    return rows


def print_table(rows: list[dict]) -> None:
    hdr = (f"{'trail':<6}{'ema':<9}{'cl':>3}{'n':>5}{'win%':>7}{'95% CI':>13}"
           f"{'net':>9}{'avgW':>7}{'avgL':>7}{'bars':>6}{'avgMFE':>8}"
           f"{'trail':>7}{'sl':>4}")
    print()
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda x: -x.get("net", 0)):
        if not r["n"]:
            print(f"{r['trail_tf']:<6}{r['ema']:<9}{r['closes']:>3}    0   (no trades)")
            continue
        ci = f"{r['lo']:.0f}-{r['hi']:.0f}%"
        print(f"{r['trail_tf']:<6}{r['ema']:<9}{r['closes']:>3}{r['n']:>5}"
              f"{r['win']:>7.1f}{ci:>13}{r['net']:>9.0f}{r['avg_w']:>7.1f}"
              f"{r['avg_l']:>7.1f}{r['bars']:>6.1f}{r['mfe']:>8.1f}"
              f"{r['trail']:>7}{r['sl']:>4}")

    best = max(rows, key=lambda x: x.get("net", 0))
    if best["n"]:
        print()
        print(f"Best by net P/L: {best['trail_tf']}/{best['ema']}/closes={best['closes']}"
              f"  net={best['net']:.0f}  win={best['win']:.1f}%")
        print("Read the COLUMN, not the row: a setting that only works at one point")
        print("and fails at both neighbours is noise. A plateau is a candidate.")
        print(f"With n={best['n']} the 95% CI is {best['lo']:.0f}-{best['hi']:.0f}% —")
        print("wide enough that ranking these by net P/L is not a reliable ordering.")


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m backtest.tools.sweep")
    p.add_argument("config")
    p.add_argument("--trail-sweep", action="store_true",
                   help="vary exit.trailing timeframe / ema / closes")
    p.add_argument("--timeframes", default="5m,15m")
    p.add_argument("--emas", default="ema_14,ema_28")
    p.add_argument("--closes", default="1,2,3")
    p.add_argument("--db", default=None)
    args = p.parse_args()

    base = StrategyConfig.from_yaml(Path(args.config))
    db = Database(args.db) if args.db else Database()

    if not args.trail_sweep:
        raise SystemExit("nothing to sweep — pass --trail-sweep")

    rows = run_trail_sweep(
        db, base,
        [s.strip() for s in args.timeframes.split(",")],
        [s.strip() for s in args.emas.split(",")],
        [int(s) for s in args.closes.split(",")],
    )
    print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
