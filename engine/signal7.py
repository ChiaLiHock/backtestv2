"""SIGNAL 7 — the ExpD regime gate, live. EXPERIMENTAL CHANNEL.

Runs the retraining pipeline's winning model (ExpD: ridge logistic P(win)
over the shared feature set + per-regime acceptance thresholds) as a live
channel BESIDE production Signal 6. Nothing about Signal 6 changes; Signal
7 exists to accumulate the forward out-of-sample record in the real-time
environment that the spec §30 "fully OOS" gate requires.

## What it is

* Population: EVERY break event in the session map (not just A-grade) —
  the gate, not a pre-filter, decides.
* Features: `signal6_retrain.features_for_break` — the SAME function the
  model was trained on, so a feature cannot mean something different at
  runtime than it meant in training (the train/serve-skew guard).
* Decision: P(win) >= threshold(regime), thresholds frozen in the model
  artifact at fit time on TRAIN data only.
* Brackets: the fixed baseline (entry at spot when confirmed, SL at
  level∓0.50R, TP at level±1.00R) — identical to Signal 6's geometry.
* Marked EXPERIMENTAL everywhere; its Telegram message says so.

## What it is not

Not measured, not validated on held-out future data, not a replacement
for Signal 6. The model artifact is produced by `tools/fit_signal7.py`
(train: MT5:GOLD full history). Backtested two ways by
`tools/backtest_signal7.py`: in-feed walk-forward 60.6% (104 trades,
MT5), and the honest time-respecting quarterly cross-feed on Bybit —
50.0% / net −64 (the earlier "cross-feed 65.8%" was trained on data
concurrent with its test window and does not count). The forward record
is what decides; if the artifact is missing or stale, Signal 7 says so
and stays quiet rather than guessing.
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

from .signal6_retrain import (_FrameCtx, _fold, features_for_break)
from ..data.db import CandleRepository, Database
from ..indicators.base import IndicatorConfig

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
# The ONE definition of the threshold cap. MT5-optimal thresholds landed
# at 0.65-0.70 and, measured on all three feeds, traded ~2x less for LESS
# net than a flat 0.5 (2026-09-20: MT5 +2196→+4013, PAXG +2209→+3365,
# Bybit −64→+329). Everything that applies the ExpD gate — the fit tool,
# the live artifact, the history walker, the paper gate — reads THIS so
# the displayed history and the live decisions can never disagree.
THRESHOLD_CAP = 0.50
MODEL_PATH = Path(__file__).resolve().parents[1] / "configs" / \
    "signal7_model.json"
# Feature context staleness: rebuilt when the next 5m bar opens (the
# anchor TF), not a fixed hour — a 1-hour cache made the evaluator blind
# to bars that closed within the hour, so signals appeared only after a
# watcher restart. Rebuild cost: ~1-2s, once per 5m bar.
CTX_TTL_MS = 300_000
CTX_TAIL_DAYS = 60


@dataclass(frozen=True)
class Signal7Config:
    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    anchor: str = "5m"
    side: str = "both"
    sl_buffer_of_range: float = 0.50   # the FIXED baseline bracket
    break_tp_of_range: float = 1.0
    lots: float = 0.01
    measured: bool = False

    @property
    def label(self) -> str:
        return ("Regime-gated sweep→break (ExpD logistic P(win)) — "
                "experimental channel")

    @property
    def bracket_label(self) -> str:
        return (f"Break: SL level-{self.sl_buffer_of_range:g}R, TP level+"
                f"{self.break_tp_of_range:g}R · acceptance P(win) >= "
                "per-regime threshold from the model artifact (capped at "
                "0.50 — MT5-optimal thresholds traded 2x less for LESS net "
                "on all three feeds). Capped walk-forward: MT5 227 · 57.7% "
                "· +4013; Bybit quarterly cross-feed 101 · 51.5% · +329. "
                "EXPERIMENTAL; carries none of the measured rule's "
                "evidence.")


class _Model:
    """The fitted artifact: standardized ridge logistic + thresholds."""

    def __init__(self, blob: dict) -> None:
        self.keys: list[str] = list(blob["keys"])
        self.w = np.array(blob["w"], dtype="float64")
        self.mu = np.array(blob["mu"], dtype="float64")
        self.sd = np.array(blob["sd"], dtype="float64")
        self.thresholds: dict[str, float] = dict(blob["thresholds"])
        self.trained_at: int = int(blob.get("trained_at") or 0)
        self.train_n: int = int(blob.get("train_n") or 0)
        self.train_feed: str = str(blob.get("train_feed") or "?")

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "_Model | None":
        try:
            blob = json.loads(Path(path).read_text(encoding="utf-8"))
            m = cls(blob)
            if len(m.w) == len(m.keys) + 1:
                return m
            log.warning("signal7 model artifact malformed (len mismatch)")
        except FileNotFoundError:
            log.info("signal7 model artifact absent — run "
                     "tools/fit_signal7.py to fit one; channel stays quiet")
        except Exception as exc:
            log.warning("signal7 model artifact unreadable: %s", exc)
        return None

    def predict(self, features: dict) -> float:
        x = self._x(features)
        return float(1.0 / (1.0 + np.exp(-np.clip(
            self.w[0] + x @ self.w[1:], -30, 30))))

    def predict_contrib(self, features: dict):
        """(p_win, [(key, contribution), ...]) — the model's "conditions".

        Each contribution is that feature's standardized value times its
        weight: positive pushed P up, negative pulled it down, and the
        order by |contribution| is which condition actually decided. A
        model gate with no visible conditions is a black box; this is the
        box opened, with the same numbers the decision used.
        """
        z = self._x(features)
        contrib = z * self.w[1:]
        p = float(1.0 / (1.0 + np.exp(-np.clip(
            self.w[0] + float(contrib.sum()), -30, 30))))
        order = np.argsort(-np.abs(contrib))
        return p, [(self.keys[i], round(float(contrib[i]), 3))
                   for i in order[:8]]

    def _x(self, features: dict) -> np.ndarray:
        x = np.array([float(features.get(k) if features.get(k) is not None
                            else 0.0) for k in self.keys])
        x[~np.isfinite(x)] = 0.0
        return (x - self.mu) / self.sd

    def threshold_for(self, features: dict) -> float:
        return float(self.thresholds.get(
            "trend" if (features.get("adx_1h") or 0) >= 25 else "range",
            0.5))


def _round(v: Any, dp: int = 2) -> Any:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, dp)


class Signal7:
    """Evaluates the ExpD gate on fresh session maps. Never raises."""

    def __init__(self, log_path: Path, cfg: Signal7Config | None = None,
                 model_path: Path | None = None) -> None:
        self.cfg = cfg or Signal7Config()
        self.log_path = Path(log_path)
        self.model = _Model.load(model_path or MODEL_PATH)
        self._events: list[dict] = []
        self._emitted: set[tuple[str, int, str]] = set()
        self._seeded = False
        self._ctx: _FrameCtx | None = None
        self._ctx_path: np.ndarray | None = None
        self._ctx_built_at = 0

    def _frame(self, db: Database):
        """(ctx, path_arrays), rebuilt at most once per CTX_TTL."""
        now = int(time.time() * 1000)
        if (self._ctx is not None
                and now - self._ctx_built_at < CTX_TTL_MS):
            return self._ctx, self._ctx_path
        repo = CandleRepository(db)
        tail = repo.load_tail(self.cfg.symbol, "1m",
                              CTX_TAIL_DAYS * 1440).reset_index(drop=True)
        if tail.empty:
            return None, None
        f5 = _fold(tail, 300_000)
        f1h = _fold(tail, 3_600_000)
        self._ctx = _FrameCtx(f5, f1h, IndicatorConfig())
        self._ctx_path = tail
        self._ctx_built_at = now
        return self._ctx, self._ctx_path

    def _range_pct(self, path: pd.DataFrame, day0: int) -> float:
        """Today's Asia range percentile among the prior 20 days (from the
        tail's own bars — the same window stat the trainer used)."""
        t = path["open_time"].to_numpy(dtype="int64")
        h = path["high"].to_numpy(dtype="float64")
        l = path["low"].to_numpy(dtype="float64")
        offset = 8 * 3_600_000
        days = ((t + offset) // 86_400_000)
        today = (day0 + offset) // 86_400_000
        lv = path.assign(d=days)
        ranges = []
        for d in range(int(today) - 20, int(today)):
            m = (days == d) & (((t // 3_600_000 + 8) % 24) >= 7) & \
                (((t // 3_600_000 + 8) % 24) < 15)
            if m.any():
                ranges.append(float(h[m].max() - l[m].min()))
        hi, lo = None, None
        m0 = (days == today) & (((t // 3_600_000 + 8) % 24) >= 7) & \
            (((t // 3_600_000 + 8) % 24) < 15)
        if not m0.any() or not ranges:
            return float("nan")
        r_today = float(h[m0].max() - l[m0].min())
        return float(np.count_nonzero(np.array(ranges) < r_today)) \
            / len(ranges)

    def evaluate(self, db: Database, block: dict | None,
                 spot: float | None = None) -> dict:
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "name": "signal7",
            "symbol": c.symbol, "broker_symbol": c.broker_symbol,
            "anchor": c.anchor, "side": c.side,
            "pattern": c.label, "bracket": c.bracket_label,
            "measured": c.measured, "fired": False, "legs": {},
            "blocking": [],
        }
        if self.model is None:
            out["reason"] = ("model artifact missing — run "
                             "tools/fit_signal7.py")
            return out
        if not isinstance(block, dict) or block.get("error"):
            out["reason"] = "no session map available"
            return out
        if block.get("symbol") and block["symbol"] != c.symbol:
            out["reason"] = f"session map is for {block['symbol']}"
            return out
        lv = block.get("levels") or {}
        hi, lo = lv.get("asia_high"), lv.get("asia_low")
        if hi is None or lo is None:
            out["reason"] = "waiting for an Asia range"
            return out

        frame, path = self._frame(db)
        if frame is None or path is None:
            out["reason"] = "no recent candles for features"
            return out

        out["ts_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        out["bar_myt"] = block.get("as_of_myt", "")
        out["day_myt"] = block.get("day_myt", "")

        day0 = block.get("_day0_ms")
        if day0 is None:
            from .session_map import myt_day_ms
            day0 = myt_day_ms(int(block.get("as_of_ms") or 0))
        rp = self._range_pct(path, int(day0))
        pt = path["open_time"].to_numpy(dtype="int64")
        prev_day_mask = (pt >= int(day0) - 86_400_000) & (pt < int(day0))
        dir_ref = float(path["open"].to_numpy()[
            int(np.argmax(prev_day_mask))]) if prev_day_mask.any() else \
            float(lv.get("prev_day_high") or hi)

        fired = None
        best = None
        for ev in block.get("events") or []:
            if not str(ev.get("type") or "").startswith("break_"):
                continue
            got = features_for_break(frame, ev, lv,
                                     float(hi) - float(lo), rp,
                                     int(day0), dir_ref,
                                     block.get("events") or [])
            if got is None:
                continue
            feat, aux = got
            key = (c.symbol, aux["confirm"], str(ev.get("type")))
            if key in self._emitted:
                continue
            p = self.model.predict(feat)
            thr = self.model.threshold_for(feat)
            row = {"event": ev, "feat": feat, "aux": aux,
                   "p_win": p, "thr": thr, "key": key}
            if best is None or p > best["p_win"]:
                best = row
            if p >= thr:
                fired = row
                break
        return self._finish(out, fired, best, spot)

    def _finish(self, out: dict, fired: dict | None, best: dict | None,
                spot: float | None) -> dict:
        c = self.cfg
        # Even a rejection carries its conditions: the dashboard shows WHY
        # the gate said no (best P vs its threshold, and which features
        # decided), exactly as the firing case shows why it said yes.
        if fired is None:
            out["legs"] = {"model loaded": True,
                           "P(win) >= regime threshold": False}
            out["blocking"] = ["P(win) >= regime threshold"]
            if best is None:
                out["reason"] = ("no break event to judge right now")
            else:
                _, contribs = self.model.predict_contrib(best["feat"])
                regime = "trend" if (best["feat"].get("adx_1h")
                                     or 0) >= 25 else "range"
                out.update({
                    "event_type": best["event"].get("type"),
                    "p_win": _round(best["p_win"], 3),
                    "threshold": _round(best["thr"], 3),
                    "regime": regime,
                    "bar_open_ms": best["aux"]["confirm"],
                    "bar_myt": best["event"].get("ts_myt", ""),
                    "contributions": contribs,
                    "reason": (f"gate rejecting: best P(win)="
                               f"{best['p_win']:.2f} < "
                               f"{best['thr']:.2f} "
                               f"({regime} threshold)"),
                })
            return out
        ev, feat, aux = fired["event"], fired["feat"], fired["aux"]
        r = aux["r"]
        sl = (aux["level"] - c.sl_buffer_of_range * r) if aux["is_long"] \
            else (aux["level"] + c.sl_buffer_of_range * r)
        tp = (aux["level"] + c.break_tp_of_range * r) if aux["is_long"] \
            else (aux["level"] - c.break_tp_of_range * r)
        entry_ref = float(spot) if spot else aux["price"]
        _, contribs = self.model.predict_contrib(feat)
        out.update({
            "fired": True,
            "side": "long" if aux["is_long"] else "short",
            "legs": {"model loaded": True,
                     "P(win) >= regime threshold": True},
            "blocking": [],
            "event_type": ev.get("type"),
            "p_win": _round(fired["p_win"], 3),
            "threshold": _round(fired["thr"], 3),
            "regime": "trend" if (feat.get("adx_1h") or 0) >= 25 else "range",
            "event_ts_ms": int(ev.get("ts_ms") or aux["confirm"]),
            "bar_open_ms": aux["confirm"],
            "bar_myt": ev.get("ts_myt", ""),
            "entry_ref": _round(entry_ref), "sl": _round(sl),
            "tp": _round(tp), "lots": c.lots,
            "asia_range": _round(r),
            "contributions": contribs,
            "reason": (f"P(win)={fired['p_win']:.2f} >= "
                       f"{fired['thr']:.2f} ({out.get('regime')})"),
        })
        self._record_event(out)
        return out

    def _record_event(self, out: dict) -> None:
        key = (out["symbol"], int(out["bar_open_ms"]),
               str(out["event_type"]))
        if key in self._emitted:
            out["fired"] = False
            return
        self._emitted.add(key)
        self._events.append({
            "id": f"s7:{out['symbol']}@{out['bar_open_ms']}:"
                  f"{out['event_type']}",
            "name": "signal7", "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"], "bar_open_ms": out["bar_open_ms"],
            "bar_myt": out["bar_myt"], "entry_ref": out["entry_ref"],
            "sl": out["sl"], "tp": out["tp"], "lots": out["lots"],
            "bracket": out["bracket"], "measured": False,
            "pattern": out["pattern"], "event_type": out["event_type"],
            "p_win": out["p_win"], "threshold": out["threshold"],
            "regime": out["regime"],
        })
        log.warning("SIGNAL7 %s %s @ %s  SL %s  TP %s  (P=%.2f>=%.2f %s, "
                    "EXPERIMENTAL)", out["symbol"], out["side"].upper(),
                    out["entry_ref"], out["sl"], out["tp"],
                    out["p_win"], out["threshold"], out["regime"])

    def events(self) -> list[dict]:
        return list(self._events)

    def seed_from_log(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        try:
            for row in _read_log(self.log_path):
                if row.get("fired") and row.get("bar_open_ms") is not None:
                    self._emitted.add((row.get("symbol", ""),
                                       int(row["bar_open_ms"]),
                                       str(row.get("event_type") or "")))
        except Exception as exc:
            log.debug("signal7 could not seed emitted events: %s", exc)

    def write(self, evaluation: dict) -> bool:
        if not evaluation.get("fired"):
            return False
        bar = evaluation.get("bar_open_ms")
        if bar is None:
            return False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            for row in _read_log(self.log_path):
                if (row.get("symbol") == evaluation.get("symbol")
                        and int(row.get("bar_open_ms", -1)) == int(bar)
                        and row.get("event_type")
                        == evaluation.get("event_type")):
                    return False
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evaluation, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False)
                        + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            log.debug("signal7 log write failed: %s", exc)
            return False


def _read_log(path: Path) -> list[dict]:
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
