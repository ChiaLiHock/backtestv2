"""SIGNAL 5 — FAST-C. The only pattern here that got BETTER out of sample.

Independent of every other signal, same as 2/3/4: own config, own log, own
events, no import of the measured rule.

## What this is

    SHORT XAUUSDT **5m** when ALL THREE are true on the last CLOSED bar:
        atr_14          >  4.4262        (volatility high enough to resolve fast)
        30m macd_hist   > -0.124451      (30m momentum not falling)
        4h  adx_14      > 22.185962      (4h actually trending)

    Bracket: TP -10 / SL +10 in price, fixed. 96-bar time stop.
    Designed for **3 concurrent positions**, not one.

## Why it is different from signals 2, 3 and 4

Those were selected for a high win rate and each decayed or inverted when moved
off the window it was fitted to. This one was selected for SPEED, and its edge
held up where it was never fitted:

                     trades/day   win%     net/trade      p        baseline
    discovery 90d       16.60    53.41%     +0.321      0.139       51.24%
    HOLDOUT   80d       16.66    55.50%     +0.709      0.0054      52.41%

Note the direction of travel. On the discovery window it was NOT significant
(p=0.139); on the untouched holdout it was (p=0.0054), and the net per trade
more than doubled. Overfitting produces the opposite pattern every time. It is
also positive in all five chronological blocks of the full history
(52.2 / 53.2 / 54.0 / 56.6 / 56.3%).

## Why speed is the mechanism

Realised duration is almost entirely a volatility variable -- Spearman between
`atr_14` and log duration is **-0.720**. Selecting high ATR selects trades that
resolve in ~24-35 minutes instead of ~90, and that is what lets three
concurrent slots carry 16 trades a day.

The same search established the cost of this: fast bars are close to coin flips.
Win rate by realised-duration decile runs 49.6% (fastest) to 55.8% (slowest), so
speed and edge are anti-correlated by construction. That is why this is a **2-3
point edge, not a 75% pattern**, and why it needs the frequency to be worth
anything at all.

## The honest caveats

* **Break-even is 52.0%** (+10 nets 9.605, -10 nets -10.395). A 53.4% discovery
  win rate is +1.4 points, not +1.4x.
* **The short side has a tailwind in this data** -- taking every bar short scored
  51.2% on discovery and 52.4% on holdout, against 48.7%/47.4% long. So the
  comparison that matters is against those contemporaneous baselines (+2.2 and
  +3.1 points), never against 50%.
* **It needs three slots.** At one position at a time the frequency collapses and
  the whole point of it goes with it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1
BREAKEVEN = 0.51987          # +10/-10 at 0.85bps


@dataclass(frozen=True)
class Signal5Config:
    """Thresholds exactly as fitted. Not knobs."""

    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    anchor: str = "5m"
    side: str = "short"

    atr_min: float = 4.4262
    macd_hist_30m_min: float = -0.124451
    adx_4h_min: float = 22.185962

    tp_usd: float = 10.0
    sl_usd: float = 10.0
    time_stop_bars: int = 96
    lots: float = 0.01
    max_concurrent: int = 3

    measured: bool = False

    @property
    def label(self) -> str:
        return (f"ATR > {self.atr_min:g} + 30m MACD hist > {self.macd_hist_30m_min:g} "
                f"+ 4h ADX > {self.adx_4h_min:g}")

    @property
    def bracket_label(self) -> str:
        return (f"SHORT TP -{self.tp_usd:g} / SL +{self.sl_usd:g} in price, fixed, "
                f"time stop {self.time_stop_bars} bars "
                f"(break-even {BREAKEVEN*100:.1f}%, up to "
                f"{self.max_concurrent} open at once)")

    def bracket(self, entry: float) -> tuple[float, float]:
        """(sl, tp) for a SHORT."""
        return (entry + self.sl_usd, entry - self.tp_usd)


def confirm_series(db: Database, symbol: str, tf: str, column: str,
                   anchor_close_ms: np.ndarray,
                   ind_cfg: IndicatorConfig) -> np.ndarray:
    """A higher timeframe's column as of each anchor bar's CLOSE.

    Indexed by the higher bar's close, never its open: a 4h bar with three hours
    left to run has not told anyone its ADX yet, and using it would be reading
    the future.
    """
    d2 = CandleRepository(db).load(symbol, tf).reset_index(drop=True)
    if d2.empty:
        return np.full(anchor_close_ms.size, np.nan)
    i2 = compute_indicators(d2, tf, ind_cfg)
    if column not in i2:
        return np.full(anchor_close_ms.size, np.nan)
    closes = d2["open_time"].to_numpy(dtype="int64") + INTERVAL_MS[tf]
    src = i2[column].to_numpy(dtype="float64")
    idx = np.searchsorted(closes, anchor_close_ms, side="right") - 1
    out = np.full(anchor_close_ms.size, np.nan)
    ok = idx >= 0
    out[ok] = src[idx[ok]]
    return out


def fire_mask(ind, macd30: np.ndarray, adx4h: np.ndarray,
              cfg: Signal5Config | None = None) -> np.ndarray:
    """Vectorised fire test. One definition, shared by the panel and the chart."""
    c = cfg or Signal5Config()
    atr = np.asarray(ind["atr_14"], dtype="float64")
    ok = np.isfinite(atr) & np.isfinite(macd30) & np.isfinite(adx4h)
    return (ok
            & (atr > c.atr_min)
            & (macd30 > c.macd_hist_30m_min)
            & (adx4h > c.adx_4h_min))


def cap_concurrency(open_time: np.ndarray, exit_ms: np.ndarray, k: int) -> np.ndarray:
    """Take a signal only when fewer than k positions are open. Chronological.

    This signal is measured at k=3 and quoting its frequency without saying so
    would be meaningless -- unlimited concurrency and one-at-a-time give wildly
    different answers for the same condition.
    """
    keep = np.zeros(open_time.size, dtype=bool)
    live: list[float] = []
    for i in range(open_time.size):
        if not np.isfinite(exit_ms[i]):
            continue
        t = open_time[i]
        live = [e for e in live if e > t]
        if len(live) < k:
            keep[i] = True
            live.append(exit_ms[i])
    return keep


def _round(v: Any, dp: int = 4) -> Any:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, dp)


class Signal5:
    """Evaluates FAST-C on the newest CLOSED 5m bar; logs every evaluation."""

    def __init__(self, log_path: Path, cfg: Signal5Config | None = None,
                 indicators: IndicatorConfig | None = None) -> None:
        self.cfg = cfg or Signal5Config()
        self.ind_cfg = indicators or IndicatorConfig()
        self.log_path = Path(log_path)
        self._events: list[dict] = []
        self._emitted: set[tuple[str, int]] = set()
        self._open: list[dict] = []
        self._seeded = False

    def evaluate(self, db: Database, spot: float | None = None) -> dict:
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "name": "signal5",
            "symbol": c.symbol, "broker_symbol": c.broker_symbol,
            "anchor": c.anchor, "side": c.side,
            "pattern": c.label, "bracket": c.bracket_label,
            "measured": c.measured, "max_concurrent": c.max_concurrent,
            "fired": False, "legs": {}, "blocking": [],
        }
        try:
            df = CandleRepository(db).load_tail(c.symbol, c.anchor, 400)
            if df.empty or len(df) < 60:
                out["reason"] = f"not enough {c.anchor} bars for {c.symbol}"
                return out
            ind = compute_indicators(df, c.anchor, self.ind_cfg)
            step = INTERVAL_MS[c.anchor]
            close_ms = df["open_time"].to_numpy(dtype="int64") + step
            macd30 = confirm_series(db, c.symbol, "30m", "macd_hist",
                                    close_ms, self.ind_cfg)
            adx4h = confirm_series(db, c.symbol, "4h", "adx_14",
                                   close_ms, self.ind_cfg)

            i = len(df) - 1
            atr = float(ind["atr_14"].iloc[i])
            m30 = float(macd30[i]) if np.isfinite(macd30[i]) else float("nan")
            a4 = float(adx4h[i]) if np.isfinite(adx4h[i]) else float("nan")
            cl = float(df["close"].iloc[i])
            bar_ms = int(df["open_time"].iloc[i])

            legs = {
                f"ATR > {c.atr_min:g}": bool(np.isfinite(atr) and atr > c.atr_min),
                f"30m MACD hist > {c.macd_hist_30m_min:g}":
                    bool(np.isfinite(m30) and m30 > c.macd_hist_30m_min),
                f"4h ADX > {c.adx_4h_min:g}":
                    bool(np.isfinite(a4) and a4 > c.adx_4h_min),
            }
            blocking = [k for k, v in legs.items() if not v]
            entry_ref = float(spot) if spot else cl
            sl, tp = c.bracket(entry_ref)
            self._release_slots(df, int(time.time() * 1000))

            out.update({
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bar_open_ms": bar_ms,
                "bar_myt": datetime.fromtimestamp(bar_ms / 1000, MYT)
                           .strftime("%Y-%m-%d %H:%M MYT"),
                "legs": legs, "blocking": blocking, "fired": not blocking,
                "atr_14": _round(atr, 3), "macd_hist_30m": _round(m30, 4),
                "adx_4h": _round(a4, 2), "close": _round(cl, 2),
                "reason": ("all legs true" if not blocking
                           else "legs false: " + ", ".join(blocking)),
            })
            if not blocking:
                out.update({"entry_ref": _round(entry_ref, 2),
                            "sl": _round(sl, 2), "tp": _round(tp, 2),
                            "lots": c.lots,
                            "time_stop_ms": bar_ms + c.time_stop_bars * step})
                took = self._record_event(out)
                out["capped"] = not took
                if not took:
                    out["reason"] = (f"pattern true but all {c.max_concurrent} "
                                     "slots are open -- skipped, as measured")
            else:
                out.update({"entry_ref": None, "sl": None, "tp": None,
                            "lots": c.lots, "time_stop_ms": None})
            out["open_slots"] = len(self._open)
        except Exception as exc:
            log.debug("signal5 evaluation failed: %s", exc)
            out["reason"] = f"evaluation failed: {type(exc).__name__}: {exc}"
        return out

    def _release_slots(self, df: pd.DataFrame, now_ms: int) -> None:
        """Free any slot whose TP, SL or time stop has been reached.

        The live path used to have no concurrency limit at all, while the
        measurement applies `cap_concurrency` at k=3. That gap is not cosmetic:
        this pattern stays true for 15-35 minutes at a stretch, so one episode
        fires on every 5m bar in the run. Measured on 2026-08-27, 33 live fires
        were only 7 distinct episodes -- runs of 4, 7, 6, 6, 6, 3 and 1 bars.
        Uncapped, that is ~53 alerts a day for the same handful of trades, and
        the 16.6/day at 55.5% that was validated describes the capped stream,
        not this one.

        A slot is released on the first bar AFTER its signal bar that touches
        either barrier -- entry is the next bar's open, as measured. Which
        barrier came first is not decided here; either way the slot frees, and
        win/loss accounting is the report's job, not the gate's.
        """
        if not self._open:
            return
        ot = df["open_time"].to_numpy(dtype="int64")
        hi = df["high"].to_numpy(dtype="float64")
        lo = df["low"].to_numpy(dtype="float64")
        still: list[dict] = []
        for slot in self._open:
            after = ot > slot["bar_open_ms"]
            if after.any() and (bool((hi[after] >= slot["sl"]).any())
                                or bool((lo[after] <= slot["tp"]).any())):
                continue
            if now_ms >= slot["time_stop_ms"]:
                continue
            still.append(slot)
        self._open = still

    def _record_event(self, out: dict) -> bool:
        """Emit unless this bar is already known or every slot is occupied."""
        key = (out["symbol"], out["bar_open_ms"])
        if key in self._emitted:
            return False
        if len(self._open) >= self.cfg.max_concurrent:
            return False
        self._emitted.add(key)
        self._open.append({"bar_open_ms": out["bar_open_ms"],
                           "sl": float(out["sl"]), "tp": float(out["tp"]),
                           "time_stop_ms": int(out["time_stop_ms"])})
        self._events.append({
            "id": f"s5:{out['symbol']}@{out['bar_open_ms']}",
            "name": "signal5", "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"], "bar_open_ms": out["bar_open_ms"],
            "bar_myt": out["bar_myt"], "entry_ref": out["entry_ref"],
            "sl": out["sl"], "tp": out["tp"], "lots": out["lots"],
            "time_stop_ms": out["time_stop_ms"], "bracket": out["bracket"],
            "measured": False, "pattern": out["pattern"],
        })
        log.warning("SIGNAL5 %s SHORT @ %s  SL %s  TP %s  (FAST-C, ~%d min hold)",
                    out["symbol"], out["entry_ref"], out["sl"], out["tp"], 30)
        return True

    def events(self) -> list[dict]:
        return list(self._events)

    def seed_from_log(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        try:
            for row in read_log(self.log_path):
                if row.get("fired") and row.get("bar_open_ms") is not None:
                    self._emitted.add((row.get("symbol", ""),
                                       int(row["bar_open_ms"])))
        except Exception as exc:
            log.debug("signal5 could not seed emitted bars: %s", exc)

    def write(self, evaluation: dict) -> bool:
        """Only FIRING bars are logged.

        Unlike the other signals this one runs on a 5m grid and fires ~16 times
        a day, so logging every evaluation would add ~288 lines a day and the
        dedup scan re-reads the file on each write. The firing bars are what the
        chart and any later audit actually need.
        """
        if not evaluation.get("fired"):
            return False
        bar = evaluation.get("bar_open_ms")
        if bar is None:
            return False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            for row in read_log(self.log_path):
                if (row.get("symbol") == evaluation.get("symbol")
                        and int(row.get("bar_open_ms", -1)) == int(bar)):
                    return False
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evaluation, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            log.debug("signal5 log write failed: %s", exc)
            return False


def read_log(path: Path) -> list[dict]:
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
