"""Bar-for-bar reconciliation of the live evaluator against the harness.

    python -m backtest.tools.live_vs_backtest --days 90

`OPERATING_PLAN.md` §10.4. This **replaces** the 30-signal paper run in
`AUTOMATION.md` step 7. At ~5 entries a week that run would take six weeks and
would only prove the plumbing works; it cannot detect a leg computed off the
wrong bar, an off-by-one in the anchor, or a stale ATR, because a live signal has
nothing to be compared against.

This does. `engine/live_signal.py` and `tools/validate_rule.py` reach the same
decision by different code paths — the first walks bars one at a time the way the
executor will, the second measures a panel — and every 15m bar of the last 90
days is compared on:

* each of the seven leg booleans, by name;
* `fired` and `would_enter`;
* entry reference, stop, target and time stop, **to the tick**.

Thousands of comparisons, minutes to run. **The mismatch count must be zero.**
When it is not, the diff is the bug and this prints it rather than a summary.

⚠️ `--path-tf 15m` is not optional. The 1m series only reaches 2026-05-12 and the
harness auto-picks the finest feed available, which silently cuts the panel from
588 trades to 76 — a reconciliation that agrees on a truncated panel has proved
nothing about the rest of it.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from ..data.db import INTERVAL_MS, Database
from ..engine.live_signal import LiveSignal, RuleConfig
from ..tools.validate_rule import (LEGS_LONG, LEGS_SHORT, build_context,
                                   leg_masks, measure)

TICK = 0.01          # GOLD price granularity; levels must agree to this


@dataclass
class Mismatch:
    bar_ms: int
    field: str
    live: object
    harness: object

    def describe(self) -> str:
        t = datetime.fromtimestamp(self.bar_ms / 1000, timezone.utc)
        return (f"{t:%Y-%m-%d %H:%M}Z  {self.field:<22} "
                f"live={self.live!r:<22} harness={self.harness!r}")


@dataclass
class Report:
    bars: int = 0
    compared: int = 0
    live_entries: int = 0
    harness_entries: int = 0
    live_fired: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.mismatches


def reconcile(
    db: Database, cfg: RuleConfig, days: int = 90, path_tf: str = "15m",
) -> Report:
    t0 = time.time()
    rep = Report()
    since = int((time.time() - days * 86400) * 1000)

    sig = LiveSignal(cfg)
    live = sig.backfill(db, since_ms=since)
    rep.bars = len(live)
    if not live:
        return rep

    # --- the harness, on the same panel, one leg-mask evaluation ------------
    ctx, _df, _cov = build_context(db, cfg.symbol, cfg.anchor, sig.ind_cfg,
                                   cfg.dip_lookback)
    ot = ctx.open_time
    masks = leg_masks(ctx, cfg.side, cfg.use_5m_leg)
    atr = ctx.ind["atr_14"].to_numpy()
    close = ctx.close
    idx = {int(t): i for i, t in enumerate(ot)}

    panels = measure(db, cfg.symbol, cfg.anchor, tp_mult=cfg.tp_atr,
                     sl_mult=cfg.sl_atr, cost_bps=cfg.cost_bps,
                     use_5m=cfg.use_5m_leg, dip_lookback=cfg.dip_lookback,
                     path_tf=path_tf, horizon_bars=cfg.time_stop_bars,
                     session=cfg.session, max_concurrent=cfg.max_concurrent)
    step = INTERVAL_MS[cfg.anchor]
    # The harness records the FILL bar; the live log records the SIGNAL bar,
    # which is one anchor bar earlier.
    h_entry = {int(t.entry_time) - step: t for t in panels[cfg.side].trades}
    rep.harness_entries = sum(1 for b in h_entry
                              if live[0].bar_open_ms <= b <= live[-1].bar_open_ms)

    def near(a, b, tol=TICK / 2) -> bool:
        if a is None or b is None:
            return a is b
        return abs(float(a) - float(b)) <= tol

    for e in live:
        i = idx.get(e.bar_open_ms)
        if i is None:
            rep.mismatches.append(Mismatch(e.bar_open_ms, "bar missing in harness",
                                           True, False))
            continue
        rep.compared += 1
        rep.live_fired += e.fired
        rep.live_entries += e.would_enter

        # --- every leg, by name --------------------------------------------
        for label, m in masks.items():
            want = bool(m[i])
            got = e.legs.get(label)
            if got != want:
                rep.mismatches.append(Mismatch(e.bar_open_ms, f"leg {label}", got, want))

        # --- would_enter against the harness's own trade list ---------------
        h = h_entry.get(e.bar_open_ms)
        if bool(h) != e.would_enter:
            rep.mismatches.append(Mismatch(e.bar_open_ms, "would_enter",
                                           e.would_enter, bool(h)))

        # --- levels, to the tick -------------------------------------------
        if e.would_enter:
            # The log stores ATR at 4 dp deliberately; comparing it to a
            # float64 at 1e-6 asserts a precision the log never claimed. The
            # LEVELS are what must agree to the tick, and they are computed from
            # the full-precision ATR before being rounded for storage — which is
            # why sl/tp below compare clean at TICK/2 while this needs 5e-5.
            if not near(e.atr_entry, atr[i], 5e-5):
                rep.mismatches.append(Mismatch(e.bar_open_ms, "atr_entry",
                                               e.atr_entry, float(atr[i])))
            if not near(e.entry_ref, close[i]):
                rep.mismatches.append(Mismatch(e.bar_open_ms, "entry_ref",
                                               e.entry_ref, float(close[i])))
            a = float(atr[i])
            want_sl = float(close[i]) - cfg.sl_atr * a
            want_tp = float(close[i]) + cfg.tp_atr * a
            if cfg.side == "short":
                want_sl, want_tp = (float(close[i]) + cfg.sl_atr * a,
                                    float(close[i]) - cfg.tp_atr * a)
            if not near(e.sl, want_sl):
                rep.mismatches.append(Mismatch(e.bar_open_ms, "sl", e.sl, round(want_sl, 2)))
            if not near(e.tp, want_tp):
                rep.mismatches.append(Mismatch(e.bar_open_ms, "tp", e.tp, round(want_tp, 2)))
            want_ts = e.bar_open_ms + step + cfg.time_stop_bars * step
            if e.time_stop_ms != want_ts:
                rep.mismatches.append(Mismatch(e.bar_open_ms, "time_stop_ms",
                                               e.time_stop_ms, want_ts))

    # An entry the harness took that the live path never saw at all.
    for b in h_entry:
        if live[0].bar_open_ms <= b <= live[-1].bar_open_ms:
            if not any(e.bar_open_ms == b and e.would_enter for e in live):
                if not any(m.bar_ms == b and m.field == "would_enter"
                           for m in rep.mismatches):
                    rep.mismatches.append(
                        Mismatch(b, "would_enter (harness only)", False, True))

    rep.seconds = time.time() - t0
    return rep


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    p = argparse.ArgumentParser(prog="python -m backtest.tools.live_vs_backtest")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--path-tf", default="15m",
                   help="REQUIRED at 15m: auto-picking 1m truncates the panel")
    p.add_argument("--max-diffs", type=int, default=40)
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)

    cfg = RuleConfig()
    with (Database(a.db) if a.db else Database()) as db:
        rep = reconcile(db, cfg, days=a.days, path_tf=a.path_tf)

    print(f"reconciled {rep.compared} bars over {a.days} days "
          f"({rep.seconds:.1f}s)   path walked at {a.path_tf}")
    print(f"  config      {cfg.side} TP{cfg.tp_atr}/SL{cfg.sl_atr} ATR, "
          f"{cfg.max_concurrent} slots, time stop {cfg.time_stop_bars} bars, "
          f"leg5 {'ON' if cfg.use_5m_leg else 'OFF'}")
    print(f"  live        {rep.live_fired} fired, {rep.live_entries} would enter")
    print(f"  harness     {rep.harness_entries} entries")
    checks = rep.compared * (len(LEGS_LONG if cfg.side == 'long' else LEGS_SHORT)
                             - (0 if cfg.use_5m_leg else 1) + 1)
    print(f"  comparisons ~{checks:,} leg/decision checks plus levels on entries")

    if rep.ok:
        print("\n  MISMATCHES: 0")
        return 0
    print(f"\n  MISMATCHES: {len(rep.mismatches)}  <-- must be zero")
    by = {}
    for m in rep.mismatches:
        by[m.field] = by.get(m.field, 0) + 1
    for k, v in sorted(by.items(), key=lambda x: -x[1]):
        print(f"    {v:5d}  {k}")
    print()
    for m in rep.mismatches[:a.max_diffs]:
        print("   ", m.describe())
    if len(rep.mismatches) > a.max_diffs:
        print(f"    ... {len(rep.mismatches) - a.max_diffs} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
