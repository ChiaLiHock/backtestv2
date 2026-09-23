"""Signal 6 Model-2 (ExpD) paper gate — forward record, no live trading.

Run daily (e.g. after the US close). For each of TODAY'S break events:

1. train the regime-gated logistic on every sample whose entry is BEFORE
   the start of today — today's data cannot influence its own decision;
2. compute P(win) and the regime threshold from that train window;
3. append the PASS/REJECT decision to `reports/signal6_paper.jsonl`.

Production Signal 6 is untouched: this only accumulates the forward OOS
record the spec §30 "fully OOS test" gate requires before any deployment
decision. The promotion rule is pre-committed, not to be re-tuned after
looking at the paper log: >= 30 settled PASS trades and OOS win rate at
least 10pp above the ~42% break-even (i.e. >= 52%) on both feeds' paper
records combined.

Usage:  python -m backtest.tools.paper_signal6 [--days N] [--notify]
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ..data.db import Database
from ..engine.signal6_retrain import (EXP_C_EXTRA, EXP_LEG, FEATURE_KEYS,
                                      LogisticPwin, build_samples, matrix)
from ..engine.signal7 import THRESHOLD_CAP
from ..indicators.base import IndicatorConfig
from .retrain_signal6 import pick_regime_thresholds, regime_of

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
LOG_PATH = Path(__file__).resolve().parents[1] / "reports" / \
    "signal6_paper.jsonl"

# The paper gate runs EXPD — the ladder's winner (60.6% OOS walk-forward /
# 65.8% cross-feed). Leg features were tested (ExpE: 59.1%, no gain over
# ExpD) and are deliberately EXCLUDED: the winning config stays frozen.
NEW5 = {"adx_slope", "ema_slope_change", "atr_pctile_change",
        "vol_change_ratio", "break_velocity"}
M1K = tuple(k for k in FEATURE_KEYS if k not in (NEW5 | set(EXP_LEG)))
EXPD_KEYS = tuple(dict.fromkeys(M1K + EXP_C_EXTRA))


def myt_today0() -> int:
    now = datetime.now(MYT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def evaluate(symbol: str, data_interval: str, today0: int,
             with_db=None, min_train: int = 60) -> list[dict]:
    """Today's events judged by a model trained strictly on earlier data."""
    db = with_db
    own = db is None
    if own:
        from ..data.db import Database as _D
        db = _D()
    try:
        samples = build_samples(db, symbol, IndicatorConfig(),
                                data_interval=data_interval)
    finally:
        if own:
            db.close()
    if not samples:
        return []

    train = [s for s in samples if s["entry_time"] < today0]
    today = [s for s in samples if s["entry_time"] >= today0]
    if not today or len(train) < min_train:
        return []

    X, y = matrix(samples)
    col = {k: i for i, k in enumerate(FEATURE_KEYS)}
    use = [col[k] for k in EXPD_KEYS]
    # samples are entry-time sorted and train = everything before today0,
    # so train rows are exactly the first len(train) rows.
    n_tr = len(train)
    Xtr, ytr = X[:n_tr][:, use], y[:n_tr]
    resolved = np.array([s["label"]["resolved"] for s in train])
    if resolved.sum() < max(1, (2 * min_train) // 3):
        return []
    m = LogisticPwin().fit(Xtr[resolved], ytr[resolved])
    thresholds = pick_regime_thresholds(
        [s for s, ok in zip(train, resolved) if ok],
        m.predict(Xtr[resolved]))
    thresholds = {k: min(v, THRESHOLD_CAP) for k, v in thresholds.items()}

    out = []
    for k, s in enumerate(today):
        xi = X[n_tr + k][use].reshape(1, -1)
        p = float(m.predict(xi)[0])
        thr = thresholds[regime_of(s)]
        out.append({
            "ts_ms": int(time.time() * 1000),
            "symbol": symbol,
            "day_myt": s["day_myt"],
            "event_type": s["event_type"],
            "entry_time": s["entry_time"],
            "side": s["side"],
            "regime": regime_of(s),
            "p_win": round(p, 3),
            "threshold": thr,
            "decision": "PASS" if p >= thr else "REJECT",
            # outcome filled by later runs once the 24h horizon passes
            "label_at_decision": {
                "resolved": s["label"]["resolved"],
                "reason": s["label"]["reason"]},
            "train_cutoff_ms": today0,
        })
    return out


def backfill_outcomes() -> int:
    """Second pass over the paper log: settle decisions whose horizon has
    passed, so the forward record carries its own outcomes."""
    if not LOG_PATH.exists():
        return 0
    rows = [json.loads(ln) for ln in
            LOG_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    changed = 0
    by_key = {}
    for r in rows:
        by_key[(r["symbol"], r["entry_time"])] = r
    with Database() as db:
        for (sym, et), r in by_key.items():
            if r.get("settled"):
                continue
            iv = "5m" if sym.startswith("MT5:") else "1m"
            from ..data.db import CandleRepository
            from ..engine.signal6_retrain import _stats  # noqa: F401
            fresh = build_samples(db, sym, IndicatorConfig(),
                                  data_interval=iv)
            match = [s for s in fresh if s["entry_time"] == et]
            if not match:
                continue
            lab = match[0]["label"]
            if lab["resolved"]:
                r["settled"] = True
                r["outcome"] = {"win": lab["win"], "net": lab["net"],
                                "reason": lab["reason"],
                                "realized_R": lab["realized_R"]}
                changed += 1
    if changed:
        LOG_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")
    return changed


def summary() -> str:
    if not LOG_PATH.exists():
        return "paper log empty"
    rows = [json.loads(ln) for ln in
            LOG_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    settled = [r for r in rows if r.get("settled")]
    passed = [r for r in settled if r["decision"] == "PASS"]
    wins = sum(1 for r in passed if r["outcome"]["win"])
    n = len(passed)
    return (f"paper record: {len(rows)} decisions, {len(settled)} settled; "
            f"PASS settled n={n} win={100 * wins / n:.1f}%" if n else
            f"paper record: {len(rows)} decisions, {len(settled)} settled; "
            f"no settled PASS trades yet "
            f"(promotion gate: n>=30 and win>=52%)")


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    notify = "--notify" in argv
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    today0 = myt_today0()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    decisions = []
    with Database() as db:
        decisions += evaluate("XAUUSDT", "1m", today0, with_db=db)
        decisions += evaluate("MT5:GOLD", "5m", today0, with_db=db)
    if decisions:
        seen = set()
        if LOG_PATH.exists():
            seen = {(json.loads(ln)["symbol"],
                     json.loads(ln)["entry_time"]) for ln in
                    LOG_PATH.read_text(encoding="utf-8").splitlines()
                    if ln.strip()}
        fresh = [d for d in decisions
                 if (d["symbol"], d["entry_time"]) not in seen]
        if fresh:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                for d in fresh:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")
            log.info("paper: %d new decision(s) appended", len(fresh))
    settled = backfill_outcomes()
    if settled:
        log.info("paper: settled %d past decision(s)", settled)
    print(summary())

    if notify:
        from ..notify import telegram as tg
        tg.install_log_redaction()
        n = tg.build()
        for d in decisions:
            if d["decision"] == "PASS":
                n.send(tg.alert("PAPER PASS — signal6 Model2",
                                f"{d['symbol']} {d['event_type']} "
                                f"p={d['p_win']} >= {d['threshold']} "
                                f"({d['regime']}) — paper only, NOT a trade"),
                       priority=tg.P2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
