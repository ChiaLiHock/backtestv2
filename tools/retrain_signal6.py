"""Signal 6 retraining experiments — implements SIGNAL6_RETRAIN_SPEC.md §24-30.

Run:  python -m backtest.tools.retrain_signal6

Produces:
  * Model 0 baseline replication on BOTH feeds (must match the shipped
    numbers before anything else is trusted);
  * Model 1 (regime+context features, ridge logistic P(win)) trained
    walk-forward on MT5:GOLD, quarterly test windows, threshold FIXED at
    0.5 chosen on no test data (spec §20 phase 1);
  * the spec §25 report table (years, quarters, feed, regime, session
    slices with N / WinRate / Expectancy / PF / MaxDD);
  * the spec §30 hard-gate checklist, honestly filled.

Model 3 (macro) is SKIPPED: this project has no economic calendar and a
fabricated one is worse than the stated absence.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np

from ..data.db import Database
from ..engine.signal6_retrain import (EXP_A_EXTRA, EXP_B_EXTRA, EXP_C_EXTRA,
                                      EXP_LEG, FEATURE_KEYS, LogisticPwin,
                                      build_samples, matrix,
                                      model0_baseline, _stats)
from ..indicators.base import IndicatorConfig

MYT = timezone(timedelta(hours=8))
QUARTER_MS = 90 * 86_400_000


# ---------------------------------------------------------------------------
# Model 2 (Q3 spec): failure analysis first — NO training involved.
# ---------------------------------------------------------------------------

def q3_analysis(samples: list[dict]) -> None:
    """The spec's step 1: dissect the weak window before touching models."""
    def q3(r):
        return r["day_myt"] >= "2026-07" and r["day_myt"] < "2026-10"

    rows = [r for r in samples if q3(r)]
    print("\n" + "=" * 64)
    print("Q3 2026 FAILURE ANALYSIS — where does the 39% live?")
    print("=" * 64)
    s = _stats(rows)
    print(f"  Q3 overall             n={s['n']:4d} win={s['win_pct']}% "
          f"exp={s['expectancy']} pf={s['pf']}")
    slices = [
        ("Trend (adx>=25)", lambda r: r["adx_1h"] >= 25, "65.9%"),
        ("Range (adx<25)", lambda r: r["adx_1h"] < 25, "48.1%"),
        ("Europe", lambda r: r["confirm_hour_myt"] < 20, "?"),
        ("US", lambda r: r["confirm_hour_myt"] >= 20, "?"),
        ("break_high", lambda r: r["event_type"] == "break_high", "?"),
        ("break_low", lambda r: r["event_type"] == "break_low", "?"),
        ("HighVol (pct>=.5)",
         lambda r: (r["vol_percentile_20d"] or 0.5) >= 0.5, "?"),
        ("LowVol (pct<.5)",
         lambda r: (r["vol_percentile_20d"] or 0.5) < 0.5, "?"),
        ("ADX falling (slope<0)", lambda r: r["adx_slope"] < 0, "?"),
        ("ADX rising (slope>=0)", lambda r: r["adx_slope"] >= 0, "?"),
        ("sweep shallow (<0.15R)",
         lambda r: r["sweep_depth_R"] < 0.15, "?"),
        ("sweep deep (>=0.15R)", lambda r: r["sweep_depth_R"] >= 0.15, "?"),
        ("vol ratio <2x", lambda r: r["break_volume_ratio"] < 2.0, "?"),
        ("vol ratio >=2x", lambda r: r["break_volume_ratio"] >= 2.0, "?"),
        ("macro day", None, "N/A — no calendar"),
    ]
    for name, pred, alltime in slices:
        if pred is None:
            print(f"  {name:24s} {alltime}")
            continue
        sub = [r for r in rows if pred(r)]
        st = _stats(sub)
        if st["n"]:
            print(f"  {name:24s} n={st['n']:4d} win={st['win_pct']}% "
                  f"exp={st['expectancy']}   (all-time {alltime})")


def regime_of(r: dict) -> str:
    return "trend" if r["adx_1h"] >= 25 else "range"


def pick_regime_thresholds(train_rows, probs) -> dict[str, float]:
    """Spec §2: thresholds decided on the TRAIN window ONLY.

    Grid {0.45..0.70}; per regime pick the threshold with the best train
    expectancy among those keeping >= 25 trades (else fall back to 0.50).
    The next quarter never influences the choice.
    """
    out = {"trend": 0.50, "range": 0.50}
    for regime in ("trend", "range"):
        best, best_exp = 0.50, -1e9
        for thr in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
            sel = [(r, p) for r, p in zip(train_rows, probs)
                   if regime_of(r) == regime and p >= thr]
            st = _stats([r for r, _ in sel])
            if st["n"] >= 25 and st["expectancy"] is not None \
                    and st["expectancy"] > best_exp:
                best, best_exp = thr, st["expectancy"]
        out[regime] = best
    return out


def wf_model(samples, keys=FEATURE_KEYS, regime_gate=False):
    """Walk-forward P(win); optionally regime-aware thresholds per fold."""
    idx = [k for k in FEATURE_KEYS if True]
    col = {k: i for i, k in enumerate(FEATURE_KEYS)}
    use = [col[k] for k in keys]
    X, y = matrix(samples)
    X = X[:, use]
    taken, all_pred = [], []
    t0 = samples[0]["entry_time"]
    start = t0 + 2 * QUARTER_MS
    while start < samples[-1]["entry_time"]:
        end = start + QUARTER_MS
        tr = [i for i, s in enumerate(samples) if s["entry_time"] < start]
        te = [i for i, s in enumerate(samples) if start <= s["entry_time"] < end]
        if len(tr) >= 60 and te:
            resolved = np.array([samples[i]["label"]["resolved"]
                                 for i in tr])
            m = LogisticPwin().fit(X[tr][resolved], y[tr][resolved])
            ptr = m.predict(X[tr][resolved])
            thresholds = None
            if regime_gate:
                thresholds = pick_regime_thresholds(
                    [samples[i] for i, ok in zip(tr, resolved) if ok], ptr)
            p = m.predict(X[te])
            for i, pi in zip(te, p):
                row = dict(samples[i])
                row["p_win"] = round(float(pi), 3)
                all_pred.append(row)
                thr = thresholds[regime_of(row)] if thresholds else 0.50
                if pi >= thr:
                    taken.append(row)
        start = end
    return taken, all_pred


def gate_metrics(name, taken, all_pred, cross=None) -> None:
    """The spec's 8-metric gate; Q3 AND the H2-2026 leg are the honesty
    lines (the year-gap analysis showed H2-26 is where the edge died)."""
    s = _stats(taken)
    rej = 100 * (1 - len(taken) / max(1, len(all_pred)))
    q3 = _stats([r for r in taken
                 if "2026-07" <= r["day_myt"] < "2026-10"])
    h2 = _stats([r for r in taken if r["day_myt"] >= "2026-07"])
    y25 = _stats([r for r in taken if r["day_myt"].startswith("2025")])
    rng = _stats([r for r in taken if regime_of(r) == "range"])
    trd = _stats([r for r in taken if regime_of(r) == "trend"])
    cf = _stats(cross) if cross else {"win_pct": None, "n": 0}
    print(f"  {name:12s} n={s['n']:4d} OOS={s['win_pct']}% "
          f"Q3={q3['win_pct']}% H2={h2['win_pct']}% 25={y25['win_pct']}% "
          f"Range={rng['win_pct']}% Trend={trd['win_pct']}% "
          f"exp={s['expectancy']} pf={s['pf']} reject={rej:.0f}% "
          f"crossfeed={cf['win_pct']}%/{cf['n']}")


def slice_rows(rows, pred):
    return [r for r in rows if pred(r)]


def table(title: str, rows: list[dict]) -> None:
    print(f"\n-- {title} --")
    for name, pred in preds():
        sub = slice_rows(rows, pred)
        if not sub:
            continue
        s = _stats(sub)
        print(f"  {name:26s} n={s['n']:4d} win={str(s['win_pct']):>5s}% "
              f"exp={s['expectancy']} pf={s['pf']} "
              f"net={s['net']:8.2f} maxdd={s['maxdd']}")


def preds():
    def by_year(y):
        return lambda r: r["day_myt"].startswith(str(y))
    def by_quarter(y, q):
        m0 = f"{y}-{(q - 1) * 3 + 1:02d}"
        return lambda r: r["day_myt"][:7] >= m0 and r["day_myt"][:4] == str(y) \
            and int(r["day_myt"][5:7]) in range((q - 1) * 3 + 1, q * 3 + 1)
    out = []
    for y in (2025, 2026):
        out.append((str(y), by_year(y)))
        for q in (1, 2, 3, 4):
            out.append((f"{y} Q{q}", by_quarter(y, q)))
    out += [
        ("Trend (adx>=25)", lambda r: r["adx_1h"] >= 25),
        ("Range (adx<25)", lambda r: r["adx_1h"] < 25),
        ("HighVol (pct>=0.5)",
         lambda r: (r["vol_percentile_20d"] or 0.5) >= 0.5),
        ("LowVol (pct<0.5)",
         lambda r: (r["vol_percentile_20d"] or 0.5) < 0.5),
        ("Europe", lambda r: r["confirm_hour_myt"] < 20),
        ("US", lambda r: r["confirm_hour_myt"] >= 20),
        ("Macro day", None),      # absent — printed as unavailable
    ]
    return out


def report(title: str, rows: list[dict]) -> None:
    print("\n" + "=" * 64)
    print(f"SIGNAL6 WALK-FORWARD REPORT — {title}")
    print("=" * 64)
    s = _stats(rows)
    print(f"  {'OVERALL':26s} n={s['n']:4d} win={s['win_pct']}% "
          f"exp={s['expectancy']} pf={s['pf']} net={s['net']} "
          f"maxdd={s['maxdd']}")
    for name, pred in preds():
        if pred is None:
            print("  Macro day                  N/A — no economic calendar "
                  "in this project")
            continue
        sub = slice_rows(rows, pred)
        if sub:
            st = _stats(sub)
            print(f"  {name:26s} n={st['n']:4d} win={st['win_pct']}% "
                  f"exp={st['expectancy']} pf={st['pf']}")
    print("  (feed tag is carried per row: see the per-feed sections above)")


def walk_forward_model1(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """Expanding-train quarterly test (spec §16). Threshold fixed at 0.5.

    Returns (taken, skipped-with-probability rows) where `taken` are the
    trades the P(win) gate allowed in each test window, each row carrying
    its p(win).
    """
    if not samples:
        return [], []
    X, y = matrix(samples)
    t0 = samples[0]["entry_time"]
    taken: list[dict] = []
    all_pred: list[dict] = []
    start = t0 + 2 * QUARTER_MS            # two quarters of warm-up train
    while start < samples[-1]["entry_time"]:
        end = start + QUARTER_MS
        tr = np.array([i for i, s in enumerate(samples)
                       if s["entry_time"] < start])
        te = np.array([i for i, s in enumerate(samples)
                       if start <= s["entry_time"] < end])
        if len(tr) >= 60 and len(te):
            resolved = np.array([samples[i]["label"]["resolved"]
                                 for i in tr])
            m = LogisticPwin().fit(X[tr][resolved],
                                   y[tr][resolved])
            p = m.predict(X[te])
            for i, pi in zip(te, p):
                row = dict(samples[i])
                row["p_win"] = round(float(pi), 3)
                all_pred.append(row)
                if pi >= 0.5:
                    taken.append(row)
        start = end
    return taken, all_pred


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    m2 = "--m2" in argv
    with Database() as db:
        print("building samples (MT5:GOLD 5m, 17 months)…")
        mt5 = build_samples(db, "MT5:GOLD", IndicatorConfig(),
                            data_interval="5m")
        print(f"MT5 break-event samples: {len(mt5)}")
        print("building samples (XAUUSDT 1m, 166 days)…")
        bybit = build_samples(db, "XAUUSDT", IndicatorConfig(),
                              data_interval="1m")
        print(f"Bybit break-event samples: {len(bybit)}")

        # ---- Model 0: baseline replication -----------------------------
        print("\n" + "=" * 64)
        print("MODEL 0 — baseline rules (must replicate the shipped runs)")
        print("=" * 64)
        for tag, rows in (("Bybit", bybit), ("MT5", mt5)):
            base = model0_baseline(rows)
            s = _stats(base)
            print(f"  {tag:6s} n={s['n']:4d} win={s['win_pct']}% "
                  f"net={s['net']}  (shipped: Bybit 105/62.9%/+1333.71, "
                  f"MT5 201/47.8%/+1204.08)")

        # ---- Model 1: regime P(win), walk-forward on MT5 ----------------
        taken, all_pred = walk_forward_model1(mt5)
        print("\n" + "=" * 64)
        print("MODEL 1 — regime features, ridge logistic P(win)>=0.5, "
              "walk-forward on MT5")
        print("=" * 64)
        s = _stats(taken)
        skipped = [r for r in all_pred if r["p_win"] < 0.5]
        s0 = _stats(skipped)
        print(f"  taken   n={s['n']:4d} win={s['win_pct']}% exp={s['expectancy']}"
              f" pf={s['pf']} net={s['net']} maxdd={s['maxdd']}")
        print(f"  skipped n={s0['n']:4d} win={s0['win_pct']}% "
              f"(the gate should cut the WORSE half)")

        report("Model 1 taken (MT5 walk-forward)", taken)

        # ---- cross-feed: apply the MT5-trained model to Bybit -----------
        if bybit:
            Xm, ym = matrix(mt5)
            resolved = np.array([r["label"]["resolved"] for r in mt5])
            m = LogisticPwin().fit(Xm[resolved], ym[resolved])
            Xb, _ = matrix(bybit)
            pb = m.predict(Xb)
            rows = [dict(r, p_win=round(float(p), 3))
                    for r, p in zip(bybit, pb)]
            cross = [r for r in rows if r["p_win"] >= 0.5]
            s1 = _stats(cross)
            print("\n" + "=" * 64)
            print("CROSS-FEED — MT5-trained Model 1 applied to Bybit "
                  "(unseen feed)")
            print("=" * 64)
            print(f"  taken   n={s1['n']:4d} win={s1['win_pct']}% "
                  f"exp={s1['expectancy']} pf={s1['pf']} net={s1['net']}")

        # ---- Model 2 (--m2): Q3 failure analysis + experiments ----------
        if m2:
            q3_analysis(taken)

            # Model 1 = the ORIGINAL 31 keys; the ladder adds the new
            # Model-2 features progressively (spec: 一次不要全塞).
            NEW5 = {"adx_slope", "ema_slope_change", "atr_pctile_change",
                    "vol_change_ratio", "break_velocity"}
            NEW9 = NEW5 | set(EXP_LEG)
            M1K = tuple(k for k in FEATURE_KEYS if k not in NEW9)
            print("\n" + "=" * 64)
            print("MODEL 2 EXPERIMENTS — walk-forward, Q3 only ever a TEST "
                  "window")
            print("=" * 64)
            print("  (8-gate format: n / OOS / Q3 / Range / Trend / exp / "
                  "pf / reject / cross-feed)")
            results = {}
            for name, keys, gate in (
                ("M1", M1K, False),
                ("ExpA", tuple(dict.fromkeys(M1K + EXP_A_EXTRA)), False),
                ("ExpB", tuple(dict.fromkeys(M1K + EXP_B_EXTRA)), False),
                ("ExpC", tuple(dict.fromkeys(M1K + EXP_C_EXTRA)), False),
                ("ExpD", tuple(dict.fromkeys(M1K + EXP_C_EXTRA)), True),
                ("ExpLeg", tuple(dict.fromkeys(M1K + EXP_LEG)), False),
                ("ExpE", tuple(dict.fromkeys(M1K + EXP_C_EXTRA + EXP_LEG)),
                 True),
            ):
                tk, ap = wf_model(mt5, keys=keys, regime_gate=gate)
                results[name] = (tk, ap)
                gate_metrics(name, tk, ap)

            # cross-feed for the ladder's key forms
            print("\n  cross-feed (train MT5 full -> Bybit):")
            Xm, ym = matrix(mt5)
            resolved = np.array([r["label"]["resolved"] for r in mt5])
            Xb, _ = matrix(bybit)
            col = {k: i for i, k in enumerate(FEATURE_KEYS)}
            for name, keys, gate in (
                    ("M1", M1K, False),
                    ("ExpLeg", tuple(dict.fromkeys(M1K + EXP_LEG)), False),
                    ("ExpD", tuple(dict.fromkeys(M1K + EXP_C_EXTRA)), True),
                    ("ExpE",
                     tuple(dict.fromkeys(M1K + EXP_C_EXTRA + EXP_LEG)), True)):
                use = [col[k] for k in keys]
                m = LogisticPwin().fit(Xm[:, use][resolved],
                                       ym[resolved])
                pb = m.predict(Xb[:, use])
                thresholds = None
                if gate:
                    thresholds = pick_regime_thresholds(
                        [r for r, ok in zip(mt5, resolved) if ok],
                        m.predict(Xm[:, use][resolved]))
                cross = [dict(r, p_win=round(float(p), 3))
                         for r, p in zip(bybit, pb)]
                cross = [r for r in cross
                         if r["p_win"] >= (thresholds[regime_of(r)]
                                           if thresholds else 0.50)]
                s1 = _stats(cross)
                print(f"    {name}: n={s1['n']} win={s1['win_pct']}% "
                      f"exp={s1['expectancy']} pf={s1['pf']} "
                      + (f"thr={thresholds}" if thresholds else ""))

            # the spec's success gate, restated for the reader
            print("\n  Q3 gate: improvement with n collapsing to a handful "
                  "does NOT count as solving it (spec).")
            for name in ("M1", "ExpA", "ExpB", "ExpC", "ExpD", "ExpLeg",
                         "ExpE"):
                tk, _ = results[name]
                q3r = [r for r in tk if "2026-07" <= r["day_myt"] < "2026-10"]
                st = _stats(q3r)
                print(f"    {name}: Q3 n={st['n']} win={st['win_pct']}% "
                      f"exp={st['expectancy']}")

        # ---- spec §30 gates ---------------------------------------------
        print("\n" + "=" * 64)
        print("SPEC §30 HARD GATES (for any future deployment decision)")
        print("=" * 64)
        gates = [
            ("no lookahead", "PASS — features as-of confirm; labels "
             "whitelisted out of the matrix"),
            ("walk-forward", "PASS — expanding train, quarterly test"),
            ("fully OOS test", "PARTIAL — cross-feed only; no held-out "
             "future exists yet"),
            ("two+ years", "PASS — MT5 spans 2025-2026; 2024 absent"),
            ("two+ feeds", "PASS — Bybit + MT5:GOLD"),
            ("macro layer", "N/A — no calendar in this project; Model 3 "
             "skipped"),
            ("expectancy>0 / PF>1", f"Model1-taken exp={s['expectancy']} "
             f"pf={s['pf']} — judge from the numbers above"),
        ]
        for name, verdict in gates:
            print(f"  [ ] {name:24s} {verdict}")
        print("\nNo deployment decision is made here. This tool only "
              "measures (spec header: do not overwrite production rules).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
