"""The always-on loop: risk read + validated rule + gate + emit.

This is what replaces re-running a backtest every five minutes. Nothing here
walks 166 days of history looking for trades. It answers one question on every
tick — *right now, on the last CLOSED bar, does the rule fire, and does the risk
read allow it* — and emits a signal when both say yes.

## Who decides what

```
engine/live_signal.py   the RULE      decides WHEN        measured: +3.3 pts vs a
                                                          matched random null
engine/risk_engine.py   the RISK      decides WHETHER     hand-set weights, never
                        read          (veto only)         measured on gold
```

The rule fires; the gate can only subtract. That split is deliberate and is the
whole reason this file exists rather than one blended score. `RiskExecutiveEngine`
is a good live read and an unmeasured trigger, and `docs/FEATURE_MINE.md` records
six filters that looked just as sensible and died on measurement. Turning the
risk read into the trigger would spend the only measured edge the project has to
buy one with no evidence behind it.

**Every evaluation is logged, gated or not.** `signals.jsonl` already records why
nothing fired; a veto is now one more reason, written into `extras.gate` with the
Danger / Confidence that caused it. Six months from now "the rule fired and the
gate blocked it" has to be distinguishable from "the rule never fired", and from
"the process was not running".

**Closed bars only, and one signal per bar.** De-duplication is by
`bar_open_ms`, so a restart, a double tick or a clock wobble cannot emit the same
entry twice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..data.db import CandleRepository, Database
from ..indicators.base import IndicatorConfig
from . import risk_engine as R
from . import risk_feed
from .live_signal import LiveSignal, RuleConfig, read_log

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

# Anchor timeframe per symbol for the RISK read. The rule's own anchor is fixed
# by `RuleConfig` and is not affected by this.
DEFAULT_ANCHOR = "1h"
RISK_ANCHOR: dict[str, str] = {"MT5:GOLD": "15m", "XAUUSDT": "15m"}

# How many emitted signals the page can look back over.
EVENT_HISTORY = 50

# Bars per timeframe the live rule evaluation reads. `tests/test_live.py` pins a
# 1,500-bar tail as bit-identical to full history for these columns, and
# `tests/test_live_signal_tail.py` re-checks it on the rule's own legs. Full
# history on the MT5 panel is ~400k bars and ~24 s per closed bar; this is ~1 s.
LIVE_TAIL_BARS = 1500


class LiveEngine:
    """Owns the risk snapshot per symbol and the rule+gate on the rule's symbol."""

    def __init__(
        self,
        rule_cfg: RuleConfig | None = None,
        gate_settings: R.GateSettings | None = None,
        log_path: Path | None = None,
        indicators: IndicatorConfig | None = None,
        emit: bool = True,
    ) -> None:
        self.cfg = rule_cfg or RuleConfig()
        self.gate = gate_settings or R.GateSettings()
        self.ind_cfg = indicators or IndicatorConfig()
        self.signal = LiveSignal(self.cfg, log_path=log_path, indicators=self.ind_cfg,
                                 tail_bars=LIVE_TAIL_BARS)
        self.emit = emit
        self._events: deque[dict] = deque(maxlen=EVENT_HISTORY)
        # (symbol, bar) — the SAME key `LiveSignal.logged_bars` returns. It was
        # a bare int, which silently made `seed_from_log` a no-op: the seeded
        # tuples never matched the int being tested, so every restart re-alerted
        # for signals that had fired days earlier.
        self._emitted_bars: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._rule_cache: tuple[int, dict] | None = None
        self._seeded = False
        # Tickets this rule opened. None until first read; re-read whenever the
        # log gains a link, which only happens at order time.
        self._tickets: set[int] | None = None

    # -- risk --------------------------------------------------------------

    def risk(self, db: Database, symbol: str, spot: float | None = None) -> dict:
        """The `RiskSnapshot` for one symbol, as a JSON-ready dict.

        Cheap on repeat: `risk_feed.cached_inputs` only recomputes when a new
        anchor bar has closed, so a 5-second tick costs the evaluation and the
        sim, not a full indicator pass.
        """
        anchor = RISK_ANCHOR.get(symbol, DEFAULT_ANCHOR)
        fixed = self.cfg.bracket_mode == "dollars"
        settings = risk_feed.settings_for(
            tp_atr=self.cfg.tp_atr, sl_atr=self.cfg.sl_atr,
            tp_usd=self.cfg.tp_usd if fixed else None,
            sl_usd=self.cfg.sl_usd if fixed else None,
            time_stop_bars=self.cfg.time_stop_bars, anchor=anchor,
        )
        try:
            out = risk_feed.snapshot(db, symbol, anchor, self.ind_cfg, spot, settings)
        except Exception as exc:              # never let a risk read kill the tick
            log.debug("risk snapshot failed for %s: %s", symbol, exc)
            return {"error": str(exc).splitlines()[0], "symbol": symbol}
        out["gate_settings"] = asdict(self.gate)
        # What the gate WOULD do to a signal on each side, shown whether or not
        # the rule fired — otherwise the first time you see the gate is the time
        # it blocked something, and you have no feel for how often it bites.
        if "error" not in out:
            snap = _rebuild(out)
            out["gate_preview"] = {
                "long": R.gate(snap, "LONG", self.gate).as_dict(),
                "short": R.gate(snap, "SHORT", self.gate).as_dict(),
            }
        return out

    # -- rule + gate -------------------------------------------------------

    def rule(self, db: Database, spot: float | None = None,
             spread: float | None = None) -> dict:
        """Evaluate the measured rule on its own symbol, gate it, maybe emit.

        Returns a dict for the page whether or not anything fired.
        """
        sym = self.cfg.symbol
        repo = CandleRepository(db)
        latest = repo.load_tail(sym, self.cfg.anchor, 1)
        if latest.empty:
            return {"error": f"no {self.cfg.anchor} candles for {sym}", "symbol": sym}
        bar_ms = int(latest["open_time"].iloc[-1])

        with self._lock:
            if self._rule_cache and self._rule_cache[0] == bar_ms:
                out = dict(self._rule_cache[1])
                out["cached"] = True
                return out

        # `build_context` memoises, but its key now carries the newest stored bar
        # rather than `id(db)`, so a new bar is a miss and stale frames cannot be
        # served. Nothing to invalidate here any more.
        try:
            ev = self.signal.evaluate_latest(db, spread=spread,
                                             position_since_ms=self.open_position_ms(db))
        except Exception as exc:
            log.warning("rule evaluation failed: %s", exc)
            return {"error": str(exc).splitlines()[0], "symbol": sym}

        risk = self.risk(db, sym, spot)
        gres = None
        if "error" not in risk:
            gres = R.gate(_rebuild(risk), self.cfg.side, self.gate)

        # `fired` = all legs true, in session, warm. `would_enter` additionally
        # requires a free slot. The gate applies to `would_enter`, not to `fired`:
        # blocking a signal that was never going to open a trade would inflate the
        # veto rate by the same 5.7x that conflating the two rates would.
        sent = bool(ev.would_enter and (gres is None or gres.passed))
        vetoed = bool(ev.would_enter and gres is not None and not gres.passed)

        out = {
            "symbol": sym, "broker_symbol": self.cfg.broker_symbol,
            "anchor": self.cfg.anchor, "side": self.cfg.side,
            "bar_open_ms": ev.bar_open_ms,
            "bar_myt": datetime.fromtimestamp(ev.bar_open_ms / 1000, MYT)
                       .strftime("%Y-%m-%d %H:%M") + " MYT",
            "fired": ev.fired, "would_enter": ev.would_enter,
            "sent": sent, "vetoed": vetoed,
            "legs": ev.legs, "blocking": ev.blocking,
            "close": ev.close, "atr": ev.atr_entry,
            "entry_ref": ev.entry_ref, "sl": ev.sl, "tp": ev.tp,
            "time_stop_ms": ev.time_stop_ms, "lots": ev.lots,
            "in_session": ev.in_session,
            "position_since_ms": ev.position_since_ms,
            "manual_positions_block": self.cfg.manual_positions_block,
            "reason": ev.reason_if_skipped,
            "config_hash": ev.config_hash,
            "gate": gres.as_dict() if gres else None,
            "bracket": self.cfg.bracket_label(),
            "bracket_measured": self.cfg.is_measured_bracket(),
            "cached": False,
        }

        # Write the evaluation with the gate attached, then emit if it survived.
        if self.emit:
            ev.extras = {"risk": _slim(risk), "gate": gres.as_dict() if gres else None,
                         "sent": sent, "vetoed": vetoed}
            try:
                self.signal.write([ev])
            except Exception as exc:
                log.warning("could not append to %s: %s", self.signal.log_path, exc)
            if sent:
                self._record_event(out)
                self._tickets = None      # a link may follow; re-read next time

        with self._lock:
            self._rule_cache = (bar_ms, out)
        return out

    def open_position_ms(self, db: Database) -> int | None:
        """Open time of the oldest position occupying a slot, else None.

        **Only positions this rule opened count**, unless
        `manual_positions_block` is set. The owner trades this instrument by hand
        and is routinely several positions deep; counting those against
        `max_concurrent` does not reproduce the measured system's queue, it just
        makes the rule go quiet for as long as a human is trading — which is
        precisely when its opinion is worth seeing. See `RuleConfig`.

        A position is "this rule's" when its ticket appears in a `link` record in
        `signals.jsonl`, which is written by `attach_ticket` at order time. With
        `execute: false` nothing has ever been linked, so every slot is free and
        every firing bar produces a visible signal.
        """
        try:
            rows = db.conn.execute(
                "SELECT open_time, position_id FROM broker_trades WHERE symbol = ? "
                "AND close_time IS NULL ORDER BY open_time",
                (self.cfg.symbol,)).fetchall()
        except Exception:
            return None
        if not self.cfg.manual_positions_block:
            mine = self._linked_tickets()
            rows = [r for r in rows if r[1] is not None and int(r[1]) in mine]
        opens = [int(r[0]) for r in rows if r[0] is not None]
        if len(opens) < self.cfg.max_concurrent:
            return None
        return min(opens)

    def _linked_tickets(self) -> set[int]:
        """Tickets this rule opened, from the log's `link` records."""
        if self._tickets is None:
            found: set[int] = set()
            try:
                for rec in read_log(self.signal.log_path):
                    if rec.get("kind") == "link" and rec.get("order_ticket"):
                        found.add(int(rec["order_ticket"]))
            except Exception as exc:
                log.debug("could not read linked tickets: %s", exc)
            self._tickets = found
        return self._tickets

    # -- emitted-signal history -------------------------------------------

    def _record_event(self, out: dict) -> None:
        key = (out["symbol"], out["bar_open_ms"])
        with self._lock:
            if key in self._emitted_bars:
                return
            self._emitted_bars.add(key)
            self._events.append({
                "id": f"{out['symbol']}@{out['bar_open_ms']}",
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ts_ms": int(time.time() * 1000),
                "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
                "side": out["side"], "bar_open_ms": out["bar_open_ms"],
                "bar_myt": out["bar_myt"],
                "entry_ref": out["entry_ref"], "sl": out["sl"], "tp": out["tp"],
                "lots": out["lots"], "time_stop_ms": out["time_stop_ms"],
                "gate": out["gate"], "bracket": out["bracket"],
            })
        log.warning("SIGNAL %s %s @ %s  SL %s  TP %s  %s lots",
                    out["symbol"], out["side"].upper(), out["entry_ref"],
                    out["sl"], out["tp"], out["lots"])

    def seed_from_log(self) -> None:
        """Load already-emitted bars so a restart cannot re-alert on old signals.

        Without this, restarting the watcher would re-emit every entry still in
        `signals.jsonl` as if it had just happened — the page would beep for a
        trade from last Tuesday.
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            with self._lock:
                self._emitted_bars |= self.signal.logged_bars()
        except Exception as exc:
            log.debug("could not seed emitted bars: %s", exc)

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)


def _slim(risk: dict) -> dict:
    """Just enough of the risk read to explain a decision in the audit log.

    The full snapshot carries every reason string and the whole sim; storing that
    on every 15-minute bar would grow `signals.jsonl` by ~2 KB a line for data
    already reproducible from the candles.
    """
    if "error" in risk:
        return {"error": risk["error"]}
    return {
        "long_danger": risk["long"]["danger"], "long_conf": risk["long"]["confidence"],
        "short_danger": risk["short"]["danger"], "short_conf": risk["short"]["confidence"],
        "bias": risk["bias"],
        "sim_matches": (risk.get("historical_sim") or {}).get("matches"),
        "sim_long_sl_first": (risk.get("historical_sim") or {}).get("long_sl_first_prob"),
        "missing": risk.get("missing_inputs", []),
    }


def _rebuild(risk: dict) -> R.RiskSnapshot:
    """Re-hydrate just enough of a snapshot dict for `gate()` to read it.

    `gate()` only touches danger, confidence, bias and missing_inputs, so the
    reasons and sim are not rebuilt. Keeping the gate reading the dataclass rather
    than the dict means there is exactly one definition of what the gate looks at.
    """
    def score(d: dict, direction: str) -> R.RiskDirectionScore:
        return R.RiskDirectionScore(
            direction=direction, danger=int(d["danger"]),
            confidence=int(d["confidence"]), trend_pts=int(d.get("trend_pts", 0)),
            orderbook_pts=int(d.get("orderbook_pts", 0)),
            momentum_pts=int(d.get("momentum_pts", 0)), reasons=())
    return R.RiskSnapshot(
        long=score(risk["long"], R.LONG), short=score(risk["short"], R.SHORT),
        bias=risk["bias"], historical_sim=None,
        data_confidence_note=risk.get("data_confidence_note", ""),
        tp_pct=float(risk.get("tp_pct") or 0.0), sl_pct=float(risk.get("sl_pct") or 0.0),
        missing_inputs=tuple(risk.get("missing_inputs") or ()),
    )
