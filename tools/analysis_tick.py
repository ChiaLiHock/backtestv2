"""One scheduled analysis tick: has anything changed, and if so, hand over the blob.

    python -m backtest.tools.analysis_tick
    python -m backtest.tools.analysis_tick --port 8787 --symbol XAUUSDT

Run every 30 minutes by a scheduled task. It prints one of two things:

* ``NO_CHANGE`` — nothing worth a fresh read happened. One line is appended to
  today's journal and the caller stops. Costs no model tokens at all.
* ``CHANGED: <reasons>`` followed by the full analysis blob — the caller runs the
  mandate carried inside that blob and appends its answer to the journal.

## Why the change test is here and not in the prompt

Asking a model to diff two 22 KB snapshots every half hour is expensive, slow and
unreliable — and worse, a model asked "analyse this" 48 times a day will
**manufacture significance to fill the format** on the 43 occasions nothing
happened. That is the failure mode the mandate itself calls the worst one.

So the question "did anything actually change" is answered by deterministic code
comparing a small fingerprint, and the model is only woken for the handful of
ticks that carry news.

## Two consumers, two state files

`--record-only` is the **recorder**: run it from Windows Task Scheduler and it
appends one heartbeat line every 30 minutes, forever, whether or not Claude is
open, idle, or permitted. It never prints the blob and never asks anyone to
analyse anything.

Without the flag it is the **analyst's** tick: on a change it writes a heading
and hands the blob over for a model to write up.

They keep SEPARATE fingerprints (`--state-key`), and that separation is the whole
point. Sharing one would mean the recorder consumed every change a few seconds
before the analyst looked, and the analyst would then see a permanently quiet
market. Two keys, two independent "what did I last see" answers.

The division of labour matters more than it looks: the record is the part that
must never be missed, and it is now the part that depends on nothing. The
analysis is the part that needs a model, and it is allowed to be best-effort.

## What counts as a change — two tiers, and the first version got this wrong

**TRIGGERS wake a model. CONTEXT is recorded and nothing more.**

The first version treated every tracked field as a trigger. Measured over one
night — twelve consecutive ticks — **all twelve** were flagged as changed, and
the things that actually matter (a signal sent, a gate veto, a stale feed) fired
**zero** times. It would have produced twelve analyses of nothing.

What was firing:

    OI quadrant       6/12    sign(dPrice) x sign(dOI) on a 15m grid. In a chop
                              this flips almost every bar. It is an indicator,
                              not an event.
    blocking legs     5/12    the SET churns constantly as legs flicker; what
                              matters is only whether it reached EMPTY
    structural bias   3/12    oscillates NEUTRAL <-> BEARISH <-> BULLISH
    danger band       4/12    one-step drifts across a boundary

None of those change what anyone would do. So they moved to CONTEXT: still
printed on every line, so a day still reads as a timeline, but they no longer
cost a model call.

A trigger has to be something a person would act on:

    a signal was SENT              the whole point of the system
    the gate VETOED one            the interesting case - the rule wanted in
    the rule became READY          blocking went empty; it is about to fire
    the feed went STALE            every reading below it describes THEN
    the watcher became unreachable silence that means nothing is running
    session applicability flipped  weekend/market-closed changes which rules apply

Note what this costs: a genuine regime change that produces no signal now passes
without a write-up. That is the intended trade. The recorder still logs it, and a
journal of 43 padded entries is one nobody reads on the day it matters.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
import pathlib
from pathlib import Path

MYT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "analysis"


def state_path(key: str) -> pathlib.Path:
    return OUT_DIR / f".state-{key}.json"

# Danger is a percentage, but a 2-point move is not news. Bands are what the
# reader actually acts on, so the fingerprint carries the band.
DANGER_BANDS = ((70, "high"), (50, "elevated"), (30, "moderate"), (0, "low"))

# Fields whose CHANGE is worth a model call. Everything else in the fingerprint
# is context: tracked and printed, never a trigger. See the module docstring for
# the measurement that produced this split.
TRIGGER_KEYS = frozenset({
    "last_signal_bar", "sent", "vetoed", "gate_passed",
    "rule_ready", "stale", "timing_rules",
})


def band(v) -> str:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return "unknown"
    for floor, name in DANGER_BANDS:
        if n >= floor:
            return name
    return "low"


def fetch(port: int, symbol: str, timeout: float = 30.0) -> tuple[str | None, dict, str | None]:
    """(blob, status, error). Never raises."""
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(
                f"{base}/api/analysis?symbol={urllib.request.quote(symbol)}",
                timeout=timeout) as r:
            blob = r.read().decode("utf-8")
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        with urllib.request.urlopen(f"{base}/status.json", timeout=timeout) as r:
            status = json.loads(r.read().decode("utf-8"))
    except Exception:
        status = {}
    return blob, status, None


def fingerprint(blob: str, status: dict, symbol: str) -> dict:
    """The small set of facts whose CHANGE is worth a fresh read."""
    rule = (status.get("rule") or {})
    risk = ((status.get("symbols") or {}).get(symbol) or {}).get("risk") or {}
    lo, sh = risk.get("long") or {}, risk.get("short") or {}
    gate = rule.get("gate") or {}

    def grab(pattern: str, default: str = "") -> str:
        # MULTILINE, or `^` anchors to the start of the whole 22 KB blob and
        # every one of these silently returns "" — a fingerprint field that is
        # always empty can never register a change, so the tick would go quiet
        # on exactly the transitions it exists to catch.
        m = re.search(pattern, blob, re.M)
        return m.group(1).strip() if m else default

    return {
        # A new signal is the single most important thing that can happen.
        "last_signal_bar": rule.get("bar_open_ms") if rule.get("sent") else None,
        "sent": bool(rule.get("sent")),
        "vetoed": bool(rule.get("vetoed")),
        "gate_passed": gate.get("passed"),
        # Which leg is blocking. "close to firing" and "nowhere near" are
        # different situations and the blocking set is how you tell.
        "blocking": sorted(rule.get("blocking") or []),
        # The leg SET churns every few bars and is context. Whether it reached
        # EMPTY is the event — that is the rule about to fire.
        "rule_ready": not (rule.get("blocking") or []),
        "bias": risk.get("bias"),
        "long_danger_band": band(lo.get("danger")),
        "short_danger_band": band(sh.get("danger")),
        "stale": "This feed is" in blob and "behind" in blob,
        "session": grab(r"^session\s+(\S+)", ""),
        "adx_regime": "ranging" if "ranging" in blob else "trending",
        "oi_quadrant": grab(r"^QUADRANT\s+(.+)$"),
        "funding_verdict": grab(r"^VERDICT\s+(.+)$"),
        "timing_rules": grab(r"^timing rules apply\s+(\S+)"),
    }


def diff(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """``(triggers, context)`` — reasons to wake a model, and reasons merely to note.

    An empty ``triggers`` means quiet, however much ``context`` moved.
    """
    if not old:
        return (["first run — no previous state to compare"], [])
    label = {
        "last_signal_bar": "NEW SIGNAL",
        "sent": "signal sent",
        "vetoed": "gate veto",
        "gate_passed": "gate verdict",
        "blocking": "blocking legs",
        "rule_ready": "RULE READY (no blocking legs)",
        "bias": "structural bias",
        "long_danger_band": "long danger band",
        "short_danger_band": "short danger band",
        "stale": "feed freshness",
        "session": "session",
        "adx_regime": "ADX regime",
        "oi_quadrant": "OI quadrant",
        "funding_verdict": "funding vs baseline",
        "timing_rules": "session timing rules",
    }
    triggers, context = [], []
    for k, name in label.items():
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        (triggers if k in TRIGGER_KEYS else context).append(f"{name}: {a} -> {b}")
    return triggers, context


def journal_path(now: datetime) -> Path:
    return OUT_DIR / f"{now.strftime('%Y-%m-%d')}.md"


def append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        day = path.stem
        path.write_text(
            f"# {day} — analysis journal\n\n"
            "Appended by `tools/analysis_tick.py` every 30 minutes. A `·` line is a\n"
            "quiet tick: state was checked and nothing decision-relevant had changed.\n"
            "A `##` section is a real read, triggered by the change listed under it.\n\n",
            encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def one_line(blob: str, status: dict, symbol: str, fp: dict) -> str:
    """The quiet-tick line. Still informative enough to skim a whole day."""
    px = ((status.get("live_prices") or {}).get(symbol)
          or ((status.get("symbols") or {}).get(symbol) or {})
          .get("live", {}).get("last_price"))
    risk = ((status.get("symbols") or {}).get(symbol) or {}).get("risk") or {}
    lo, sh = risk.get("long") or {}, risk.get("short") or {}
    rule = status.get("rule") or {}
    blocked = ", ".join(fp["blocking"]) if fp["blocking"] else "none"
    return (f"· {datetime.now(MYT):%H:%M} · {px or '?'} · "
            f"danger L{lo.get('danger','?')}/S{sh.get('danger','?')} · "
            f"bias {fp.get('bias','?')} · blocking: {blocked}"
            + ("  **[FEED STALE]**" if fp.get("stale") else "")
            + ("  **[SIGNAL SENT]**" if rule.get("sent") else "") + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.tools.analysis_tick")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--symbol", default="XAUUSDT")
    ap.add_argument("--force", action="store_true",
                    help="analyse even if nothing changed")
    ap.add_argument("--record-only", action="store_true",
                    help="append the heartbeat line and stop. Never prints the "
                         "blob, never asks for analysis. This is what Windows "
                         "Task Scheduler runs, so the record survives Claude "
                         "being closed, busy, or unpermitted.")
    ap.add_argument("--state-key", default=None,
                    help="which fingerprint file to compare against. Defaults to "
                         "'recorder' with --record-only and 'analyst' without, so "
                         "the two never consume each other's changes.")
    a = ap.parse_args(argv)

    now = datetime.now(MYT)
    path = journal_path(now)
    key = a.state_key or ("recorder" if a.record_only else "analyst")
    STATE = state_path(key)
    blob, status, err = fetch(a.port, a.symbol)

    # The watcher being down is NEWS, not silence. A journal that looks quiet
    # because nothing is running is the worst possible failure of a journal.
    if err is not None:
        append(path, f"· {now:%H:%M} · **WATCHER UNREACHABLE** on port "
                     f"{a.port} — {err}\n")
        print("NO_CHANGE")
        print(f"The watcher on 127.0.0.1:{a.port} could not be reached ({err}). "
              "Recorded in the journal. Nothing to analyse — start watch.bat.")
        return 0

    new = fingerprint(blob, status, a.symbol)
    try:
        old = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        old = {}
    reasons, context = diff(old.get(a.symbol, {}), new)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    allstate = old if isinstance(old, dict) else {}
    allstate[a.symbol] = new
    STATE.write_text(json.dumps(allstate, indent=1, ensure_ascii=False),
                     encoding="utf-8")

    # The recorder's whole job: one line, every time, no exceptions. A change is
    # marked with `**` so the analyst's missing write-up is visible as a gap
    # rather than as nothing at all.
    if a.record_only:
        line = one_line(blob, status, a.symbol, new).rstrip("\n")
        if reasons:
            line += "  **[" + "; ".join(reasons[:2]) + "]**"
        elif context:
            # Context is worth seeing on the line, but in a quieter form — it is
            # explicitly NOT a reason anyone should read further.
            line += "  _(" + "; ".join(c.split(":")[0] for c in context[:3]) + ")_"
        append(path, line + "\n")
        print("RECORDED" + (" — TRIGGER" if reasons else ""))
        return 0

    if not reasons and not a.force:
        append(path, one_line(blob, status, a.symbol, new))
        print("NO_CHANGE")
        moved = f" ({len(context)} context field(s) moved: "
        moved += ", ".join(c.split(":")[0] for c in context[:3]) + ")" if context else ""
        print(f"State checked at {now:%H:%M} MYT — no decision-relevant trigger"
              f"{moved}. One line appended to {path.name}. Stop here.")
        return 0

    # The heading is written to the journal HERE, before the caller has done
    # anything. The state has already advanced, so if the analysis step then
    # fails — a stalled permission prompt, a crash, a model that never answers
    # — the trigger would otherwise be consumed and the change lost with no
    # trace anywhere. Writing the heading first means the journal shows a
    # section with nothing under it: visibly incomplete, which is recoverable,
    # instead of silently absent, which is not.
    heading = f"## {now:%H:%M} MYT — " + "; ".join(reasons[:3])
    body = "\n".join(f"- {r}" for r in reasons)
    append(path, "\n" + heading + "\n\n" + body + "\n\n")

    print("CHANGED: " + " | ".join(reasons))
    print()
    print(f"JOURNAL_FILE: {path}")
    print(f"HEADING: {heading}")
    print("NOTE: that heading is ALREADY in the journal. Append your analysis")
    print("under it — do not repeat the heading.")
    print()
    print("=" * 80)
    print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
