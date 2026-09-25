"""Live watch: two loops, one page, no reloads.

A full cycle â€” sync every timeframe, re-run the strategy over 166 days, rebuild
an 8 MB report â€” takes ~7 s per symbol. Running that every 5 seconds is not
possible and would not help: the backtest cannot change until a bar closes.

So the work is split by how fast it can actually change:

* **fast loop** (default 5 s) â€” one 1-minute kline request per symbol. The
  higher-timeframe *forming* bars are folded from those 1m bars, indicators are
  recomputed over a short tail, and the result is written to ``live.json``. The
  page patches it into the arrays it already has, so the chart moves with the
  market while your zoom, your selected trade and any measurement survive.
* **slow loop** (default 300 s) â€” the real thing: sync, backtest, rebuild the
  payload, write ``status.json``. The page notices the new run and fetches the
  payload in place. Still no reload.

Two rules the split must not break:

**The forming bar never produces a signal.** ``live.json`` carries it for drawing
only; ``ut_buy``/``ut_sell`` and every entry-condition read come from CLOSED bars.
This is the app's own CONFIRMED/LIVE split (`docs/OPEN_QUESTIONS.md` Q28), and a
signal read off an unfinished bar can un-fire before that bar closes.

**The tail window is exact, not approximate.** Indicators are recomputed over the
last ``TAIL_BARS`` bars rather than all of history. That is only sound because
every column the chart draws converges: over 1,500 bars the tail result is
bit-identical to the full-history result on the last 100 bars, which
``tests/test_live_tail.py`` pins. It is *not* true of the swing-structure
columns, so those are deliberately not part of this feed.
"""

from __future__ import annotations

import functools
import json
from http.server import HTTPServer
import http.server
import logging
import os
import socketserver
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .analysis import brief as brief_mod
from .analysis.report import (atomic_write, build_book, build_live_payload,
                              build_payload, oi_on_bars, render,
                              write_payload_json, write_cockpit_page)
from .data.bybit_client import BybitClient, BybitError
from .data.db import (DEFAULT_DB_PATH, INTERVAL_MS, CandleRepository,
                      Database, MetaRepository)
from .data.sync import MarketDataSync
from .indicators.base import IndicatorConfig
from .engine.backtester import Backtester, persist
from .engine.config import StrategyConfig
from .engine import risk_engine as R
from .engine.live_engine import LiveEngine
from .engine.session_map import build_map as build_session_map
from .engine.signal2 import Signal2
from .engine.signal3 import Signal3 as Signal3V2
from .engine.signal4 import Signal4
from .engine.signal5 import Signal5
from .engine.signal7 import Signal7
from .engine.signals import REGISTRY, SignalContext, evaluate_signal
from .engine.symbols import (SYMBOL_LABEL, common_period, comparison_context,
                             retarget, stored_yaml)
from .indicators.registry import compute_indicators

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

# Long enough that every column below is bit-identical to a full-history run
# (see the module docstring), short enough that five timeframes recompute in
# ~0.3 s so a 5-second tick is comfortable.
TAIL_BARS = 1500

# Bars sent per timeframe per tick. Only has to cover what closed since the last
# tick plus the forming bar; anything older is already on the page.
SEND_BARS = 6

LIVE_COLUMNS = ("ema_7", "ema_14", "ema_28", "ut_level", "vwap",
                "rsi_14", "atr_14", "macd", "macd_signal", "macd_hist")

# Symbols served by the MT5 terminal rather than by Bybit. `data/mt5_rates.py`
# stores them under this prefix so the two feeds cannot be confused for each
# other in `candles` â€” a `GOLD` row from XM is not a `XAUUSDT` row from Bybit and
# they are 3.38 apart.
MT5_PREFIX = "MT5:"


def is_mt5(symbol: str) -> bool:
    return symbol.startswith(MT5_PREFIX)


def broker_symbol(symbol: str) -> str:
    return symbol[len(MT5_PREFIX):] if is_mt5(symbol) else symbol


def payload_name(symbol: str) -> str:
    """Filename for a symbol's payload.

    `MT5:GOLD` cannot be a Windows filename â€” a colon starts an alternate data
    stream, so `payload_MT5:GOLD.json` silently writes to a stream named
    `GOLD.json` on `payload_MT5` and the browser then 404s on a file that
    `os.path.exists` says is there. The page reads this name from the book's
    `files` map rather than rebuilding it, so the two can never drift.
    """
    safe = symbol.replace(":", "_").replace("/", "_").replace("\\", "_")
    return f"payload_{safe}.json"
LIVE_DP = {"rsi_14": 1, "atr_14": 3, "macd": 3, "macd_signal": 3, "macd_hist": 3}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the report directory, plus a small local API.

    Two things the page cannot do on its own are handled here:

    * **`/api/awake`** â€” `navigator.wakeLock` is released the moment the tab is
      hidden, so a minimised browser lets the display and sleep timers run again.
      Only a process holding `SetThreadExecutionState` stops that, and this is
      the process that is always running.
    * **`/api/analysis`** â€” the Copy-analysis blob. Built server-side because it
      needs the DB (open positions, zones, the signal log), which the page has no
      access to.

    Bound to 127.0.0.1 only, like the rest of the server. These endpoints change
    this machine's power state and read this machine's trading state; neither
    should be reachable from the network.
    """

    watcher: "Watcher | None" = None

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        # live.json is rewritten every few seconds; a cached copy would freeze
        # the chart while looking perfectly healthy.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    # -- the API ----------------------------------------------------------

    def _send(self, code: int, body: str, ctype: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _api(self) -> bool:
        """Handle an /api/* path. Returns True if it was one."""
        from urllib.parse import parse_qs, urlparse

        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return False
        q = parse_qs(u.query)
        route = u.path[len("/api/"):]
        try:
            if route == "awake":
                from . import keepawake
                if "on" in q:
                    keepawake.toggle(q["on"][0] not in ("0", "false", "off"))
                self._send(200, json.dumps(keepawake.status()), "application/json")
            elif route == "analysis":
                sym = (q.get("symbol") or [None])[0]
                fmt = (q.get("format") or ["text"])[0]
                if self.watcher is None:
                    self._send(503, "watcher not ready", "text/plain")
                else:
                    body = self.watcher.analysis_blob(sym, as_json=(fmt == "json"))
                    self._send(200, body,
                               "application/json" if fmt == "json" else "text/plain")
            elif route == "signal4_refit":
                # Takes ~10 s (it walks 1m bars to label the window), so the
                # button is responsible for showing that it is working.
                if self.watcher is None:
                    self._send(503, "watcher not ready", "text/plain")
                else:
                    def _f(name, default):
                        try:
                            return float((q.get(name) or [default])[0])
                        except (TypeError, ValueError):
                            return default
                    result = self.watcher.refit_signal4(
                        days=int(_f("days", 5)),
                        min_win=_f("min_win", 0.85),
                        min_per_day=_f("min_per_day", 3.0))
                    self._send(200, json.dumps(result), "application/json")
            elif route == "oi_refresh":
                # Manual retry for the auto-sync going silent -- see
                # Watcher.refresh_open_interest for why this exists at all.
                # A real network round trip per symbol, so this can take a
                # couple of seconds; that is the button's job to show, not
                # this handler's to hide.
                if self.watcher is None:
                    self._send(503, "watcher not ready", "text/plain")
                else:
                    raw = (q.get("symbol") or [""])[0]
                    syms = [s.strip().upper() for s in raw.split(",") if s.strip()] or None
                    result = self.watcher.refresh_open_interest(syms)
                    self._send(200, json.dumps(result), "application/json")
            elif route == "cockpit":
                # The live block behind the second screen. Read-only and cheap
                # (zones come from cache, indicators off a 400-bar tail), so a
                # 5-second poll is fine.
                if self.watcher is None:
                    self._send(503, json.dumps({"error": "watcher not ready"}),
                               "application/json")
                else:
                    sym = (q.get("symbol") or [None])[0]
                    self._send(200, json.dumps(self.watcher.cockpit_block(sym)),
                               "application/json")
            elif route == "cockpit_brief":
                # The blob the AI button copies. Text, because it is going into
                # a chat window and JSON would waste half the message on quotes.
                if self.watcher is None:
                    self._send(503, "watcher not ready", "text/plain")
                else:
                    sym = (q.get("symbol") or [None])[0]
                    self._send(200, self.watcher.cockpit_brief(sym), "text/plain")
            elif route == "ai_read":
                # GET returns whatever read is on file; POST stores one. This is
                # the write-back half of the AI button: the blob goes out to a
                # chat, the answer comes back in here, and the page shows it with
                # the price it was written at so a stale read is obvious.
                if self.watcher is None:
                    self._send(503, json.dumps({"error": "watcher not ready"}),
                               "application/json")
                elif self.command == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length).decode("utf-8", "replace")
                    self._send(200, json.dumps(self.watcher.put_ai_read(body)),
                               "application/json")
                else:
                    self._send(200, json.dumps(self.watcher.get_ai_read()),
                               "application/json")
            else:
                self._send(404, json.dumps({"error": f"no route {route}"}),
                           "application/json")
        except Exception as exc:            # an API fault must not kill the server
            log.debug("api %s failed: %s", route, exc)
            self._send(500, json.dumps({"error": str(exc).splitlines()[0]}),
                       "application/json")
        return True

    def do_GET(self) -> None:               # noqa: N802 â€” http.server's spelling
        if not self._api():
            super().do_GET()

    def do_POST(self) -> None:              # noqa: N802
        if not self._api():
            self._send(405, "POST is only accepted on /api/*", "text/plain")


def _keepawake_status() -> dict:
    """Never let a power-management read break a status write."""
    try:
        from . import keepawake
        return keepawake.status()
    except Exception as exc:
        return {"supported": False, "on": False, "reason": str(exc).splitlines()[0]}


def _round(v: object, dp: int = 2) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, dp)


def _fold_bucket(part: pd.DataFrame, bucket: int) -> dict:
    return {
        "open_time": int(bucket),
        "open": float(part["open"].iloc[0]),
        "high": float(part["high"].max()),
        "low": float(part["low"].min()),
        "close": float(part["close"].iloc[-1]),
        "volume": float(part["volume"].sum()),
        "turnover": float(part["turnover"].sum()),
    }


def fold_from_minute(
    minute: pd.DataFrame, tf: str, last_stored_ms: int, at_ms: int
) -> tuple[list[dict], int | None]:
    """1-minute bars -> every ``tf`` bar the stored series does not have yet.

    Returns ``(bars, forming_open_time)``. ``bars`` starts at
    ``last_stored_ms + step`` and runs to the bucket in progress; the last one is
    the forming bar unless the boundary happened to land exactly.

    Bybit's higher-timeframe bars are aggregations of the same trades, so folding
    1m into them is exact for OHLCV. One 1m request per symbol therefore serves
    every timeframe, instead of one request per (symbol, timeframe).

    **Contiguity is the whole point.** The slow loop only syncs every few
    minutes, so by the time a tick runs, one or more real bars may have closed
    and not been stored. Appending just the in-progress bar onto that stale tail
    would leave a HOLE, and the indicators â€” EMA, ATR, and the path-dependent UT
    trailing stop â€” would then be computed as if the missing bar never traded.
    So either the whole contiguous run is produced, or nothing is: if the 1m
    window does not reach back far enough to cover the first missing bucket, this
    returns ``([], None)`` and the tick simply shows stored data until the slow
    sync catches up.
    """
    step = INTERVAL_MS[tf]
    if minute.empty:
        return [], None
    ot = minute["open_time"].to_numpy()
    first_bucket = (int(last_stored_ms) // step) * step + step
    # The in-progress bucket comes from whichever is further along: the local
    # clock or the newest minute the exchange has published. Taking only the
    # clock would drop real trades whenever this machine runs slow.
    current = max((int(at_ms) // step) * step, (int(ot.max()) // step) * step)
    if current < first_bucket:
        return [], None            # nothing new has opened yet

    if ot.min() > first_bucket:
        # The 1m window starts inside the first missing bar, so folding it would
        # invent a bar from partial data. Refuse rather than approximate.
        return [], None

    bars: list[dict] = []
    bucket = first_bucket
    while bucket <= current:
        part = minute[(ot >= bucket) & (ot < bucket + step)]
        if part.empty:
            # A hole in the 1m feed itself. Stop here; what came before is still
            # contiguous with the stored series and safe to use.
            break
        bars.append(_fold_bucket(part, bucket))
        bucket += step

    if not bars:
        return [], None
    forming = bars[-1]["open_time"] if bars[-1]["open_time"] == current else None
    return bars, forming


def _source_fingerprint() -> dict[str, float]:
    """mtime of every .py in this package, as loaded."""
    root = Path(__file__).resolve().parent
    out: dict[str, float] = {}
    for f in root.rglob("*.py"):
        parts = set(f.parts)
        if ".venv" in parts or "__pycache__" in parts:
            continue
        try:
            out[str(f.relative_to(root))] = f.stat().st_mtime
        except OSError:
            continue
    return out


def _stale_sources(baseline: dict[str, float]) -> list[str]:
    """Files edited since this process imported them.

    Python caches modules at import, so a watcher left running across an edit
    keeps building payloads with the old code â€” while the HTML template, which is
    re-read from disk every render, picks the change up immediately. The result
    is a page with half the new features and no error anywhere, which is the most
    confusing possible failure. So it is detected and said out loud.
    """
    now = _source_fingerprint()
    changed = [f for f, m in now.items() if f in baseline and m > baseline[f] + 0.5]
    changed += [f for f in now if f not in baseline]
    return sorted(changed)


def _pid_alive(pid: int) -> bool:
    """Windows has no os.kill(pid, 0); ask the OS whether the pid is listed."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return str(pid) in out
    except Exception:
        return True      # cannot tell -> assume it is, and refuse rather than clobber


class DirectoryInUse(SystemExit):
    pass


class _DirLock:
    """One watcher per report directory.

    Two watchers on different ports still share ``reports/``, and they will
    overwrite each other's ``index.html``, ``live.json`` and payloads. That
    happened during development and produced a page whose data was from one
    process and whose HTML was from another â€” which looks like a code bug and is
    not one. A stale lock from a killed process is reclaimed automatically.
    """

    def __init__(self, directory: Path, port: int) -> None:
        self.path = directory / ".watcher.lock"
        self.port = port

    def acquire(self) -> None:
        if self.path.exists():
            try:
                held = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                held = {}
            pid = int(held.get("pid", 0) or 0)
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise DirectoryInUse(
                    f"another watcher (pid {pid}, port {held.get('port')}) is already "
                    f"writing {self.path.parent}.\n"
                    "Close that window first â€” two watchers overwrite each other's "
                    "report and the page ends up showing a mix of both."
                )
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "port": self.port,
                        "started": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8")

    def release(self) -> None:
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
            if int(held.get("pid", 0)) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass


class Watcher:
    def __init__(
        self,
        config_path: Path,
        db_path: str | Path | None = None,
        interval_s: int = 300,
        port: int = 8787,
        report_dir: Path | None = None,
        symbols: list[str] | None = None,
        live_interval_s: int = 5,
        normalise: bool = True,
        mt5_sync: bool = True,
        live_mode: bool = True,
        gate: R.GateSettings | None = None,
        emit: bool = True,
        max_bars: int = 1500,
    ) -> None:
        self.config_path = Path(config_path)
        #  resolves this before dispatching, but a Watcher built
        # directly (a test, a script) would otherwise hand None to Database and
        # fail inside the first cycle rather than at construction.
        self.db_path = db_path if db_path is not None else DEFAULT_DB_PATH
        self.interval_s = max(30, interval_s)
        self.live_interval_s = max(2, live_interval_s)
        self.port = port
        self.report_dir = report_dir or (Path(__file__).resolve().parent / "reports")
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.index = self.report_dir / "index.html"
        self.status_path = self.report_dir / "status.json"
        self.live_path = self.report_dir / "live.json"
        self.normalise = normalise
        self.mt5_sync = mt5_sync
        # LIVE MODE is the default now. The slow loop no longer re-runs a
        # strategy over 166 days of history every cycle â€” a backtest cannot change
        # until a bar closes, and re-deriving it every five minutes to serve a
        # page that only ever shows the newest state was the cost this replaces.
        # `--backtest` puts the old behaviour back for when you genuinely want to
        # look at a run's trades.
        self.live_mode = live_mode
        self.max_bars = max_bars
        # The signal log lives WITH the reports it explains. Default is
        # `backtest/reports/signals.jsonl`, unchanged â€” but a second watcher run
        # with `--report-dir` then gets its own audit trail instead of appending
        # test evaluations to the real one.
        self.engine = LiveEngine(
            gate_settings=gate, emit=emit,
            log_path=self.report_dir / "signals.jsonl") if live_mode else None
        if self.engine is not None:
            # A restart must not re-alert on signals already in the log.
            self.engine.seed_from_log()
        # SIGNAL 2 -- a separately mined pattern. Its own object, its own log,
        # its own event list; it shares no state with `self.engine` so it cannot
        # influence what the measured rule decides. See engine/signal2.py for
        # what it is and, more importantly, for what it is not.
        self.signal2 = Signal2(
            log_path=self.report_dir / "signals2.jsonl") if live_mode else None
        if self.signal2 is not None:
            self.signal2.seed_from_log()
        self._rule2: dict = {}
        # SIGNAL 3 -- v2: created later (after signal7) because it uses
        # signal7 as its ExpD fallback; see below.
        self.signal3 = None
        self._rule3: dict = {}
        # SIGNAL 7 -- the ExpD regime gate as an EXPERIMENTAL live channel
        # (engine/signal7.py). Runs beside Signal 6 to accumulate the
        # forward OOS record; its model artifact is fitted by
        # `tools/fit_signal7.py` and the channel stays quiet without it.
        self.signal7 = Signal7(
            log_path=self.report_dir / "signals7.jsonl") if live_mode else None
        if self.signal7 is not None:
            self.signal7.seed_from_log()
        self._rule7: dict = {}
        self._s7_seen: set[tuple] = set()
        # SIGNAL 3 v2 — created HERE because it shares signal7 as its
        # ExpD fallback (see engine/signal3.py).
        if live_mode:
            self.signal3 = Signal3V2(
                log_path=self.report_dir / "signals3.jsonl",
                signal7=self.signal7)
            self.signal3.seed_from_log()
        # The newest 5m bar Signal 5 has already been evaluated on. The slow
        # cycle runs every ~346 s (300 s wait plus its own ~46 s of work) while
        # 5m bars close every 300 s, so a signal anchored on 5m and evaluated
        # only there would silently MISS about 38 of the 288 bars in a day.
        # Signals 1-4 are on a 15m anchor and cannot miss one; this is specific
        # to Signal 5, so the fast loop evaluates it instead -- gated on a new
        # bar so it costs one ~340 ms evaluation per bar, not one per 5 s tick.
        # SIGNAL 4 -- config-driven and re-mined weekly. It reads
        # `configs/signal4.json` and reloads it when that file changes, so a
        # refit applies without restarting this process.
        self.signal4 = Signal4(
            log_path=self.report_dir / "signals4.jsonl") if live_mode else None
        if self.signal4 is not None:
            self.signal4.seed_from_log()
        self._rule4: dict = {}
        # SIGNAL 5 -- FAST-C. Runs on the 5m grid and fires ~16x a day, so it
        # logs firing bars only; see engine/signal5.py.
        self.signal5 = Signal5(
            log_path=self.report_dir / "signals5.jsonl") if live_mode else None
        if self.signal5 is not None:
            self.signal5.seed_from_log()
        self._rule5: dict = {}
        self._s5_bar: int | None = None
        # SESSION MAP -- the intraday framework as an observation (see
        # engine/session_map.py): Asia range, Europe sweeps, US confirmation.
        # Not a signal: it never enters the rule, the gate or any log of
        # decisions; its events are announcements about STRUCTURE ("the Asia
        # high was swept and reclaimed"), so they get their own id prefix
        # `ses:` and their own notify channel rather than riding the signal
        # channels with none of their evidence.
        self._sessions: dict[str, dict] = {}
        self._session_events: list[dict] = []
        self._ses_seen: set[str] = set()
        self._ses_minute: dict[str, int] = {}
        self._ses_seeded: set[str] = set()
        # Push notifications. Falls back to console when unconfigured -- the
        # trading loop must never fail because a chat bot is not set up, and it
        # must never be quietly not-notifying either, so `build()` logs which
        # of the two it chose at startup.
        self._notifier = None
        self._notified: set[str] = set()
        self._notify_channels: set[str] = set()
        if live_mode:
            try:
                from .notify import telegram as _tg

                # Before anything can log a request. httpx writes the Bot API
                # URL -- token included -- at INFO, so this has to be installed
                # ahead of the first send, not after it.
                _tg.install_log_redaction()
                self._notifier = _tg.build()
                raw = os.environ.get("TELEGRAM_SIGNALS")
                if raw is None:
                    raw = (_tg.env_value("TELEGRAM_SIGNALS")
                           or "rule,signal3,session,signal7")
                self._notify_channels = {c.strip() for c in raw.split(",") if c.strip()}
                log.info("notifying on: %s",
                         ", ".join(sorted(self._notify_channels)) or "(nothing)")
            except Exception as exc:
                log.warning("notifications unavailable: %s", exc)
        self._mt5_note: str | None = None
        self._rates_note: str | None = None
        self._risk: dict[str, dict] = {}
        self._rule: dict = {}
        # Open-interest sync failures are best-effort (a bad cycle must not cost
        # the candles) and so are caught broadly -- which also means they can go
        # silent. This surfaces the last failure per symbol in status.json, so
        # "OI stopped updating" has an actual reason to read instead of a guess.
        # Cleared on the next success, so a stale entry here always means the
        # PREVIOUS attempt failed, not "failed once, long ago".
        self._oi_errors: dict[str, str] = {}
        self._zone_errors: dict[str, str] = {}
        self._lock_file = _DirLock(self.report_dir, port)
        self._sources = _source_fingerprint()
        self._stop = threading.Event()
        # Guards every write to `self.db_path`. `cycle()` runs on the main
        # thread; the manual OI-refresh endpoint (`refresh_open_interest`) runs
        # on whichever HTTP-server thread served the request. Without this,
        # the two could interleave writes to the same SQLite file, which is
        # exactly the kind of thing that could explain OI syncing going silent
        # for hours at a time while every isolated retry of the same call
        # succeeds instantly -- a bug this project has not yet root-caused.
        self._write_lock = threading.Lock()
        # Serialises the Signal 4 refit. It runs on an HTTP-server thread, takes
        # ~10 s, and rewrites the config file the live evaluator reads -- two
        # overlapping clicks would have two searches racing to write it.
        self._refit_lock = threading.Lock()

        base = StrategyConfig.from_yaml(self.config_path)
        self.symbols = symbols or [base.symbol]
        self.base_cfg = base
        # Resolved per symbol on the first slow cycle; the fast loop needs the
        # per-symbol config too (tick size, size, timeframes).
        self.configs: dict[str, StrategyConfig] = {}
        self.runs: dict[str, str] = {}
        self.comparison: dict | None = None
        self._status: dict = {}
        self._lock = threading.Lock()

    def put_ai_read(self, body: str) -> dict:
        """Store a written read. Accepts JSON `{text, ...}` or bare text.

        The spot and the time are stamped HERE, not taken from the body: a read
        is only usable next to the price it was written at, and a caller that
        supplied its own timestamp could make an hour-old opinion look current.
        """
        text = body
        sym = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                text = str(parsed.get("text") or "")
                # The read is about whichever symbol was on screen. Stamping it
                # with the rule's symbol instead put an ETHUSDT read next to
                # gold's price, which made the staleness check -- "how far has
                # price moved since this was written" -- compare two different
                # instruments and silently report nonsense.
                asked = str(parsed.get("symbol") or "").upper()
                if asked in {s.upper() for s in self.symbols}:
                    sym = asked
        except ValueError:
            pass
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty read — nothing stored"}
        if sym is None:
            sym = self.engine.cfg.symbol if self.engine else self.symbols[0]
        rec = {
            "text": text[:20000],
            "written_ms": int(time.time() * 1000),
            "spot": self._last_price(sym),
            "symbol": sym,
        }
        try:
            atomic_write(self._ai_read_path(), json.dumps(rec))
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **rec}

    # -- config ------------------------------------------------------------

    def _resolve_configs(self, db: Database) -> None:
        """Point the base config at each symbol, sizing so results compare."""
        start_ms = end_ms = None
        if len(self.symbols) > 1:
            try:
                start_ms, end_ms = common_period(db, self.symbols, self.base_cfg.timeframe)
            except SystemExit as exc:
                raise SystemExit(
                    f"{exc}\nSync every symbol before watching them together."
                ) from None
        for sym in self.symbols:
            cfg, note = retarget(self.base_cfg, db, sym, normalise=self.normalise,
                                 start_ms=start_ms, end_ms=end_ms)
            if start_ms is not None:
                cfg = cfg.model_copy(update={"period": cfg.period.model_copy(update={
                    "start": datetime.fromtimestamp(start_ms / 1000, timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": "now",
                })})
            self.configs[sym] = cfg
            if sym != self.base_cfg.symbol:
                log.info("%s", note.describe())
                for n in note.notes:
                    log.warning("%s", n)
        if len(self.symbols) > 1:
            self.comparison = comparison_context(db, self.base_cfg, self.symbols)

    # -- slow loop: sync, backtest, rebuild --------------------------------

    def _refresh_broker_trades(self, db: Database) -> None:
        """Pull the newest fills from the running MT5 terminal, if there is one.

        Best-effort by design: the terminal being closed is the normal state
        half the time, and it must not stop the backtest from rebuilding. It runs
        before the payload build so a trade taken since the last cycle is on the
        chart as soon as the page refreshes.
        """
        if not self.mt5_sync:
            return
        try:
            from .data import mt5_live
            terminal, n = mt5_live.sync(db)
            self._mt5_note = (f"{terminal.login}@{terminal.server} "
                              f"UTC{terminal.offset_hours:+d}, {n} positions")
            log.info("MT5: %s", self._mt5_note)
        except Exception as exc:
            # Includes Mt5Unavailable (terminal closed) and an absent package.
            self._mt5_note = "unavailable: " + str(exc).splitlines()[0]
            log.debug("MT5 sync skipped: %s", exc)

    def _sync_rates(self, db: Database) -> dict[str, dict[str, int]]:
        """Refresh candles for every symbol, from whichever feed owns it.

        Bybit and the MT5 terminal are different venues with different prices â€”
        XM's spot `GOLD` sits ~3.38 under Bybit's `XAUUSDT` perp â€” so they are
        stored under different symbols and synced by different code. A failure on
        either side is logged and the stored data is used; a broker being closed,
        or the terminal not running, is the normal state half the time and must
        never stop the page from updating.
        """
        synced: dict[str, dict[str, int]] = {}
        bybit_syms = [s for s in self.symbols if not is_mt5(s)]
        mt5_syms = [s for s in self.symbols if is_mt5(s)]

        if bybit_syms:
            try:
                with BybitClient() as client:
                    sync = MarketDataSync(client, db)
                    for sym in bybit_syms:
                        cfg = self.configs[sym]
                        symbol = sync.sync_instrument(sym)
                        got: dict[str, int] = {}
                        # 1m as well: the fast loop folds forming bars from it,
                        # and the engine's ambiguity replay needs it anyway.
                        for tf in {*cfg.required_timeframes(), "1m"}:
                            # 200 bars of overlap: sync is resumable and
                            # idempotent, so this only has to cover what closed
                            # since the last cycle plus a margin. Asking for more
                            # re-fetches the whole 4h history every 300 seconds.
                            now = int(time.time() * 1000)
                            rep = sync.sync_candles(
                                symbol, tf, now - 200 * INTERVAL_MS[tf], now)
                            got[tf] = rep.bars_written
                        # Funding and open interest: two more requests, and both
                        # unlock readings the page could otherwise only report as
                        # missing. OI in particular is what turns "price is up"
                        # into "price is up on NEW positions" versus "price is up
                        # on shorts covering" â€” the same candle, opposite meaning.
                        # Best-effort: a failure here must not cost the candles.
                        got.update(self._sync_context(sync, symbol))
                        synced[sym] = got
            except BybitError as exc:
                log.warning("Bybit sync failed, using stored data: %s", exc)
                synced["_bybit_error"] = {"error": 1}
                self._rates_note = f"bybit: {str(exc).splitlines()[0]}"

        if mt5_syms:
            try:
                from .data import mt5_rates
                for sym in mt5_syms:
                    # Only what a live read needs. The terminal serves the full
                    # panel too, but re-pulling 25 years of H4 every cycle is
                    # minutes of work to learn nothing. Seven days is far more
                    # than any gap a running watcher can open, and covers a long
                    # weekend plus a restart.
                    now = int(time.time() * 1000)
                    _terminal, reports = mt5_rates.sync(
                        db, broker_symbol(sym),
                        intervals=["1m", "5m", "15m", "30m", "1h", "4h"],
                        start_ms=now - 7 * 86_400_000, end_ms=now)
                    synced[sym] = {r.interval: r.bars for r in reports}
                self._rates_note = None
            except Exception as exc:
                # Includes Mt5Unavailable (terminal closed) and an absent package.
                self._rates_note = "MT5 rates: " + str(exc).splitlines()[0]
                log.debug("MT5 rate sync skipped: %s", exc)
        return synced

    # One grid, shared with the reader â€” see `risk_feed.OI_INTERVAL`. Choosing
    # per-symbol here meant the writer and the reader could disagree and the
    # reader would silently find nothing.
    from .engine.risk_feed import OI_INTERVAL, OI_RETENTION_DAYS

    def _sync_context(self, sync: "MarketDataSync", symbol: str) -> dict[str, int]:
        """Funding and open interest for one symbol. Never raises."""
        out: dict[str, int] = {}
        now = int(time.time() * 1000)
        try:
            out["funding"] = sync.sync_funding(symbol, now - 14 * 86_400_000, now)
        except Exception as exc:
            log.debug("funding sync skipped for %s: %s", symbol, exc)
        try:
            # Resume from the newest stored point, NOT a fixed lookback. The
            # fixed 200-bar (50 h) window this replaces made downtime permanent:
            # nothing re-requested the middle of a longer outage and nothing
            # reported the hole, so the gap simply stayed. Resuming makes the
            # request self-sizing -- one or two points on a normal cycle, a few
            # hundred after a night off -- and it costs one MAX(ts) lookup.
            #
            # Floored at the source's own retention because Bybit will not serve
            # older 15m OI at any price; asking further back just wastes pages.
            step = INTERVAL_MS[self.OI_INTERVAL]
            floor = now - self.OI_RETENTION_DAYS * 86_400_000
            last = sync.meta.newest_open_interest(symbol, self.OI_INTERVAL)
            start = max(floor, last - step) if last is not None else floor
            out["oi"] = sync.sync_open_interest(symbol, self.OI_INTERVAL, start, now)
            # A success after a prior failure is exactly when the failure stops
            # mattering -- keeping the stale entry around would blame a cycle
            # that already recovered.
            self._oi_errors.pop(symbol, None)
        except Exception as exc:
            # This is best-effort by design (a bad cycle must not cost the
            # candles), but best-effort must not mean invisible: this failure
            # mode has twice gone unnoticed for over an hour, across a symbol's
            # ENTIRE OI history, because `log.debug` normally prints nowhere.
            # `status.json` is read whether or not anyone is watching a console.
            self._oi_errors[symbol] = f"{type(exc).__name__}: {exc}"
            log.warning("open interest sync failed for %s: %s", symbol, exc)
        return out

    def _book(self, active_payload: dict, active: str) -> dict:
        """A one-payload book carrying the symbol list and the file map.

        `files` exists because `MT5:GOLD` cannot be interpolated into a filename
        — see `payload_name`. The page looks the name up here instead of building
        it, so a symbol whose name needs escaping cannot 404 silently.
        """
        return {
            "book": True,
            "symbols": list(self.symbols),
            "labels": {s: SYMBOL_LABEL.get(s, s) for s in self.symbols},
            "files": {s: payload_name(s) for s in self.symbols},
            "active": active,
            "runs": {active: active_payload},
            "external": True,
            "comparison": None,      # cross-symbol P/L is a backtest artefact
            "live_mode": True,
        }

    def _last_price(self, symbol: str) -> float | None:
        """Newest price the fast loop saw, if it has run yet."""
        with self._lock:
            blob = ((self._status.get("live_prices") or {}) if self._status else {})
        return blob.get(symbol)

    # -- the copy-analysis blob --------------------------------------------

    def analysis_blob(self, symbol: str | None = None, as_json: bool = False) -> str:
        """Everything a model would need about the current state, as text.

        Built here rather than in the page because it needs the database: open
        positions, the KEY LEVELS card and the signal log are not in the payload.
        """
        sym = symbol or (self.engine.cfg.symbol if self.engine else self.symbols[0])
        risk = self._risk.get(sym)
        rule = self._rule if (self.engine and sym == self.engine.cfg.symbol) else None
        events = self.engine.events() if self.engine else []
        positions = []
        try:
            with Database(self.db_path) as db:
                rows = db.conn.execute(
                    "SELECT symbol, broker_symbol, side, volume, open_time, "
                    "open_price, sl, tp FROM broker_trades WHERE close_time IS NULL "
                    "ORDER BY open_time").fetchall()
                positions = [dict(r) for r in rows]
        except Exception as exc:
            log.debug("could not read open positions: %s", exc)

        # The mandate's interpretation rules read ADX slope, the volume band,
        # the funding baseline and the OI quadrant — none of which is in the
        # risk snapshot. Gathered here so no rule has to answer "insufficient
        # data" about a value the database already holds.
        ctx = {}
        try:
            from .analysis import context as ctx_mod
            anchor = (self.engine.cfg.anchor if self.engine
                      and sym == self.engine.cfg.symbol else "1h")
            with Database(self.db_path) as db:
                ctx = ctx_mod.gather(db, sym, anchor, spot=self._last_price(sym))
        except Exception as exc:
            log.debug("context gather failed for %s: %s", sym, exc)

        if as_json:
            return brief_mod.build_json(symbol=sym, risk=risk, rule=rule, ctx=ctx,
                                        positions=positions, events=events,
                                        price=self._last_price(sym))
        return brief_mod.build(sym, risk, rule, positions=positions, events=events,
                               price=self._last_price(sym), ctx=ctx)

    # -- the second screen --------------------------------------------------

    def _top_up_zones(self, db: Database, symbol: str,
                      cfg: IndicatorConfig) -> None:
        """Compute any H1 zone snapshots the cache is missing. Never raises.

        `build_live_payload` reads zones cache-only on purpose: a cold cache on
        a 25-year series is minutes of work inside a loop that must stay
        responsive. But nothing was ever filling it during a watch, so the KEY
        LEVELS card and now the cockpit drifted -- measured at 14 hours stale on
        2026-08-28, which for a page whose whole job is "what is above and below
        me right now" makes it actively misleading rather than merely old.

        The catch-up is incremental: only H1 bars with no cached snapshot are
        computed. Measured at 0.38 s for XAUUSDT's 4,125 snapshots when 12 bars
        behind, which is affordable once per cycle and is why this can live here
        rather than in a separate command the owner has to remember to run.
        """
        started = time.time()
        try:
            from .indicators import zones as _zones
            _zones.snapshot_series(db, symbol, cfg)
        except Exception as exc:
            self._zone_errors[symbol] = f"{type(exc).__name__}: {exc}"
            log.debug("zone top-up failed for %s: %s", symbol, exc)
            return
        self._zone_errors.pop(symbol, None)
        took = time.time() - started
        if took > 5.0:
            log.info("zone top-up for %s took %.1fs — the cache was far behind",
                     symbol, took)

    def cockpit_block(self, symbol: str | None = None) -> dict:
        """The live block behind the second screen."""
        from .analysis import cockpit as _cockpit

        sym = symbol or (self.engine.cfg.symbol if self.engine else self.symbols[0])
        cfg = self.configs.get(sym)
        anchor = (self.engine.cfg.anchor
                  if self.engine and sym == self.engine.cfg.symbol else "1h")
        try:
            with Database(self.db_path) as db:
                out = _cockpit.build(
                    db, sym, cfg=(cfg.indicators if cfg else None),
                    spot=self._last_price(sym), anchor=anchor,
                    channels=self._channel_states(sym))
        except Exception as exc:
            log.debug("cockpit build failed for %s: %s", sym, exc)
            return {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}
        out["symbols"] = list(self.symbols)
        out["zone_error"] = self._zone_errors.get(sym)
        out["ai_read"] = self.get_ai_read()
        return out

    def _channel_states(self, symbol: str) -> list[dict]:
        """What each of the five channels says about the last closed bar.

        Only for the symbol they are fitted to -- signals 2-5 are XAUUSDT-only,
        and showing them under BTC as "not firing" would read as information
        when it is just the wrong instrument.
        """
        out: list[dict] = []
        for name, state in (("rule", self._rule), ("signal2", self._rule2),
                            ("signal3", self._rule3), ("signal4", self._rule4),
                            ("signal5", self._rule5)):
            if not isinstance(state, dict) or not state:
                continue
            if state.get("symbol") and state["symbol"] != symbol:
                continue
            out.append({
                "name": name,
                "measured": name == "rule",
                "side": state.get("side"),
                "fired": bool(state.get("fired")),
                "blocking": list(state.get("blocking") or []),
                "legs": state.get("legs") or {},
                "pattern": state.get("pattern") or "",
                "bar_myt": state.get("bar_myt") or "",
                "reason": state.get("reason") or "",
            })
        return out

    def cockpit_brief(self, symbol: str | None = None) -> str:
        """The text the AI button copies, for pasting into a chat.

        Deliberately the cockpit's own numbers rather than `analysis_blob`: that
        one answers the Copy-analysis mandate and is long. This one is the
        second screen's state -- where price sits between levels, how strong
        they are, and what the force behind it is doing -- which is what a reply
        would need to say "wait" or "go" and nothing more.
        """
        from .analysis import cockpit_brief as _cb

        return _cb.build(self.cockpit_block(symbol))

    # The written read lives on disk rather than in memory so it survives a
    # restart -- it is the one thing on this page a person authored.
    def _ai_read_path(self) -> Path:
        return self.report_dir / "ai_read.json"

    def get_ai_read(self) -> dict:
        try:
            raw = self._ai_read_path().read_text(encoding="utf-8")
            return json.loads(raw)
        except (OSError, ValueError):
            return {}

    def _cycle_backtest(self, db: Database, synced: dict) -> dict:
        """The original path: re-run the strategy and rebuild from the run."""
        per_symbol: dict[str, dict] = {}
        for sym in self.symbols:
            cfg = self.configs[sym]
            result = Backtester(db, cfg).run()
            persist(db, result, stored_yaml(
                self.config_path.read_text(encoding="utf-8"), cfg,
                sym != self.base_cfg.symbol))
            self.runs[sym] = result.run_id
            pnl = [t.net_pnl for t in result.trades]
            per_symbol[sym] = {
                "label": SYMBOL_LABEL.get(sym, sym),
                "run_id": result.run_id,
                "trades": len(result.trades),
                "net_pnl": round(float(sum(pnl)), 2) if pnl else 0.0,
                "qty": cfg.sizing.qty,
                "bars_synced": synced.get(sym, {}),
                "live": self._live_signals(db, cfg),
            }

        # Payload per symbol as a sibling file: the page swaps them in
        # without a reload, and a 3-symbol book embedded would be ~25 MB.
        active = self.symbols[0]
        for sym in self.symbols:
            write_payload_json(
                build_payload(db, self.runs[sym],
                              tuple(self.configs[sym].features.timeframes)),
                self.report_dir / payload_name(sym),
            )
        book = build_book(
            db, {s: self.runs[s] for s in self.symbols}, active=active,
            timeframes=tuple(self.configs[active].features.timeframes),
            comparison=self.comparison, external=True,
        )
        book["files"] = {s: payload_name(s) for s in self.symbols}
        render(book, self.index)
        # Re-copied each cycle so template edits appear on refresh, same as
        # the chart page.
        write_cockpit_page(self.report_dir / "cockpit.html")
        return per_symbol

    def _cycle_live(self, db: Database, synced: dict) -> dict:
        """No backtest. Evaluate the rule, read risk, rebuild the page.

        The expensive thing a cycle used to do — walk 166 days looking for trades
        — is gone. What is left is a rule evaluation on ONE bar, a risk read per
        symbol, and a payload rebuild so newly closed bars and any new zone
        snapshot reach the page. The 5-second loop does the rest.
        """
        from .engine.live_signal import read_log

        # The rule runs on its own symbol and anchor whether or not that symbol
        # is on the chart — it is the thing that decides, and it must not stop
        # deciding because you switched the view to BTC.
        rule = self.engine.rule(db, spot=self._last_price(self.engine.cfg.symbol))
        self._rule = rule

        # SIGNAL 2, evaluated after the real rule and unable to affect it: it
        # gets its own spot read and writes its own log. A failure here is
        # swallowed for the same reason the OI sync's is -- a mined extra must
        # never be able to stop the measured system from deciding.
        if self.signal2 is not None:
            try:
                r2 = self.signal2.evaluate(
                    db, spot=self._last_price(self.signal2.cfg.symbol))
                self._rule2 = r2
                self.signal2.write(r2)
            except Exception as exc:
                log.debug("signal2 cycle failed: %s", exc)
        if self.signal3 is not None:
            # Signal 3 v2 is block-based (Asia range → expd gate); the fast loop
            # supplies the session block and the slow cycle re-evaluates with
            # whatever the map last produced (say no louder than the fast loop).
            try:
                sym3 = self.signal3.cfg.symbol
                r3 = self.signal3.evaluate(
                    db, self._sessions.get(sym3),
                    spot=self._last_price(sym3))
                self._rule3 = r3
                self.signal3.write(r3)
            except Exception as exc:
                log.debug("signal3 cycle failed: %s", exc)
        if self.signal4 is not None:
            try:
                r4 = self.signal4.evaluate(
                    db, spot=self._last_price(self.signal4.cfg.symbol
                                              if self.signal4.cfg else "XAUUSDT"))
                self._rule4 = r4
                self.signal4.write(r4)
            except Exception as exc:
                log.debug("signal4 cycle failed: %s", exc)
        if self.signal5 is not None:
            try:
                r5 = self.signal5.evaluate(
                    db, spot=self._last_price(self.signal5.cfg.symbol))
                self._rule5 = r5
                self.signal5.write(r5)
            except Exception as exc:
                log.debug("signal5 cycle failed: %s", exc)

        signals = read_log(self.engine.signal.log_path)
        per_symbol: dict[str, dict] = {}
        active = self.symbols[0]
        # In-memory events die with the process; the jsonl log is the
        # durable record. Both, so a restart does not blank the panel's
        # signal3 rows for trades the phone was already notified about.
        # The log rows are evaluation outputs (no event id) — keyed on
        # (bar_open_ms, side), which is exactly the merge key the panel
        # uses, with the in-memory events (which DO carry ids) winning.
        signal3_live = None
        if self.signal3 is not None:
            from .engine.signal3 import _read_log as _s3_read_log
            by_key: dict[tuple, dict] = {}
            for ev in _s3_read_log(self.signal3.log_path):
                if not ev.get("fired"):
                    continue
                key = (int(ev.get("bar_open_ms") or 0),
                       str(ev.get("side") or ""))
                if key[0] <= 0:
                    continue
                by_key[key] = ev
            for ev in self.signal3.events():
                key = (int(ev.get("bar_open_ms") or 0),
                       str(ev.get("side") or ""))
                if key[0] > 0:
                    by_key[key] = ev        # in-memory wins on collision
            signal3_live = list(by_key.values())

        for sym in self.symbols:
            cfg = self.configs[sym]
            self._top_up_zones(db, sym, cfg.indicators)
            risk = self.engine.risk(db, sym, spot=self._last_price(sym))
            self._risk[sym] = risk
            payload = build_live_payload(
                db, sym, tuple(cfg.features.timeframes),
                indicator_config=cfg.indicators,
                max_bars=self.max_bars,
                strategy_name=f"{self.base_cfg.name} (live)",
                strategy_timeframe=self.engine.cfg.anchor,
                signals=signals if sym == self.engine.cfg.symbol else [],
                config_yaml=self.config_path.read_text(encoding="utf-8"),
                config_hash=rule.get("config_hash", ""),
                qty=cfg.sizing.qty,
                signal3_live_events=signal3_live if sym == self.engine.cfg.symbol
                                  else None,
            )
            write_payload_json(payload, self.report_dir / payload_name(sym))
            self.runs[sym] = payload["run_id"]
            per_symbol[sym] = {
                "label": SYMBOL_LABEL.get(sym, sym),
                "run_id": payload["run_id"],
                "trades": len(payload["trades"]),
                "net_pnl": 0.0,
                "qty": cfg.sizing.qty,
                "bars_synced": synced.get(sym, {}),
                "live": self._live_signals(db, cfg),
                "risk": risk,
                "rule": rule if sym == self.engine.cfg.symbol else None,
            }
            if sym == active:
                render(self._book(payload, active), self.index)
        write_cockpit_page(self.report_dir / "cockpit.html")
        return per_symbol

    def serve(self) -> None:
        # The API routes need to reach this watcher. Set on the class because
        # `SimpleHTTPRequestHandler` is instantiated per request.
        _QuietHandler.watcher = self
        handler = functools.partial(_QuietHandler, directory=str(self.report_dir))
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), handler)
        httpd.allow_reuse_address = True
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        # Written here too, not only per cycle: the first slow cycle can take
        # a minute, and a 404 on the second window looks like a broken install
        # rather than a page that has not been generated yet.
        try:
            write_cockpit_page(self.report_dir / "cockpit.html")
        except OSError as exc:
            log.warning("could not write cockpit.html: %s", exc)
        log.info("serving %s at http://127.0.0.1:%d/", self.report_dir, self.port)

    def cycle(self) -> dict:
        started = time.time()

        with self._write_lock, Database(self.db_path) as db:
            if not self.configs:
                self._resolve_configs(db)
            self._refresh_broker_trades(db)
            synced = self._sync_rates(db)

            if self.live_mode:
                per_symbol = self._cycle_live(db, synced)
            else:
                per_symbol = self._cycle_backtest(db, synced)

        stale = _stale_sources(self._sources)
        if stale:
            log.warning(
                "source changed since this watcher started: %s â€” it is still "
                "running the OLD code. Stop and restart watch.bat.",
                ", ".join(stale[:6]) + (" ..." if len(stale) > 6 else ""),
            )

        status = {
            "version": int(time.time() * 1000),
            # Surfaced on the page: a stale watcher silently omits whatever the
            # payload builder gained since it started.
            "stale_sources": stale,
            "mt5": self._mt5_note,
            "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z",
            "updated_myt": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S") + " MYT",
            "strategy": self.base_cfg.name,
            "timeframe": self.base_cfg.timeframe,
            "symbols": per_symbol,
            "runs": dict(self.runs),
            "comparison": self.comparison,
            "cycle_seconds": round(time.time() - started, 1),
            "next_update_in_s": self.interval_s,
            "live_interval_s": self.live_interval_s,
            "live_mode": self.live_mode,
            "files": {sym: payload_name(sym) for sym in self.symbols},
            "rates_note": self._rates_note,
            # Non-empty only for a symbol whose MOST RECENT open-interest sync
            # attempt failed. Read this before guessing at a stale OI pane --
            # it is the actual exception, not a reconstruction of one.
            "oi_errors": dict(self._oi_errors),
            # The rule's own state, at the top level rather than nested under a
            # symbol: it decides for the account, not for whichever chart is on
            # screen, and the page must show it even while you are looking at BTC.
            "rule": self._rule or None,
            "gate_settings": (asdict(self.engine.gate) if self.engine else None),
            # The session map (Asia range, Europe sweeps, US confirmation) per
            # symbol, top-level like the rules: it describes the market, not
            # whichever chart happens to be on screen.
            "sessions": {s: b for s, b in self._sessions.items()},
            # SIGNAL 3, alongside the rule rather than merged into it. A reader
            # that does not know about it sees exactly what it saw before.
            "rule2": self._rule2 or None,
            "rule3": self._rule3 or None,
            "rule4": self._rule4 or None,
            "rule5": self._rule5 or None,
            # SIGNAL 7 — the ExpD regime gate, experimental channel.
            "rule7": self._rule7 or None,
            # Whether pushes are actually going anywhere.
            "notify": {
                "backend": type(self._notifier).__name__ if self._notifier
                           else "none",
                "channels": sorted(self._notify_channels),
                "sent": getattr(self._notifier, "sent", None),
                "failed": getattr(self._notifier, "failed", None),
            },
            # Emitted signals. The page beeps for any id it has not seen, so this
            # has to survive a status refresh — it is the whole alert channel.
            # The mined signals prefix their ids `s2:`..`s5:` so no two channels
            # can ever collide and silently suppress one another.
            "events": ((self.engine.events() if self.engine else [])
                       + (self.signal2.events() if self.signal2 else [])
                       + (self.signal3.events() if self.signal3 else [])
                       + (self.signal4.events() if self.signal4 else [])
                       + (self.signal5.events() if self.signal5 else [])
                       + (self.signal7.events() if self.signal7 else [])
                       + list(self._session_events)),
            "keepawake": _keepawake_status(),
        }
        with self._lock:
            self._status = status
        atomic_write(self.status_path, json.dumps(status, indent=1))
        self._push_events()
        return status

    def refit_signal4(self, days: int = 5, min_win: float = 0.85,
                      min_per_day: float = 3.0) -> dict:
        """Re-mine Signal 4 now, from the page, and report what happened.

        Calls exactly the same entry point the weekly scheduled task calls, so
        the button and the schedule cannot drift into doing different things.
        Everything about the search -- including its refusal to write anything
        when nothing meets the bar -- is the miner's behaviour, unchanged.

        Returns a summary rather than a bare success flag, because "nothing met
        the bar, last week's pattern is still live" is the interesting outcome
        and is indistinguishable from success by an exit code alone.
        """
        import contextlib
        import io as _io

        from .engine.signal4 import CONFIG_PATH, Signal4Config
        from .tools.mine_signal4 import main as mine_main

        if not self._refit_lock.acquire(blocking=False):
            return {"ok": False, "busy": True,
                    "message": "a refit is already running"}
        try:
            before = None
            try:
                before = CONFIG_PATH.read_text(encoding="utf-8")
            except OSError:
                pass

            buf = _io.StringIO()
            argv = ["--days", str(int(days)), "--min-win", str(float(min_win)),
                    "--min-per-day", str(float(min_per_day))]
            try:
                with contextlib.redirect_stdout(buf):
                    rc = int(mine_main(argv))
            except Exception as exc:
                log.warning("signal4 refit failed: %s", exc)
                return {"ok": False, "rc": 1,
                        "message": f"{type(exc).__name__}: {exc}"}

            after = None
            try:
                after = CONFIG_PATH.read_text(encoding="utf-8")
            except OSError:
                pass
            updated = bool(after and after != before)

            out: dict = {"ok": rc in (0, 2), "rc": rc, "updated": updated,
                         "log": buf.getvalue()[-4000:]}
            if rc == 2:
                out["message"] = ("nothing met the bar -- the previous pattern "
                                  "is still live")
            elif updated:
                cfg = Signal4Config.load()
                if cfg is not None:
                    f = cfg.fitted or {}
                    out.update({
                        "message": "config updated",
                        "pattern": cfg.label, "side": cfg.side,
                        "win_rate": f.get("win_rate"), "trades": f.get("trades"),
                        "per_day": f.get("per_day"),
                        "conditions_tested": f.get("conditions_tested"),
                        "days": f.get("days"),
                    })
                # Apply it to the live evaluator immediately rather than waiting
                # for the next cycle, so the panel agrees with the button.
                if self.signal4 is not None:
                    self.signal4.reload_if_changed()
                    try:
                        with Database(self.db_path) as db:
                            self._rule4 = self.signal4.evaluate(
                                db, spot=self._last_price("XAUUSDT"))
                    except Exception as exc:
                        log.debug("signal4 re-evaluate after refit failed: %s", exc)
            else:
                out["message"] = "search finished but the config did not change"
            return out
        finally:
            self._refit_lock.release()

    def refresh_open_interest(self, symbols: list[str] | None = None) -> dict[str, dict]:
        """One-shot OI catch-up for the manual retry button on the page.

        Exists because the automatic sync has twice gone silent for hours at a
        time inside this process, while an isolated retry of the EXACT SAME
        call (`_sync_context`, unchanged here) has succeeded immediately every
        time -- see the `_write_lock` note in `__init__`. Until that is root-
        caused, a person needs a way to fix it themselves rather than wait on
        a diagnosis, and `oi_errors` in status.json needs a way to actually
        clear between now and the next scheduled cycle.

        Rebuilds the affected payload file(s) immediately rather than leaving
        that to the next scheduled cycle (`interval_s`, 5 minutes by default):
        a button that fixes the database but leaves the chart looking broken
        for another five minutes has not fixed anything a person can see.
        """
        from .engine.live_signal import read_log

        targets = [s for s in (symbols or self.symbols) if not is_mt5(s)]
        result: dict[str, dict] = {}
        with self._write_lock, Database(self.db_path) as db, BybitClient() as client:
            sync = MarketDataSync(client, db)
            for sym in targets:
                before = sync.meta.newest_open_interest(sym, self.OI_INTERVAL)
                got = self._sync_context(sync, sym)
                after = sync.meta.newest_open_interest(sym, self.OI_INTERVAL)
                result[sym] = {
                    "rows": got.get("oi", 0),
                    "advanced": after != before,
                    "newest_myt": (datetime.fromtimestamp(after / 1000, MYT)
                                   .strftime("%Y-%m-%d %H:%M") if after else None),
                    "error": self._oi_errors.get(sym),
                }

            if self.live_mode and self.engine is not None and self.configs:
                rule = self._rule or {}
                signals = read_log(self.engine.signal.log_path)
                for sym in targets:
                    cfg = self.configs.get(sym)
                    if cfg is None:
                        continue
                    payload = build_live_payload(
                        db, sym, tuple(cfg.features.timeframes),
                        indicator_config=cfg.indicators,
                        max_bars=self.max_bars,
                        strategy_name=f"{self.base_cfg.name} (live)",
                        strategy_timeframe=self.engine.cfg.anchor,
                        signals=signals if sym == self.engine.cfg.symbol else [],
                        config_yaml=self.config_path.read_text(encoding="utf-8"),
                        config_hash=rule.get("config_hash", ""),
                        qty=cfg.sizing.qty,
                        signal3_live_events=(self.signal3.events()
                                             if sym == self.engine.cfg.symbol
                                             and self.signal3 else None),
                    )
                    write_payload_json(payload, self.report_dir / payload_name(sym))
                    if sym == self.symbols[0]:
                        render(self._book(payload, sym), self.index)
        return result

    def _push_events(self) -> None:
        """Send any signal that has fired since the last check. Never raises."""
        if self._notifier is None or not self._notify_channels:
            return
        try:
            from .notify import telegram as _tg

            channels = [
                ("rule", self.engine.events() if self.engine else []),
                ("signal2", self.signal2.events() if self.signal2 else []),
                ("signal3", self.signal3.events() if self.signal3 else []),
                ("signal4", self.signal4.events() if self.signal4 else []),
                ("signal5", self.signal5.events() if self.signal5 else []),
                ("signal7", self.signal7.events() if self.signal7 else []),
                ("session", list(self._session_events)),
            ]
            for name, evs in channels:
                for e in evs:
                    eid = e.get("id")
                    if not eid or eid in self._notified:
                        continue
                    if name not in self._notify_channels:
                        self._notified.add(eid)
                        continue
                    try:
                        ok = False
                        if name == "session":
                            if e.get("type") == "day_verdict":
                                ok = self._notifier.send(_tg.day_verdict(
                                    symbol=e.get("symbol", "?"),
                                    bias=str(e.get("bias") or "?"),
                                    basis=list(e.get("basis") or []),
                                    as_of_myt=e.get("bar_myt", ""),
                                ), priority=_tg.P2)
                            else:
                                ok = self._notifier.send(_tg.session_event(
                                    symbol=e.get("symbol", "?"),
                                    kind=e.get("type", "?"),
                                    price=float(e.get("price") or 0.0),
                                    level=float(e.get("level") or 0.0),
                                    bar_myt=e.get("bar_myt", ""),
                                    vol_ok=e.get("vol_ok"),
                                ), priority=_tg.P2)
                        else:
                            ok = self._notifier.send(_tg.signal_fired(
                                name=name, symbol=e.get("symbol", "?"),
                                side=e.get("side", "?"),
                                price=float(e.get("entry_ref") or 0.0),
                                sl=float(e.get("sl") or 0.0),
                                tp=float(e.get("tp") or 0.0),
                                lots=float(e.get("lots") or 0.0),
                                bar_myt=e.get("bar_myt", ""),
                                pattern=e.get("pattern", ""),
                                measured=(name == "rule"),
                            ), priority=_tg.P1)
                        if ok:
                            self._notified.add(eid)
                        else:
                            log.warning("telegram send FAILED for %s — "
                                        "will retry next pass", eid)
                    except Exception as exc:
                        log.debug("notify send failed for %s: %s", eid, exc)
        except Exception as exc:
            log.debug("notify pass failed: %s", exc)

    def _tick_sessions(self, repo, sym: str, minute: pd.DataFrame,
                       now_ms: int) -> dict | None:
        """Update session liquidity map for a symbol from fresh minute data.

        The first call for a symbol is absorbed silently (no events emitted).
        Subsequent calls emit events for state changes (sweep, break, reclaim,
        day_verdict).
        """
        from .engine import session_map as sm

        if minute is None or minute.empty:
            return None

        # The map needs the WHOLE day: Asia 07:00–15:00, yesterday's H/L and
        # yesterday's US session (spans midnight). The live 1m pull only
        # reaches 6h back, so past 21:00 MYT the Asia window fell out of the
        # frame, the map collapsed to waiting_asia with null levels, and the
        # day verdict thrashed (short → range → undecided…) — which is also
        # what the Telegram notifications were spamming. Stored 1m (synced
        # every slow cycle) carries the history, the live pull carries the
        # freshest closed minutes; merge them, live wins on a duplicate.
        try:
            stored = repo.load_tail(sym, "1m", 2880)   # ~2 days of minutes
            if not stored.empty:
                minute = (pd.concat([stored, minute])
                          .drop_duplicates("open_time", keep="last")
                          .sort_values("open_time")
                          .reset_index(drop=True))
        except Exception as exc:
            log.debug("session minute merge fell back to the live pull: %s",
                      exc)

        block = sm.build_map(minute, as_of_ms=now_ms)
        prev = self._sessions.get(sym)
        self._sessions[sym] = block

        if prev is None:
            return block

        # Emit events for state changes
        if block.get("state") != prev.get("state"):
            state = block.get("state", "")
            # Map internal state names to event types
            event_type = state
            if state == "swept_high":
                event_type = "sweep_high"
            elif state == "swept_low":
                event_type = "sweep_low"
            elif state == "broken_high":
                event_type = "break_high"
            elif state == "broken_low":
                event_type = "break_low"
            elif state == "reclaimed_high":
                event_type = "reclaim_high"
            elif state == "reclaimed_low":
                event_type = "reclaim_low"

            # The announcement must carry the EVENT's own facts, not the
            # map's session aggregates: the price where it happened, the
            # Asia boundary it happened to, and the volume verdict. The
            # map's own events list has the real record — take the newest
            # one of this type; fall back to the aggregates only if the
            # map has no event yet (a state set by a quiet return).
            lv = block.get("levels") or {}
            ev_price = (lv.get("session_high") or lv.get("session_low")
                        or 0.0)
            ev_level = lv.get("session_high") or 0.0
            if "low" in event_type:
                ev_level = lv.get("asia_low") or ev_level
            else:
                ev_level = lv.get("asia_high") or ev_level
            vol_ok = None
            for me in reversed(block.get("events") or []):
                if me.get("type") == event_type:
                    ev_price = float(me.get("price") or ev_price)
                    vol_ok = me.get("vol_ok")
                    break

            self._session_events.append({
                "id": f"ses:{sym}@{now_ms}",
                "type": event_type,
                "kind": "session_event",
                "symbol": sym,
                "price": ev_price,
                "level": ev_level,
                "bar_myt": sm._fmt_myt(now_ms),
                "vol_ok": vol_ok,
            })

        # Day verdict event
        curr_bias = block.get("bias")
        prev_bias = prev.get("bias")
        if isinstance(curr_bias, dict) and isinstance(prev_bias, dict):
            if curr_bias.get("bias") != prev_bias.get("bias"):
                self._session_events.append({
                    "id": f"ses:{sym}@:verdict:{curr_bias.get('bias')}",
                    "type": "day_verdict",
                    "symbol": sym,
                    "bias": curr_bias.get("bias"),
                    "basis": curr_bias.get("basis", []),
                    "bar_myt": sm._fmt_myt(now_ms),
                })
        elif curr_bias != prev_bias:
            self._session_events.append({
                "id": f"ses:{sym}@:verdict:{curr_bias}",
                "type": "day_verdict",
                "symbol": sym,
                "bias": curr_bias,
                "basis": block.get("basis", []),
                "bar_myt": sm._fmt_myt(now_ms),
            })

        return block

    def _tick_signal7(self, db, sym, block, spot):
        """Evaluate Signal 7 (ExpD gate) on every fresh session map."""
        if self.signal7 is None or block is None:
            return
        if sym != self.signal7.cfg.symbol:
            return
        try:
            r7 = self.signal7.evaluate(db, block, spot=spot)
            self._rule7 = r7
            self.signal7.write(r7)
        except Exception as exc:
            log.debug("signal7 fast tick failed: %s", exc)

    def _tick_signal3(self, db, sym, block, spot, minute=None):
        """Evaluate Signal 3 v2 on every fresh session map.

        The live 1m pull goes in too: the sweep frame reads the database,
        which only syncs every `interval_s`, so without `minute` a sweep
        that closed two minutes ago would not be seen — or notified — until
        the next slow cycle.
        """
        if self.signal3 is None or block is None:
            return
        if sym != self.signal3.cfg.symbol:
            return
        try:
            r3 = self.signal3.evaluate(db, block, spot=spot, minute=minute)
            self._rule3 = r3
            self.signal3.write(r3)
        except Exception as exc:
            log.debug("signal3 fast tick failed: %s", exc)

    def _tick_signal5(self, db: Database, repo: CandleRepository) -> None:
        """Evaluate Signal 5 once per NEW 5m bar, from the fast loop.

        Anchored on 5m, so the slow cycle is too coarse to see every bar (see
        `_s5_bar`). Gating on the bar's open time keeps this to one evaluation
        per bar rather than one per 5-second tick, and makes a missed bar
        impossible rather than merely unlikely.
        """
        if self.signal5 is None:
            return
        try:
            cfg = self.signal5.cfg
            tail = repo.load_tail(cfg.symbol, cfg.anchor, 2)
            if tail.empty:
                return
            newest = int(tail["open_time"].iloc[-1])
            if newest == self._s5_bar:
                return
            self._s5_bar = newest
            r5 = self.signal5.evaluate(
                db, spot=self._last_price(cfg.symbol))
            self._rule5 = r5
            self.signal5.write(r5)
        except Exception as exc:                 # a tick must never die for this
            log.debug("signal5 fast tick failed: %s", exc)

    def live_tick(self, client):
        """One 1m request per symbol -> forming bars for every timeframe."""
        now_ms = int(time.time() * 1000)
        out = {}

        with Database(self.db_path) as db:
            if not self.configs:
                self._resolve_configs(db)
            repo = CandleRepository(db)
            meta = MetaRepository(db)
            self._tick_signal5(db, repo)

            for sym in self.symbols:
                cfg = self.configs[sym]
                if is_mt5(sym):
                    minute = self._stored_minute(repo, sym, now_ms)
                else:
                    try:
                        fresh = client.klines(sym, "1m", now_ms - 6 * 3_600_000,
                                              now_ms, max_pages=1)
                    except BybitError as exc:
                        log.debug("live 1m fetch failed for %s: %s", sym, exc)
                        fresh = []
                    minute = pd.DataFrame(
                        fresh,
                        columns=["open_time", "open", "high", "low", "close",
                                 "volume", "turnover"],
                    )
                price = float(minute["close"].iloc[-1]) if not minute.empty else None

                ses_block = self._tick_sessions(repo, sym, minute, now_ms)
                self._tick_signal7(db, sym, ses_block, price)
                self._tick_signal3(db, sym, ses_block, price, minute=minute)

                widest = max(INTERVAL_MS[tf] for tf in cfg.features.timeframes)
                oi_df = meta.load_open_interest(
                    sym, self.OI_INTERVAL,
                    now_ms - (SEND_BARS + 2) * widest, now_ms)
                oi_ts = oi_df["ts"].to_numpy(dtype="int64")
                oi_val = oi_df["oi"].to_numpy(dtype="float64")

                series = {}
                for tf in cfg.features.timeframes:
                    df = repo.load_tail(sym, tf, TAIL_BARS)
                    if df.empty:
                        continue
                    extra, forming_t = fold_from_minute(
                        minute, tf, int(df["open_time"].iloc[-1]), now_ms)
                    if extra:
                        df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
                    closed_n = len(df) - (1 if forming_t is not None else 0)

                    ind = compute_indicators(df, tf, cfg.indicators)
                    k = min(max(SEND_BARS, len(extra) + 1), len(df))
                    sl = slice(len(df) - k, len(df))
                    entry = {
                        "t": [int(v) for v in df["open_time"].to_numpy()[sl]],
                        "o": [_round(v) for v in df["open"].to_numpy()[sl]],
                        "h": [_round(v) for v in df["high"].to_numpy()[sl]],
                        "l": [_round(v) for v in df["low"].to_numpy()[sl]],
                        "c": [_round(v) for v in df["close"].to_numpy()[sl]],
                        "v": [_round(v, 0) for v in df["volume"].to_numpy()[sl]],
                        "forming_t": forming_t,
                    }
                    for col in LIVE_COLUMNS:
                        entry[col] = [_round(v, LIVE_DP.get(col, 2))
                                      for v in ind[col].to_numpy()[sl]]
                    buy = np.flatnonzero(ind["ut_buy"].to_numpy()[:closed_n])
                    sell = np.flatnonzero(ind["ut_sell"].to_numpy()[:closed_n])
                    ot = df["open_time"].to_numpy()
                    lo = len(df) - k
                    entry["ut_buy"] = [int(ot[i]) for i in buy if i >= lo]
                    entry["ut_sell"] = [int(ot[i]) for i in sell if i >= lo]
                    entry["oi"] = oi_on_bars(ot[sl], INTERVAL_MS[tf], oi_ts, oi_val)
                    series[tf] = entry

                out[sym] = {"price": _round(price, 4), "series": series,
                            "sessions": self._sessions.get(sym)}

        with self._lock:
            prev = self._status
        for sym, blob in out.items():
            info = (prev.get("symbols") or {}).get(sym) or {}
            if info.get("live"):
                blob["live"] = info["live"]

        prices = {sym: blob.get("price") for sym, blob in out.items()}
        if self.engine is not None:
            try:
                with Database(self.db_path) as db:
                    for sym in self.symbols:
                        risk = self.engine.risk(db, sym, spot=prices.get(sym))
                        self._risk[sym] = risk
                        if sym in out:
                            out[sym]["risk"] = risk
            except Exception as exc:
                log.debug("live risk read failed: %s", exc)

        payload = {
            "version": now_ms,
            "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z",
            "updated_myt": datetime.now(MYT).strftime("%H:%M:%S") + " MYT",
            "interval_s": self.live_interval_s,
            "symbols": out,
            "rule": self._rule or None,
            "rule2": self._rule2 or None,
            "rule3": self._rule3 or None,
            "rule4": self._rule4 or None,
            "rule5": self._rule5 or None,
            "rule7": self._rule7 or None,
            "events": ((self.engine.events() if self.engine else [])
                       + (self.signal2.events() if self.signal2 else [])
                       + (self.signal3.events() if self.signal3 else [])
                       + (self.signal4.events() if self.signal4 else [])
                       + (self.signal5.events() if self.signal5 else [])
                       + (self.signal7.events() if self.signal7 else [])
                       + list(self._session_events)),
        }
        with self._lock:
            if self._status:
                self._status["live_prices"] = prices
        atomic_write(self.live_path, json.dumps(payload, separators=(",", ":")))
        self._push_events()
        return payload

    # -- live signal state on the last CLOSED bar --------------------------

    def _live_signals(self, db: Database, cfg: StrategyConfig) -> dict:
        """Evaluate every registered signal on the last CLOSED bar."""
        repo = CandleRepository(db)
        df = repo.load(cfg.symbol, cfg.timeframe)
        if df.empty:
            return {"live_error": "no candles"}

        ind = compute_indicators(df, cfg.timeframe, cfg.indicators)

        htf = {}
        from .engine import features as F
        frames = {}
        for tf in cfg.features.timeframes:
            d = repo.load(cfg.symbol, tf)
            if d.empty:
                continue
            frames[tf] = (d["open_time"].to_numpy(dtype="int64"),
                          compute_indicators(d, tf, cfg.indicators))
        idx = F.build_htf_alignment(df["open_time"].to_numpy(dtype="int64"),
                                    cfg.timeframe, frames)
        for tf, (_ot, frame) in frames.items():
            if tf != cfg.timeframe:
                htf[tf] = F.aligned_frame(frame, idx[tf])

        ctx = SignalContext(
            ind=ind,
            close=df["close"].to_numpy(),
            high=df["high"].to_numpy(),
            low=df["low"].to_numpy(),
            open_time=df["open_time"].to_numpy(dtype="int64"),
            thresholds={
                "ut_near_atr": cfg.entry.ut_near_atr,
                "zone_near_atr": cfg.entry.zone_near_atr,
                "dynamic_near_atr": cfg.entry.dynamic_near_atr,
                "zone_touch_atr": cfg.entry.zone_touch_atr,
            },
            htf=htf,
        )

        i = len(df) - 1
        firing = []
        for name in sorted(REGISTRY):
            try:
                if bool(evaluate_signal(name, ctx)[i]):
                    firing.append(name)
            except Exception:  # a signal needing data this run lacks
                continue

        last = ind.iloc[i]
        bar_ms = int(df["open_time"].iloc[i])
        return {
            # Raw epoch ms as well as the formatted strings: the page renders in
            # whichever zone the reader picked, and cannot do that from a string.
            "last_closed_bar_ms": bar_ms,
            "last_closed_bar_utc": datetime.fromtimestamp(
                bar_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z",
            "last_closed_bar_myt": datetime.fromtimestamp(
                bar_ms / 1000, MYT).strftime("%Y-%m-%d %H:%M") + " MYT",
            "last_price": _round(df["close"].iloc[i], 4),
            "ut_level": _round(last.get("ut_level"), 4),
            "atr_14": _round(last.get("atr_14"), 4),
            "rsi_14": _round(last.get("rsi_14"), 4),
            "adx_14": _round(last.get("adx_14"), 4),
            "firing": firing,
            # Would the configured entry fire on this bar? The honest live read.
            "entry_would_fire": _entry_state(cfg, ctx, i),
        }

    # -- server + loops ----------------------------------------------------

    def _stored_minute(self, repo: CandleRepository, symbol: str,
                       now_ms: int) -> pd.DataFrame:
        """The last few hours of stored 1m bars, in the shape the folder wants."""
        # 6 hours of 1m bars, by count rather than by timestamp: gold's market
        # is closed at the weekend and over the daily rollover, so a time filter
        # returns NOTHING and the forming bar (and the price pill) go blank on
        # exactly the instrument this page exists for.
        d = repo.load_tail(symbol, "1m", 360)
        if d.empty:
            return d
        return d[d["open_time"] >= now_ms - 30 * 86_400_000].reset_index(drop=True)

    def _live_loop(self, on_tick=None):
        """Runs until stopped. A failed tick is logged and retried, never fatal."""
        with BybitClient() as client:
            while not self._stop.is_set():
                if self.configs and self.index.exists():
                    try:
                        payload = self.live_tick(client)
                        if on_tick:
                            on_tick(payload)
                    except Exception as exc:
                        log.debug("live tick failed: %s", exc)
                self._stop.wait(self.live_interval_s)

    def run(self, on_cycle=None, on_tick=None):
        self._lock_file.acquire()
        try:
            self.serve()
            threading.Thread(target=self._live_loop, args=(on_tick,), daemon=True).start()
            while not self._stop.is_set():
                try:
                    status = self.cycle()
                    if on_cycle:
                        on_cycle(status)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    log.exception("cycle failed: %s", exc)
                self._stop.wait(self.interval_s)
        finally:
            self._lock_file.release()

    def stop(self):
        self._stop.set()


def _entry_state(cfg: StrategyConfig, ctx: SignalContext, i: int) -> dict:
    """Per-condition truth for the configured entry on the last closed bar.

    Shows WHICH leg is blocking, not just whether the entry fired — that is the
    thing you actually want when watching live.
    """
    from .engine.signals import evaluate_condition

    out: dict[str, bool] = {}
    def walk(node, label: str) -> bool:
        if node.signal:
            v = bool(evaluate_signal(node.signal, ctx)[i])
            out[node.signal] = v
            return v
        if node.expr:
            v = bool(evaluate_condition(node, ctx)[i])
            out[f"expr: {node.expr}"] = v
            return v
        if node.all_of:
            return all(walk(c, label) for c in list(node.all_of))
        if node.any_of:
            return any([walk(c, label) for c in list(node.any_of)])
        return False

    fired = True
    if cfg.entry.all_of:
        fired &= all(walk(n, "all_of") for n in cfg.entry.all_of)
    if cfg.entry.any_of:
        fired &= any([walk(n, "any_of") for n in cfg.entry.any_of])
    return {"fired": bool(fired), "conditions": out}
