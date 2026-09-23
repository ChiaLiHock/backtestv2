"""Signal 7 backtest — the definitive walk-forward record, both feeds.

Two evaluations, both strictly out-of-sample:

* IN-FEED (MT5:GOLD 17 months): expanding train, quarterly test.
* CROSS-FEED (Bybit): trained on MT5 data before each Bybit quarter,
  tested on that quarter — the setup the channel actually runs (its model
  artifact is MT5-trained) and the one that needs no warm-up on the short
  Bybit history.

Each prints the full stats block (N / WinRate / Expectancy / PF / Net /
MaxDD) sliced by year, quarter, regime and session, plus the research
labels (MFE/MAE). Nothing here trains on what it prints.

Usage:  python -m backtest.tools.backtest_signal7 [--vs-signal6]
"""

from __future__ import annotations

import sys
from typing import Any

import numpy as np

from ..data.db import Database
from ..engine.signal6_retrain import (EXP_C_EXTRA, EXP_LEG, FEATURE_KEYS,
                                      LogisticPwin, build_samples, matrix,
                                      _stats)
from ..engine.signal6_retrain import model0_baseline
from ..engine.signal7 import THRESHOLD_CAP
from ..indicators.base import IndicatorConfig
from ..tools.retrain_signal6 import pick_regime_thresholds, regime_of

QUARTER = 90 * 86_400_000
NEW5 = {"adx_slope", "ema_slope_change", "atr_pctile_change",
        "vol_change_ratio", "break_velocity"}
M1K = tuple(k for k in FEATURE_KEYS if k not in (NEW5 | set(EXP_LEG)))
EXPD_KEYS = tuple(dict.fromkeys(M1K + EXP_C_EXTRA))


def _wf(test, train, warmup=2):
    """Walk-forward: per test quarter, fit on TRAIN entries before it."""
    Xt, _ = matrix(test)
    Xtr, ytr = matrix(train)
    col = {k: i for i, k in enumerate(FEATURE_KEYS)}
    use = [col[k] for k in EXPD_KEYS]
    taken: list[dict] = []
    start = test[0]["entry_time"] + (warmup * QUARTER if train is test else 0)
    while start < test[-1]["entry_time"]:
        end = start + QUARTER
        tr = [i for i, s in enumerate(train) if s["entry_time"] < start]
        te = [i for i, s in enumerate(test)
              if start <= s["entry_time"] < end]
        if len(tr) >= 60 and te:
            resolved = np.array([train[i]["label"]["resolved"]
                                 for i in tr])
            m = LogisticPwin().fit(Xtr[tr][:, use][resolved],
                                   ytr[tr][resolved])
            thresholds = pick_regime_thresholds(
                [train[i] for i, ok in zip(tr, resolved) if ok],
                m.predict(Xtr[tr][:, use][resolved]))
            thresholds = {k: min(v, THRESHOLD_CAP)
                          for k, v in thresholds.items()}
            p = m.predict(Xt[te][:, use])
            for i, pi in zip(te, p):
                s = test[i]
                if pi >= thresholds[regime_of(s)]:
                    taken.append(dict(s, p_win=round(float(pi), 3)))
        start = end
    return taken


def _block(title: str, rows: list[dict]) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)
    s = _stats(rows)
    print(f"  OVERALL   n={s['n']:4d} win={s['win_pct']}% "
          f"exp={s['expectancy']} pf={s['pf']} net={s['net']} "
          f"maxdd={s['maxdd']}")
    settled = [r["label"] for r in rows if r["label"]["resolved"]]
    if settled:
        mfe = [l["mfe_R"] for l in settled]
        mae = [l["mae_R"] for l in settled]
        print(f"  research  MFE mean={round(sum(mfe)/len(mfe), 2)}R  "
              f"MAE mean={round(sum(mae)/len(mae), 2)}R  "
              f"avg minutes={round(sum(l['minutes_to_outcome'] for l in settled)
                                  / len(settled), 0)}")
    slices = [
        ("2025", lambda r: r["day_myt"].startswith("2025")),
        ("2026 H1", lambda r: "2026-01" <= r["day_myt"] < "2026-07"),
        ("2026 H2", lambda r: r["day_myt"] >= "2026-07"),
        ("Trend", lambda r: regime_of(r) == "trend"),
        ("Range", lambda r: regime_of(r) == "range"),
        ("Europe", lambda r: r["confirm_hour_myt"] < 20),
        ("US", lambda r: r["confirm_hour_myt"] >= 20),
        ("break_high", lambda r: r["event_type"] == "break_high"),
        ("break_low", lambda r: r["event_type"] == "break_low"),
    ]
    for name, pred in slices:
        sub = [r for r in rows if pred(r)]
        if sub:
            st = _stats(sub)
            print(f"  {name:11s} n={st['n']:4d} win={st['win_pct']}% "
                  f"exp={st['expectancy']} pf={st['pf']} net={st['net']}")


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    with Database() as db:
        print("building samples…")
        mt5 = build_samples(db, "MT5:GOLD", IndicatorConfig(),
                            data_interval="5m")
        bybit = build_samples(db, "XAUUSDT", IndicatorConfig(),
                              data_interval="1m")
        print(f"MT5 {len(mt5)} samples, Bybit {len(bybit)} samples")

        infeed = _wf(mt5, mt5, warmup=2)
        _block("SIGNAL 7 BACKTEST — in-feed walk-forward "
               "(train MT5 past, test MT5 quarter)", infeed)

        cross = _wf(bybit, mt5, warmup=0)
        _block("SIGNAL 7 BACKTEST — cross-feed walk-forward "
               "(train MT5 past, test Bybit quarter)", cross)

        if "--vs-signal6" in argv:
            for tag, rows in (("MT5", mt5), ("Bybit", bybit)):
                base = model0_baseline(rows)
                _block(f"REFERENCE — production Signal 6 rules on {tag}",
                       base)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
