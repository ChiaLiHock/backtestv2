"""Headless analysis: run the tick, and if it triggered, have Claude write it up.

    python -m backtest.tools.analysis_ai
    python -m backtest.tools.analysis_ai --force --timeout 900

This is **route B**: the Claude Code CLI in print mode, driven by Windows Task
Scheduler. It needs no Claude app open, no idle REPL, and no interactive
permission approval — the three things that made the in-app scheduler fire 0 of
12 times overnight while the Windows recorder fired 12 of 12.

It also costs no API credits: the bundled CLI authenticates against the same
subscription the desktop app uses.

## The one thing this cannot do for you

`claude auth login` is an OAuth browser flow. It has to be run once, by hand, by
the person who owns the account. Until then this script stops with a clear
message rather than failing obscurely at 3am — a scheduled job that dies quietly
is worse than one that never ran.

## Why the prompt goes on stdin

The snapshot is ~22 KB. Windows caps a command line at 8,191 characters, so
passing it as an argument truncates it silently — the model would receive a blob
that ends mid-sentence and analyse it anyway. stdin has no such limit.

## What it writes

Nothing, on a quiet tick — the recorder already logs those lines, and two
processes writing heartbeats would double every entry. On a trigger it writes the
heading first (so a failed analysis leaves a visible gap, not silence), then
appends whatever the model returns underneath.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import analysis_tick as T

# The CLI the desktop app ships. Discovered rather than pinned: the version
# directory changes on every update, and a hard-coded path would break silently
# at the next one — in a scheduled job nobody is watching.
#
# `CLAUDE_CLI_PATH` overrides discovery entirely. That escape hatch exists
# because discovery failed once from a double-clicked .bat while succeeding from
# a shell seconds later on the same machine — most likely an on-access virus
# scanner holding the 315 MB binary open. A person who knows where the file is
# should be able to say so and move on.
CLI_ENV = "CLAUDE_CLI_PATH"


def _roots() -> list[Path]:
    """Every place the bundled CLI might be, REAL paths first.

    `%APPDATA%\\Claude` is a **junction** into the Store package container:

        C:\\Users\\<u>\\AppData\\Roaming\\Claude
          -> C:\\Users\\<u>\\AppData\\Local\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude

    A process running INSIDE the container resolves it and sees the CLI. A
    process outside — anything you double-click from Explorer, and anything
    Task Scheduler starts — may not, and reports `is_dir=False` on a directory
    that visibly exists. That is exactly what happened: discovery worked from a
    shell and failed from a double-clicked .bat on the same machine, minutes
    apart, which looked like a virus scanner and was not.

    So the package path is searched FIRST and the junction only as a fallback.
    The package name carries a publisher hash, so it is globbed rather than
    written out.
    """
    home = Path.home()
    out: list[Path] = []
    pkgs = home / "AppData" / "Local" / "Packages"
    try:
        for pkg in sorted(pkgs.glob("Claude_*")):
            out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude-code")
    except OSError:
        pass
    out.append(home / "AppData" / "Roaming" / "Claude" / "claude-code")
    out.append(home / "AppData" / "Local" / "Programs" / "claude-code")
    return out


CLI_ROOTS = tuple(_roots())


def _real(p: Path) -> Path:
    """Follow junctions/symlinks so the returned path works outside the container."""
    try:
        return p.resolve(strict=False)
    except OSError:
        return p


def _usable(p: Path) -> bool:
    """Does this look like a runnable exe?

    `is_file()` swallows OSError and returns False, so a transiently locked file
    reads as absent. `exists()` plus a size check survives that, and the worst
    case is a clearer error one step later instead of a wrong "not found" here.
    """
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        # Even stat() can fail while a scanner holds the handle. If the name is
        # there at all, let the caller try to run it.
        return p.name.lower() == "claude.exe"


def find_cli(diagnose: bool = False) -> Path | None:
    """Newest bundled `claude.exe`, or None. ``diagnose`` prints what it saw."""
    import os

    override = os.environ.get(CLI_ENV, "").strip().strip('"')
    if override:
        p = Path(override)
        if _usable(p):
            return _real(p)
        if diagnose:
            print(f"  {CLI_ENV} is set to {override!r} but nothing usable is there")

    found: list[Path] = []
    for root in CLI_ROOTS:
        if diagnose:
            print(f"  root {root}  is_dir={root.is_dir()}")
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            exe = child / "claude.exe" if child.is_dir() else child
            if exe.name.lower() != "claude.exe":
                continue
            ok = _usable(exe)
            if diagnose:
                print(f"    {exe}  usable={ok}")
            if ok:
                found.append(exe)
    if not found:
        return None

    # Sort by the version directory, newest last. Plain string sort is wrong
    # across a 2.9 -> 2.10 bump, so split on dots numerically.
    def ver(p: Path):
        try:
            return tuple(int(x) for x in p.parent.name.split("."))
        except ValueError:
            return (0,)
    return _real(sorted(found, key=ver)[-1])


def logged_in(cli: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run([str(cli), "auth", "status"],
                           capture_output=True, text=True, timeout=60)
        blob = (r.stdout or "") + (r.stderr or "")
        try:
            return bool(json.loads(blob).get("loggedIn")), blob.strip()
        except ValueError:
            return ("loggedIn\": true" in blob), blob.strip()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


INSTRUCTIONS = """\
You are appending one entry to a live trading journal. Everything after the
`====` line below is a data snapshot that carries its OWN mandate: the analyst
role, the engine's scoring rules, the session timing rules, the required output
format, and an ABSENT section naming what data this project does not have.

Follow that embedded mandate exactly:

- Output in Traditional Chinese (繁體中文).
- The four core modules, and the three conditional ones ONLY when their
  condition holds.
- Where a module's data is insufficient, write 數據不足 and say what is missing.
  Never invent a price or a direction to fill the format.
- Obey the engine's scoring bands literally — there is no "1.0x volume" line in
  this engine and ADX has no "trend confirmed" threshold.
- You have web search. Use it when the mandate's rules 1-3 apply. This project
  has NO economic calendar, so an event's absence from the snapshot is not
  evidence that none is scheduled. If you verify something and the conclusion
  does not change, say so explicitly.

Output ONLY the journal entry itself. No preamble, no "here is the analysis",
no closing remarks. It is appended verbatim under a heading that already exists.

The system does NOT place trades — it emits signals the owner acts on manually.
Never phrase output as if an order was sent.

"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.tools.analysis_ai")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--symbol", default="XAUUSDT")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds to allow the model (default 900)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything except call the model")
    a = ap.parse_args(argv)

    cli = find_cli()
    if cli is None:
        print("NO_CLI: no bundled claude.exe found. What discovery actually saw:")
        find_cli(diagnose=True)
        print(f"\n  Fix: set {CLI_ENV} to the full path of claude.exe, e.g.\n"
              f'    setx {CLI_ENV} "%APPDATA%\\Claude\\claude-code\\<version>\\claude.exe"\n'
              "  then open a NEW window and try again.")
        return 2
    ok, detail = logged_in(cli)
    if not ok and not a.dry_run:
        print("NOT_LOGGED_IN: the CLI has its own credential store, separate "
              "from the desktop app's.\nRun this ONCE, by hand:\n"
              f'  "{cli}" auth login\n\nstatus said: {detail}')
        return 3

    now = datetime.now(T.MYT)
    path = T.journal_path(now)
    blob, status, err = T.fetch(a.port, a.symbol)
    if err is not None:
        # The recorder already logs an unreachable watcher; duplicating the line
        # here would double it. Report and stop.
        print(f"WATCHER_DOWN: {err}")
        return 0

    new = T.fingerprint(blob, status, a.symbol)
    state = T.state_path("analyst")
    try:
        old = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    except Exception:
        old = {}
    triggers, context = T.diff(old.get(a.symbol, {}), new)
    state.parent.mkdir(parents=True, exist_ok=True)
    old[a.symbol] = new
    state.write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")

    if not triggers and not a.force:
        moved = ", ".join(c.split(":")[0] for c in context[:3]) or "none"
        print(f"NO_TRIGGER at {now:%H:%M} MYT (context moved: {moved})")
        return 0

    # `--force` runs with no real trigger, which left the heading as a bare
    # "## 07:19 MYT —" with an empty bullet list under it. A heading that
    # names nothing is worse than no heading: it reads like a lost entry.
    why = triggers or ["手動強制執行（--force），非自動觸發"]
    heading = f"## {now:%H:%M} MYT — " + "; ".join(why[:3])
    body = "\n".join(f"- {t}" for t in why)
    T.append(path, "\n" + heading + "\n\n" + body + "\n\n")
    print("TRIGGER: " + (" | ".join(triggers) if triggers else "forced (--force)"))

    if a.dry_run:
        print(f"DRY_RUN: would call {cli} with {len(blob):,} chars on stdin")
        return 0

    prompt = INSTRUCTIONS + "=" * 80 + "\n" + blob
    try:
        r = subprocess.run(
            [str(cli), "-p", "--allowed-tools", "WebSearch"],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=a.timeout, cwd=str(T.ROOT.parent),
        )
    except subprocess.TimeoutExpired:
        T.append(path, f"_(analysis timed out after {a.timeout}s — heading above "
                       "records what changed)_\n\n---\n")
        print(f"TIMEOUT after {a.timeout}s")
        return 1

    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        detail = (r.stderr or "").strip()[:400] or f"exit {r.returncode}"
        T.append(path, f"_(analysis failed: {detail})_\n\n---\n")
        print(f"FAILED: {detail}")
        return 1

    T.append(path, out + "\n\n---\n")
    print(f"WROTE {len(out):,} chars to {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
