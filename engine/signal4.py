"""SIGNAL 4 — a REFITTED pattern. The config is data, not code.

Signals 2 and 3 have their thresholds written into the source, because they were
each fitted once. Signal 4 is meant to be re-mined every week against the last
five trading days, so hard-coding it would mean editing and re-testing Python on
a schedule -- and the thing most likely to break under that is the agreement
between what was mined and what runs live.

So the pattern lives in `configs/signal4.json`, written by
`tools/mine_signal4.py`, and BOTH the miner and this evaluator compute their
features through `build_features()` below. There is one definition of every
feature name, so a refit cannot silently mean something different at runtime.

## The bracket is ASYMMETRIC

    LONG or SHORT, TP +15 / SL -25 in price, fixed, 96-bar (24 h) time stop.

Risking 25 to make 15 moves break-even to **63.5%**, not the ~51% a symmetric
bracket needs:

    win nets  15 - 0.395 =  14.605
    loss nets -25 - 0.395 = -25.395
    break-even = 25.395 / 40 = 0.6349

A 70% win rate here is barely above water; the previous signals' 70% was a large
edge. The two numbers are not comparable and the panel says so.

## Weekends

Signals are only taken when the ENTRY bar falls Monday-Friday MYT. Entry is the
bar AFTER the signal bar, so a bar opening 23:45 Sunday enters at 00:00 Monday
and counts as a Monday trade; a bar opening 23:45 Friday enters Saturday and is
dropped. Classifying by the signal bar instead would put that Friday trade in
the week, which is not when it opens.

## What the config is worth

Five trading days is a very small sample. At 3 trades a day that is ~15 trades,
and 13 of 15 (86.7%) carries a raw p around 0.05 BEFORE accounting for the
thousands of conditions the miner tries. `configs/signal4.json` records the
sample size, the search size and the baseline alongside the thresholds so the
panel can show what the number rests on rather than only the number.
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
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database, MetaRepository
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1
CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "signal4.json"

TP_USD = 15.0
SL_USD = 25.0
TIME_STOP_BARS = 96
COST_BPS = 0.85
BREAKEVEN = 0.63488

HTF = ("5m", "30m", "1h", "4h")
HTF_COLS = ("rsi_14", "adx_14", "bb_percent_b", "ema_alignment", "ut_bias",
            "relative_volume", "confirmed_bias", "macd_hist", "atr_14",
            "ut_position", "volatility_percentile", "directional_score")

OPS = {
    "<": lambda v, t: v < t,
    "<=": lambda v, t: v <= t,
    ">": lambda v, t: v > t,
    ">=": lambda v, t: v >= t,
    "==": lambda v, t: v == t,
}


def build_features(db: Database, symbol: str, anchor: str,
                   ind_cfg: IndicatorConfig | None = None,
                   tail: int | None = None) -> pd.DataFrame:
    """Every named feature a Signal 4 condition may reference.

    Deliberately the SAME construction the miner uses, imported by it rather
    than reimplemented, so "rsi_slope_3" cannot mean one thing during the search
    and another at 3am on a Tuesday.
    """
    ind_cfg = ind_cfg or IndicatorConfig()
    repo = CandleRepository(db)
    df = (repo.load_tail(symbol, anchor, tail) if tail
          else repo.load(symbol, anchor)).reset_index(drop=True)
    if df.empty:
        return pd.DataFrame()
    ind = compute_indicators(df, anchor, ind_cfg)
    step = INTERVAL_MS[anchor]
    ot = df["open_time"].to_numpy(dtype="int64")
    close_ms = ot + step

    f = pd.DataFrame({"open_time": ot})
    for c in ind.columns:
        if c != "open_time":
            f[c] = pd.to_numeric(ind[c], errors="coerce").to_numpy(dtype="float64")
    # Raw OHLCV as well: the miner needs `open` to price the next-bar fill, and
    # the live evaluator needs `close` for the entry reference. Deriving those
    # from an EMA as a fallback would put a different number in the panel than
    # the one the mining used.
    for c in ("open", "high", "low", "close", "volume"):
        f[c] = df[c].to_numpy(dtype="float64")

    o = df["open"].to_numpy(dtype="float64")
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c_ = df["close"].to_numpy(dtype="float64")
    v = df["volume"].to_numpy(dtype="float64")
    atr = f["atr_14"].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        f["ret_1"] = np.r_[np.nan, np.diff(c_)]
        f["ret_3"] = c_ - np.r_[[np.nan] * 3, c_[:-3]]
        f["ret_6"] = c_ - np.r_[[np.nan] * 6, c_[:-6]]
        f["body_atr"] = (c_ - o) / atr
        f["range_atr"] = (h - l) / atr
        f["upper_wick_atr"] = (h - np.maximum(o, c_)) / atr
        f["lower_wick_atr"] = (np.minimum(o, c_) - l) / atr
        f["close_pos_in_bar"] = np.where(h > l, (c_ - l) / (h - l), 0.5)
        for n in (7, 14, 28):
            f[f"dist_ema{n}_atr"] = (c_ - f[f"ema_{n}"].to_numpy()) / atr
        f["dist_ut_atr"] = (c_ - f["ut_level"].to_numpy()) / atr
        f["dist_vwap_atr"] = (c_ - f["vwap"].to_numpy()) / atr
        for n in (12, 24, 48, 96):
            hh = pd.Series(h).rolling(n).max().to_numpy()
            ll = pd.Series(l).rolling(n).min().to_numpy()
            f[f"pos_range_{n}"] = np.where(hh > ll, (c_ - ll) / (hh - ll), 0.5)
            f[f"dist_hh{n}_atr"] = (c_ - hh) / atr
            f[f"dist_ll{n}_atr"] = (c_ - ll) / atr
        f["rsi_slope_3"] = f["rsi_14"] - f["rsi_14"].shift(3)
        f["adx_slope_3"] = f["adx_14"] - f["adx_14"].shift(3)
        f["atr_ratio_24"] = atr / pd.Series(atr).rolling(24).mean().to_numpy()
        f["vol_ratio_24"] = v / pd.Series(v).rolling(24).mean().to_numpy()

    for tf in HTF:
        if tf == anchor:
            continue
        d2 = repo.load(symbol, tf).reset_index(drop=True)
        if d2.empty:
            continue
        i2 = compute_indicators(d2, tf, ind_cfg)
        closes = d2["open_time"].to_numpy(dtype="int64") + INTERVAL_MS[tf]
        idx = np.searchsorted(closes, close_ms, side="right") - 1
        ok = idx >= 0
        # Assembled as a block and concatenated once. Adding twelve columns
        # per timeframe one at a time is what fragmented the frame in the first
        # place, and pandas warns on every single one of them.
        block = {}
        for col in HTF_COLS:
            if col not in i2:
                continue
            src = i2[col].to_numpy(dtype="float64")
            arr = np.full(ot.size, np.nan)
            arr[ok] = src[idx[ok]]
            block[f"{tf}_{col}"] = arr
        if block:
            f = pd.concat([f, pd.DataFrame(block, index=f.index)], axis=1)

    # ~130 columns added one at a time leaves the frame badly fragmented, which
    # pandas warns about and which measurably slows the weekly refit. De-fragment
    # once here -- after the higher-timeframe loop that adds most of them, and
    # before the remaining blocks, so nothing downstream keeps re-triggering it.
    f = f.copy()

    try:
        oi = MetaRepository(db).load_open_interest(
            symbol, "15m", int(ot[0]) - step, int(ot[-1]) + step)
        if not oi.empty:
            ots = oi["ts"].to_numpy(dtype="int64")
            ov = oi["oi"].to_numpy(dtype="float64")
            i_now = np.searchsorted(ots, close_ms, side="right") - 1
            i_prev = np.searchsorted(ots, ot, side="right") - 1
            cur = np.where(i_now >= 0, ov[np.clip(i_now, 0, ov.size - 1)], np.nan)
            prv = np.where(i_prev >= 0, ov[np.clip(i_prev, 0, ov.size - 1)], np.nan)
            f["oi"] = cur
            f["oi_delta"] = cur - prv
            with np.errstate(divide="ignore", invalid="ignore"):
                f["oi_delta_pct"] = (cur - prv) / prv * 100.0
                base = pd.Series(cur).rolling(96).mean().to_numpy()
                f["oi_dev_pct"] = (cur - base) / base * 100.0
                f["oi_chg_24"] = cur - pd.Series(cur).shift(24).to_numpy()
    except Exception as exc:
        log.debug("signal4: open interest unavailable: %s", exc)

    t = pd.to_datetime(close_ms, unit="ms", utc=True).tz_convert("Etc/GMT-8")
    f["hour_myt"] = t.hour.to_numpy()
    f["dow_myt"] = t.dayofweek.to_numpy()
    f["is_asia"] = ((t.hour >= 6) & (t.hour < 15)).astype(float)
    f["is_london"] = ((t.hour >= 15) & (t.hour < 21)).astype(float)
    f["is_ny"] = ((t.hour >= 21) | (t.hour < 6)).astype(float)
    return f


def entry_is_weekday(open_time: np.ndarray, step: int) -> np.ndarray:
    """True where the bar AFTER this one opens Mon-Fri MYT.

    Entry, not signal: the trade opens on the next bar, so a Friday 23:45 signal
    is a Saturday trade and does not count, while a Sunday 23:45 signal enters
    Monday 00:00 and does.
    """
    entry_ms = np.asarray(open_time, dtype="int64") + step
    dow = pd.to_datetime(entry_ms, unit="ms", utc=True) \
            .tz_convert("Etc/GMT-8").dayofweek.to_numpy()
    return dow <= 4


def bracket(entry: float, side: str) -> tuple[float, float]:
    """(sl, tp) for the asymmetric TP15/SL25 bracket."""
    if side == "long":
        return (entry - SL_USD, entry + TP_USD)
    return (entry + SL_USD, entry - TP_USD)


def fire_mask(feat: pd.DataFrame, conditions: list[dict]) -> np.ndarray:
    """AND of every condition. Unknown feature or op -> fires nothing.

    Failing closed matters: a config referring to a feature this build does not
    produce must go quiet, never fire on a half-evaluated rule.
    """
    if feat.empty or not conditions:
        return np.zeros(len(feat), dtype=bool)
    m = np.ones(len(feat), dtype=bool)
    for c in conditions:
        col, op, thr = c.get("feature"), c.get("op"), c.get("value")
        if col not in feat.columns or op not in OPS:
            log.warning("signal4: unusable condition %r -- firing nothing", c)
            return np.zeros(len(feat), dtype=bool)
        v = feat[col].to_numpy(dtype="float64")
        m &= OPS[op](v, float(thr)) & np.isfinite(v)
    return m


@dataclass
class Signal4Config:
    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    anchor: str = "15m"
    side: str = "long"
    conditions: list[dict] = field(default_factory=list)
    lots: float = 0.01
    measured: bool = False
    # Provenance, written by the miner. Shown on the page so the win rate is
    # never displayed without what it rests on.
    fitted: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Signal4Config | None":
        p = Path(path or CONFIG_PATH)
        if not p.exists():
            return None
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("signal4: cannot read %s: %s", p, exc)
            return None
        return cls(
            symbol=blob.get("symbol", "XAUUSDT"),
            broker_symbol=blob.get("broker_symbol", "GOLD"),
            anchor=blob.get("anchor", "15m"),
            side=blob.get("side", "long"),
            conditions=blob.get("conditions") or [],
            lots=float(blob.get("lots", 0.01)),
            fitted=blob.get("fitted") or {},
        )

    @property
    def label(self) -> str:
        return " AND ".join(
            f"{c.get('feature')} {c.get('op')} {c.get('value'):g}"
            for c in self.conditions) or "(no conditions)"

    @property
    def bracket_label(self) -> str:
        return (f"{self.side.upper()} TP +{TP_USD:g} / SL -{SL_USD:g} in price, "
                f"fixed, time stop {TIME_STOP_BARS} bars "
                f"(break-even {BREAKEVEN*100:.1f}%)")


def _round(v: Any, dp: int = 4) -> Any:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else round(x, dp)


class Signal4:
    """Config-driven evaluator. Quiet and harmless when no config exists."""

    def __init__(self, log_path: Path, config_path: Path | None = None,
                 indicators: IndicatorConfig | None = None) -> None:
        self.log_path = Path(log_path)
        self.config_path = Path(config_path or CONFIG_PATH)
        self.ind_cfg = indicators or IndicatorConfig()
        self.cfg = Signal4Config.load(self.config_path)
        self._events: list[dict] = []
        self._emitted: set[tuple[str, int]] = set()
        self._seeded = False
        self._mtime: float | None = self._stat()

    def _stat(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Pick up a weekly refit without restarting the watcher.

        The refit is the whole point of this signal, and requiring a restart to
        apply one is the kind of step that gets skipped.
        """
        m = self._stat()
        if m is not None and m != self._mtime:
            self._mtime = m
            self.cfg = Signal4Config.load(self.config_path)
            log.warning("signal4: config reloaded -- %s",
                        self.cfg.label if self.cfg else "(unreadable)")
            return True
        return False

    def evaluate(self, db: Database, spot: float | None = None) -> dict:
        self.reload_if_changed()
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "name": "signal4", "measured": False,
            "fired": False, "legs": {}, "blocking": [],
        }
        if c is None or not c.conditions:
            out["reason"] = ("no signal4 config yet -- run "
                             "`python -m backtest.tools.mine_signal4`")
            return out
        out.update({"symbol": c.symbol, "broker_symbol": c.broker_symbol,
                    "anchor": c.anchor, "side": c.side, "pattern": c.label,
                    "bracket": c.bracket_label, "fitted": c.fitted})
        try:
            feat = build_features(db, c.symbol, c.anchor, self.ind_cfg, tail=600)
            if feat.empty or len(feat) < 60:
                out["reason"] = f"not enough {c.anchor} bars for {c.symbol}"
                return out
            i = len(feat) - 1
            step = INTERVAL_MS[c.anchor]
            bar_ms = int(feat["open_time"].iloc[i])

            legs: dict[str, bool] = {}
            for cond in c.conditions:
                col, op, thr = cond.get("feature"), cond.get("op"), cond.get("value")
                label = f"{col} {op} {float(thr):g}"
                if col not in feat.columns or op not in OPS:
                    legs[label] = False
                    continue
                v = feat[col].to_numpy(dtype="float64")[i]
                legs[label] = bool(np.isfinite(v) and OPS[op](v, float(thr)))
            blocking = [k for k, v in legs.items() if not v]

            weekday_ok = bool(entry_is_weekday(
                np.array([bar_ms], dtype="int64"), step)[0])
            if not weekday_ok:
                blocking = blocking + ["entry falls on a weekend"]
                legs["entry is Mon-Fri MYT"] = False
            else:
                legs["entry is Mon-Fri MYT"] = True

            entry_ref = float(spot) if spot else float(feat["close"].iloc[i])
            sl, tp = bracket(entry_ref, c.side)

            out.update({
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bar_open_ms": bar_ms,
                "bar_myt": datetime.fromtimestamp(bar_ms / 1000, MYT)
                           .strftime("%Y-%m-%d %H:%M MYT"),
                "legs": legs, "blocking": blocking, "fired": not blocking,
                "readings": {c2.get("feature"): _round(
                    feat[c2["feature"]].to_numpy(dtype="float64")[i], 4)
                    for c2 in c.conditions if c2.get("feature") in feat.columns},
                "reason": ("all legs true" if not blocking
                           else "legs false: " + ", ".join(blocking)),
            })
            if not blocking:
                out.update({"entry_ref": _round(entry_ref, 2),
                            "sl": _round(sl, 2), "tp": _round(tp, 2),
                            "lots": c.lots,
                            "time_stop_ms": bar_ms + TIME_STOP_BARS * step})
                self._record_event(out)
            else:
                out.update({"entry_ref": None, "sl": None, "tp": None,
                            "lots": c.lots, "time_stop_ms": None})
        except Exception as exc:
            log.debug("signal4 evaluation failed: %s", exc)
            out["reason"] = f"evaluation failed: {type(exc).__name__}: {exc}"
        return out

    def _record_event(self, out: dict) -> None:
        key = (out["symbol"], out["bar_open_ms"])
        if key in self._emitted:
            return
        self._emitted.add(key)
        self._events.append({
            "id": f"s4:{out['symbol']}@{out['bar_open_ms']}",
            "name": "signal4", "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"], "bar_open_ms": out["bar_open_ms"],
            "bar_myt": out["bar_myt"], "entry_ref": out["entry_ref"],
            "sl": out["sl"], "tp": out["tp"], "lots": out["lots"],
            "time_stop_ms": out["time_stop_ms"], "bracket": out["bracket"],
            "measured": False, "pattern": out["pattern"],
        })
        log.warning("SIGNAL4 %s %s @ %s  SL %s  TP %s  (weekly refit, NOT measured)",
                    out["symbol"], out["side"].upper(), out["entry_ref"],
                    out["sl"], out["tp"])

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
            log.debug("signal4 could not seed emitted bars: %s", exc)

    def write(self, evaluation: dict) -> bool:
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
            log.debug("signal4 log write failed: %s", exc)
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
