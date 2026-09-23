"""Fit the Signal 7 model artifact (ExpD) and save configs/signal7_model.json.

Train: MT5:GOLD 5m, full history, every resolved break sample (the cross-
feed-validated setup: the honest choice for a model applied to Bybit live).

Thresholds: picked on train by the walk-forward picker, then CAPPED
(default 0.50). The cap replicates on all three feeds — uncapped
MT5-optimal thresholds trade ~2x less for a 2-3pp win-rate gain and LESS
net everywhere (measured 2026-09-20: MT5 +2196→+4013, PAXG +2209→+3365,
Bybit −64→+329 at cap 0.5); a flat 0.5 is also the least-tuned choice.

    python -m backtest.tools.fit_signal7 [--cap 0.5]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from ..data.db import Database
from ..engine.signal6_retrain import (EXP_C_EXTRA, EXP_LEG, FEATURE_KEYS,
                                      LogisticPwin, build_samples, matrix,
                                      _stats)
from ..engine.signal7 import THRESHOLD_CAP
from ..indicators.base import IndicatorConfig
from ..tools.retrain_signal6 import pick_regime_thresholds, regime_of

MODEL_PATH = Path(__file__).resolve().parents[1] / "configs" / \
    "signal7_model.json"
NEW5 = {"adx_slope", "ema_slope_change", "atr_pctile_change",
        "vol_change_ratio", "break_velocity"}
M1K = tuple(k for k in FEATURE_KEYS if k not in (NEW5 | set(EXP_LEG)))
EXPD_KEYS = tuple(dict.fromkeys(M1K + EXP_C_EXTRA))


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    cap = THRESHOLD_CAP
    if "--cap" in argv:
        try:
            cap = float(argv[argv.index("--cap") + 1])
        except (IndexError, ValueError):
            pass
    with Database() as db:
        print("building MT5:GOLD training samples (5m, full history)…")
        samples = build_samples(db, "MT5:GOLD", IndicatorConfig(),
                                data_interval="5m")
        X, y = matrix(samples)
        col = {k: i for i, k in enumerate(FEATURE_KEYS)}
        use = [col[k] for k in EXPD_KEYS]
        resolved = np.array([s["label"]["resolved"] for s in samples])
        Xtr, ytr = X[:, use][resolved], y[resolved]
        print(f"resolved train samples: {int(resolved.sum())}")
        m = LogisticPwin().fit(Xtr, ytr)
        thresholds = pick_regime_thresholds(
            [s for s, ok in zip(samples, resolved) if ok],
            m.predict(Xtr))
        raw = dict(thresholds)
        if cap is not None:
            thresholds = {k: round(min(v, cap), 3)
                          for k, v in thresholds.items()}
        # in-sample sanity only (the honest numbers live in the walker)
        p = m.predict(Xtr)
        tr_rows = [s for s, ok in zip(samples, resolved) if ok]
        taken = [r for r, pi in zip(tr_rows, p)
                 if pi >= thresholds[regime_of(r)]]
        st = _stats(taken)
        print(f"train (in-sample, sanity): n={st['n']} win={st['win_pct']}% "
              f"thresholds={thresholds}")

        blob = {
            "keys": list(EXPD_KEYS),
            "w": [float(v) for v in m.w],
            "mu": [float(v) for v in m.mu],
            "sd": [float(v) for v in m.sd],
            "thresholds": thresholds,
            "thresholds_uncapped": raw,
            "threshold_cap": cap,
            "trained_at": int(time.time() * 1000),
            "train_n": int(resolved.sum()),
            "train_feed": "MT5:GOLD 5m full history",
            "note": ("ExpD regime gate, thresholds capped at "
                     f"{cap} — replicated on MT5/PAXG/Bybit: more trades, "
                     "more net (2026-09-20). EXPERIMENTAL channel 7."),
        }
        MODEL_PATH.write_text(json.dumps(blob, indent=1), encoding="utf-8")
        print(f"saved {MODEL_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
