"""SIGNAL 2 — a mined pattern, kept deliberately separate from the measured rule.

This module shares NO code path with `live_signal.py` / `live_engine.py`. It has
its own config, its own log file (`signals2.jsonl`), its own event list and its
own emitted-bar set. Nothing here can change what the original rule decides, and
that separation is the point: the original rule is the measured system and this
is not.

## What this is

    SHORT XAUUSDT 15m when ALL THREE are true on the last CLOSED bar:
        minus_di            >= 34.7074      (top-decile downside directional)
        |close - ut_level| / atr_14 <= 1.1336   (price pinned to the UT line)
        adx_14              >  31.5998      (established trend, not chop)

    Bracket: TP -25 / SL +25 in price, fixed. 96-bar (24 h) time stop.

## What was measured, and on what

Mined on **XAUUSDT 15m, 2026-05-28 -> 2026-08-26** (the last three months, which
is the window the owner asked for and the only window it was fitted to):

    25 de-overlapped trades: 19 win / 2 loss / 4 timeout  =  90.5%
    net +417.90, +16.72 per fire, about 8 fires per month

## What is NOT true about it, stated here so it cannot be lost

This pattern was selected out of **more than 555,000 tested hypotheses** on a
window containing only ~332 independent trades. At that ratio a 90% win rate
arrives by chance, and the usual corrections do not merely weaken it, they erase
it: Bonferroni on its p=0.0001 over that many tests gives a corrected p above 1.

It was then checked against MT5:GOLD over **2025-04 -> 2026-05**, a different
book and thirteen months that had no role in choosing it:

    72 trades, 47.8% win, net -117.50   (baseline over the same span: 48.9%)

So it does not generalise. It is kept because the owner asked for it after being
shown these numbers, explicitly to watch rather than to follow. The honest
description is "a pattern that fits the last three months", and the `measured`
flag below says so to anything that reads this signal's output.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Signal2Config:
    """Thresholds exactly as fitted. Changing one makes it a different pattern."""

    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    anchor: str = "15m"
    side: str = "short"

    minus_di_min: float = 34.7074
    ut_distance_atr_max: float = 1.1336
    adx_min: float = 31.5998

    tp_usd: float = 25.0
    sl_usd: float = 25.0
    time_stop_bars: int = 96
    lots: float = 0.01

    # Set False so every consumer -- the page, the AI brief, a future executor --
    # can tell this apart from the rule that was actually validated.
    measured: bool = False

    @property
    def label(self) -> str:
        return "-DI >= 34.7 + price pinned to UT (<=1.13 ATR) + ADX > 31.6"

    @property
    def bracket_label(self) -> str:
        return (f"SHORT TP -{self.tp_usd:g} / SL +{self.sl_usd:g} in price, "
                f"fixed, time stop {self.time_stop_bars} bars")

    def bracket(self, entry: float) -> tuple[float, float]:
        """(sl, tp) for a SHORT. Short profits when price falls."""
        return (entry + self.sl_usd, entry - self.tp_usd)


def fire_mask(df, ind, cfg: Signal2Config | None = None):
    """Vectorised: which bars the pattern fires on. One definition, two callers.

    `Signal2.evaluate` reads the newest bar for the live panel; the chart needs
    every bar in the window so the rail can show the pattern's whole history.
    Both go through here, so the marker under a bar and the panel that fired it
    can never disagree about what the pattern is.
    """
    import numpy as np

    c = cfg or Signal2Config()
    close = np.asarray(df["close"], dtype="float64")
    atr = np.asarray(ind["atr_14"], dtype="float64")
    ut = np.asarray(ind["ut_level"], dtype="float64")
    adx = np.asarray(ind["adx_14"], dtype="float64")
    mdi = np.asarray(ind["minus_di"], dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        ut_dist = np.abs(close - ut) / atr
    ok = np.isfinite(atr) & (atr > 0) & np.isfinite(ut_dist)
    return (ok
            & (mdi >= c.minus_di_min)
            & (ut_dist <= c.ut_distance_atr_max)
            & (adx > c.adx_min))


def _round(v: Any, dp: int = 4) -> Any:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, dp)


class Signal2:
    """Evaluates the pattern on the newest CLOSED bar and logs every evaluation.

    Logs non-firing bars too, for the same reason `live_signal` does: "it never
    triggered" and "the watcher was not running" are different answers, and only
    a row per evaluated bar can tell them apart.
    """

    def __init__(self, log_path: Path, cfg: Signal2Config | None = None,
                 indicators: IndicatorConfig | None = None) -> None:
        self.cfg = cfg or Signal2Config()
        self.ind_cfg = indicators or IndicatorConfig()
        self.log_path = Path(log_path)
        self._events: list[dict] = []
        self._emitted: set[tuple[str, int]] = set()
        self._seeded = False

    # -- evaluation --------------------------------------------------------

    def evaluate(self, db: Database, spot: float | None = None) -> dict:
        """One evaluation of the newest closed anchor bar. Never raises."""
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "name": "signal2",
            "symbol": c.symbol,
            "broker_symbol": c.broker_symbol,
            "anchor": c.anchor,
            "side": c.side,
            "pattern": c.label,
            "bracket": c.bracket_label,
            "measured": c.measured,
            "fired": False,
            "legs": {},
            "blocking": [],
        }
        try:
            df = CandleRepository(db).load_tail(c.symbol, c.anchor, 400)
            if df.empty or len(df) < 60:
                out["reason"] = f"not enough {c.anchor} bars for {c.symbol}"
                return out
            ind = compute_indicators(df, c.anchor, self.ind_cfg)
            i = len(df) - 1

            close = float(df["close"].iloc[i])
            atr = float(ind["atr_14"].iloc[i])
            ut = float(ind["ut_level"].iloc[i])
            adx = float(ind["adx_14"].iloc[i])
            mdi = float(ind["minus_di"].iloc[i])
            bar_ms = int(df["open_time"].iloc[i])

            if not np.isfinite(atr) or atr <= 0:
                out["reason"] = "ATR not finite yet (warm-up)"
                return out

            ut_dist = abs(close - ut) / atr
            legs = {
                f"-DI >= {c.minus_di_min:g}": bool(mdi >= c.minus_di_min),
                f"|price-UT| <= {c.ut_distance_atr_max:g} ATR": bool(
                    ut_dist <= c.ut_distance_atr_max),
                f"ADX > {c.adx_min:g}": bool(adx > c.adx_min),
            }
            blocking = [k for k, v in legs.items() if not v]

            entry_ref = float(spot) if spot else close
            sl, tp = c.bracket(entry_ref)
            step = INTERVAL_MS[c.anchor]

            out.update({
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bar_open_ms": bar_ms,
                "bar_myt": datetime.fromtimestamp(bar_ms / 1000, MYT)
                           .strftime("%Y-%m-%d %H:%M MYT"),
                "legs": legs,
                "blocking": blocking,
                "fired": not blocking,
                "close": _round(close, 2),
                "minus_di": _round(mdi, 2),
                "adx_14": _round(adx, 2),
                "ut_level": _round(ut, 2),
                "ut_distance_atr": _round(ut_dist, 3),
                "atr_14": _round(atr, 4),
                "reason": ("all legs true" if not blocking
                           else "legs false: " + ", ".join(blocking)),
            })
            if not blocking:
                out.update({
                    "entry_ref": _round(entry_ref, 2),
                    "sl": _round(sl, 2),
                    "tp": _round(tp, 2),
                    "lots": c.lots,
                    "time_stop_ms": bar_ms + c.time_stop_bars * step,
                })
                self._record_event(out)
            else:
                out.update({"entry_ref": None, "sl": None, "tp": None,
                            "lots": c.lots, "time_stop_ms": None})
        except Exception as exc:                 # must never break a cycle
            log.debug("signal2 evaluation failed: %s", exc)
            out["reason"] = f"evaluation failed: {type(exc).__name__}: {exc}"
        return out

    # -- the alert channel -------------------------------------------------

    def _record_event(self, out: dict) -> None:
        key = (out["symbol"], out["bar_open_ms"])
        if key in self._emitted:
            return
        self._emitted.add(key)
        self._events.append({
            "id": f"s2:{out['symbol']}@{out['bar_open_ms']}",
            "name": "signal2",
            "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"], "bar_open_ms": out["bar_open_ms"],
            "bar_myt": out["bar_myt"],
            "entry_ref": out["entry_ref"], "sl": out["sl"], "tp": out["tp"],
            "lots": out["lots"], "time_stop_ms": out["time_stop_ms"],
            "bracket": out["bracket"], "measured": out["measured"],
            "pattern": out["pattern"],
        })
        log.warning("SIGNAL2 %s SHORT @ %s  SL %s  TP %s  (mined pattern, "
                    "NOT the measured rule)",
                    out["symbol"], out["entry_ref"], out["sl"], out["tp"])

    def events(self) -> list[dict]:
        return list(self._events)

    def seed_from_log(self) -> None:
        """A restart must not re-alert on bars already in the log."""
        if self._seeded:
            return
        self._seeded = True
        try:
            for row in read_log(self.log_path):
                if row.get("fired") and row.get("bar_open_ms") is not None:
                    self._emitted.add((row.get("symbol", ""),
                                       int(row["bar_open_ms"])))
        except Exception as exc:
            log.debug("signal2 could not seed emitted bars: %s", exc)

    # -- the log -----------------------------------------------------------

    def write(self, evaluation: dict) -> bool:
        """Append one evaluation. Skips a bar already written."""
        bar = evaluation.get("bar_open_ms")
        if bar is None:
            return False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._already_logged(evaluation.get("symbol", ""), int(bar)):
                return False
            line = json.dumps(evaluation, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            log.debug("signal2 log write failed: %s", exc)
            return False

    def _already_logged(self, symbol: str, bar_ms: int) -> bool:
        try:
            for row in read_log(self.log_path):
                if (row.get("symbol") == symbol
                        and int(row.get("bar_open_ms", -1)) == bar_ms):
                    return True
        except Exception:
            pass
        return False


def read_log(path: Path) -> list[dict]:
    """Every evaluation ever written. A bad line is skipped, never fatal."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
