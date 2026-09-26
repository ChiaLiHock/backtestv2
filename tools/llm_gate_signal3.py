"""Signal 3 LLM gate — a local-Ollama reviewer over the v3 features.

Positioning (decided 2026-09-25, after the v2 spec research):

The LLM does NOT see charts or prices directly and does not "predict".
It is a STRUCTURED REVIEWER: each signal3 candidate is rendered as a
fixed feature card (the same 13 causal features the v3 harness logs,
plus the bucket statistics measured on the TRAIN window only), and the
model is asked for a verdict (PASS/REJECT) with a confidence. The same
card always renders identically, so verdicts are cacheable and the
whole gate is backtestable offline — the two properties a trading
component in this repo must have.

Validation follows the spec's rules: fit/calibrate nothing on test
windows; score the walk-forward OOS once; report expR/PF/lo95.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:\Users\User\Downloads\backtest")
sys.path.insert(0, r"C:\Users\User\Downloads\backtest\backtest")

import numpy as np

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "v3", Path(__file__).with_name("backtest_signal3_v3.py"))
v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v3)

CACHE_PATH = Path(__file__).parent / ".llm_gate_cache.json"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "llama3.2:3b"

PROMPT = """You are a trading-strategy reviewer. You receive one
candidate reversal trade described by measured features. Recent
statistics from this strategy (same setup family, measured on past
data only) are given. Decide whether this specific candidate is
above or below the family's average quality.

Answer with EXACTLY one line: PASS or REJECT.
PASS if you judge the candidate better than the family average,
REJECT otherwise. No other text.

FEATURES:
{card}

FAMILY STATS (past window):
{fam}
ANSWER:"""


def card(x: dict) -> str:
    return "\n".join([
        f"side: {x['side']}",
        f"session: {x['session']}",
        f"zone_width: {x['zone']:.2f}",
        f"rr (reward/risk): {x['rr']:.2f}",
        f"sweep_depth/ATR: {x['sweep_depth_atr']:.2f}",
        f"reclaim_bars: {x['reclaim_bars']}",
        f"break_count: {x['breaks']}",
        f"risk/ATR: {x['risk_atr']:.2f}",
        f"target/ATR: {x['opp_dist_atr']:.2f}",
        f"adx: {x['adx']:.1f}",
        f"di_spread: {x['di_spread']:+.1f}",
        f"ema_align(ATR): {x['ema_align']:+.2f}",
        f"atr_pct: {x['atr_pct']:.2f}",
        f"prior_failed_breakout: {x['pfb']}",
    ])


def family_stats(rows) -> str:
    if not rows:
        return "no data"
    r = np.array([x["r"] for x in rows])
    adx = np.array([x["adx"] for x in rows])
    fast = np.array([x["reclaim_bars"] <= 2 for x in rows])
    return "\n".join([
        f"n: {len(r)}",
        f"mean_expR: {r.mean():+.3f}",
        f"win_rate: {100 * (r > 0).mean():.1f}%",
        f"expR_when_adx_ge_25: "
        f"{r[adx >= 25].mean() if (adx >= 25).any() else float('nan'):+.3f}",
        f"expR_when_adx_lt_25: "
        f"{r[adx < 25].mean() if (adx < 25).any() else float('nan'):+.3f}",
        f"expR_when_fast_reclaim: "
        f"{r[fast].mean() if fast.any() else float('nan'):+.3f}",
        f"expR_when_slow_reclaim: "
        f"{r[~fast].mean() if (~fast).any() else float('nan'):+.3f}",
    ])


_cache: dict | None = None


def cache() -> dict:
    global _cache
    if _cache is None:
        _cache = (json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                  if CACHE_PATH.exists() else {})
    return _cache


def ask_ollama(prompt: str, retries: int = 2) -> str:
    import httpx
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f"{OLLAMA}/api/generate",
                           json={"model": MODEL, "prompt": prompt,
                                 "stream": False,
                                 "options": {"temperature": 0,
                                             "num_predict": 4}},
                           timeout=60)
            txt = r.json().get("response", "")
            up = txt.strip().upper()
            if "PASS" in up and "REJECT" not in up:
                return "PASS"
            if "REJECT" in up:
                return "REJECT"
            return "UNPARSEABLE:" + txt.strip()[:40]
        except Exception as exc:
            if attempt == retries:
                return f"ERROR:{str(exc)[:60]}"
            time.sleep(1.0)
    return "ERROR"


def verdict(x: dict, fam: dict) -> str:
    key = card(x) + "||" + fam
    c = cache()
    if key in c:
        return c[key]
    v = ask_ollama(PROMPT.format(card=card(x), fam=fam))
    c[key] = v
    CACHE_PATH.write_text(json.dumps(c), encoding="utf-8")
    return v


def stats(rows):
    if not rows:
        return "n=0"
    r = np.array([x["r"] for x in rows])
    gl = -r[r < 0].sum()
    pf = r[r > 0].sum() / gl if gl > 0 else float("inf")
    return (f"n={len(r):4d} win={100*(r>0).mean():5.1f}% "
            f"expR={r.mean():+.3f} pf={pf:5.2f} sumR={r.sum():+7.1f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--feed", default="XAU")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--limit", type=int, default=200,
                   help="max candidates to review (cost control)")
    p.add_argument("--walk", action="store_true",
                   help="walk-forward: family stats from trailing window "
                        "only, verdicts scored once")
    a = p.parse_args()
    globals()["MODEL"] = a.model

    rows = sorted(v3.candidates(v3.load(a.feed)),
                  key=lambda x: x["entry_time"])
    if a.walk:
        # trailing family stats, 60/40 split, score the last 40% once
        cut = int(len(rows) * 0.6)
        oos = rows[cut:]
        fam_rows = rows[:cut]
        fam = family_stats(fam_rows)
        print(f"walk-forward on {a.feed}: train n={len(fam_rows)}, "
              f"scoring {len(oos)} OOS candidates")
        kept = []
        for x in oos[: a.limit]:
            v = verdict(x, fam)
            x["llm"] = v
            if v == "PASS":
                kept.append(x)
        r = np.array([x["r"] for x in kept]) if kept else np.array([0.0])
        rng = np.random.default_rng(0)
        lo95 = (float(np.percentile(
            rng.choice(r, (2000, len(r))).mean(1), 2.5))
            if len(kept) > 1 else float("nan"))
        print(f"  LLM-GATED OOS {stats(kept)}  lo95={lo95:+.3f}")
        print(f"  baseline OOS   {stats(oos)}")
        from collections import Counter
        print("  verdicts:", Counter(x.get("llm", "-")
                                     for x in oos[: a.limit]))
    else:
        fam = family_stats(rows)
        kept = []
        for x in rows[: a.limit]:
            v = verdict(x, fam)
            x["llm"] = v
            if v == "PASS":
                kept.append(x)
        print(f"{a.feed} (in-sample — read nothing into this):")
        print(f"  LLM-GATED {stats(kept)}")
        print(f"  baseline  {stats(rows)}")
        from collections import Counter
        print("  verdicts:", Counter(x.get("llm", "-")
                                     for x in rows[: a.limit]))


if __name__ == "__main__":
    main()