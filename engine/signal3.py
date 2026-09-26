"""SIGNAL 3 v2 â€” the double-sweep/fake-break reversal, 15m, with ExpD fallback.

Complete rewrite of the old mined dip-buy pattern. The owner's framework:

## Primary patterns (fully symmetric, 15m grid)

**LONG â€” è¿žç»­æ‰«ç©ºï¼ˆæ— é‡ï¼‰åˆ‡å›žè°ƒä¹°ä¸Š**:
A swing LOW is swept twice on LOW volume and reclaimed:

    swing low â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
             â†“ â‘  break (no volume)   â†“ â‘¢ break again (no volume)
                â†‘ â‘¡ reclaim             â†‘ â‘£ reclaim = BUY LONG

**SHORT â€” çªç ´é˜»åŠ›+æ— é‡+è·Œå›žåŒºé—´ï¼ˆå‡çªç ´åšç©ºï¼‰**:
A swing HIGH is broken twice on LOW volume and falls back:

    swing high â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
              â†‘ â‘  break (no volume)  â†‘ â‘¢ break again (no volume)
                 â†“ â‘¡ fall back          â†“ â‘£ fall back = BUY SHORT

Both are liquidity traps: someone pushed through a level without the
volume to hold it, got rejected, tried again, got rejected again.
The second rejection is the trade.

## Exit / flip logic

After entry, å†²é«˜å›žè½ (or å†²ä½Žå›žå‡ for shorts):
* Does NOT break the swept zone â†’ HOLD (the level is defended)
* BREAKS the swept zone â†’ CLOSE + FLIP (the defence failed)

## Fallback: Signal 7's ExpD gate (5m)

When no sweep/fake-break is active, every Asia-range break event is
judged by the Signal 7 logistic P(win) model.

## Not measured

Neither the sweep patterns nor the combination with the fallback has any
walk-forward evidence. The panel says so. Tested on the walker after
this ships.
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

from .signal6_retrain import _FrameCtx, _fold, features_for_break
from ..data.db import CandleRepository, Database
from ..indicators.base import IndicatorConfig

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))
SCHEMA_VERSION = 2

# --- swing detection (15m fractal) --------------------------------------
SWING_LOOKBACK = 3        # bars each side for a fractal swing point
SWING_TAIL_BARS = 80      # how many 15m bars back to search (20 hours)

# --- sweep/fake-break detection -----------------------------------------
QUIET_VOL_RATIO_MAX = 0.8    # sweep bars must be QUIET: vol < 0.8Ã— median
VOL_MEDIAN_BARS = 20         # trailing volume median window
MIN_BREAKS = 2               # need at least 2 breaks + 2 returns

# --- zone-width floor (backtest-validated 2026-09-20) --------------------
# HARD floor in dollars: a zone narrower than the $8 bar-range threshold
# cannot be a meaningful liquidity sweep â€” it produces SL of $1-3 and TP
# of $3-8, which is spread noise, not structure. With this floor, no
# trade should ever have SL < ~$4 or TP < ~$8.
MIN_ZONE_WIDTH_USD = 8.0
# Relative floor as backup (measured: bottom quartile = 46-49%).
MIN_ZONE_WIDTH_OF_MEDIAN = 0.5

# --- research switches (defaults reproduce the legacy behaviour) ---------
# EXTREME_TRACK_FULL: the legacy machine records the sweep extreme only on
# the bar that OPENS a break. Later bars still beyond the level, and the
# return bar's own wick, are ignored -> zone width is under-measured and
# the SL sits too close. True = track the real excursion.
EXTREME_TRACK_FULL = False
# INVALIDATE_ON_LOUD_BREAK: the legacy machine silently ignores a
# high-volume close beyond the level, and a following quiet bar can still
# be counted as a "quiet break" (a real breakout gets traded as a sweep).
# True = a close beyond the level on volume >= LOUD_VOL_RATIO x the
# rolling median abandons the swing.
INVALIDATE_ON_LOUD_BREAK = False
LOUD_VOL_RATIO = 1.5
# Max age (bars) of the return bar for a live fire. The backtest walker
# enters right after the return bar closes; a larger lag lets live fire
# later, at a different price, than anything the backtest measured.
MAX_ENTRY_LAG_BARS = 12

# --- brackets ------------------------------------------------------------
SL_ZONE_MULT = 1.0         # SL beyond the extreme by 1Ã— zone width
# Owner's rule (2026-09-23): the target must be at least half the stop
# distance (rr = TP dist / SL dist >= 0.5). Measured on all three markets
# â€” the rr<0.5 tail is the only slice that loses money net; a 1:1 cut
# removed far too many profitable high-win-rate trades. rr>=0.5 keeps
# ~68% of trades and lifts PF on every symbol (XAU 1.49â†’1.79, MT5
# 1.59â†’1.74, PAXG 1.70â†’1.94).
MIN_RR = 0.5
# rr ceiling (owner's rule 2026-09-26): the TP override can latch onto a
# distant opposing swing (measured live: TP $200 on a $32 stop, rr 6.3) —
# a target price will not reach inside a day for structural reasons that
# have nothing to do with the pattern. Cap the payout at MAX_RR x the
# stop distance; anything beyond is truncated to the cap, never skipped
# (the swing is still valid structure, it is just too far to be the goal).
MAX_RR = 3.0
# TP confined to today's Asia range (owner's rule 2026-09-26): the Asia
# session (07:00-15:00 MYT) is the day's anchor — a TP placed beyond its
# high/low is betting on a breakout that usually does not come inside a
# day. Measured on all three feeds (TP beyond Asia H/L): XAU 33.3% win /
# net -9 vs 90.6% / +1498 inside; MT5 9.5% / -450 vs 83.5% / +1218;
# PAXG 51.4% / +7 vs 79.9% / +3051. Generalising to "previous session's
# H/L" (US->Asia, Europe->US) was tested and is NOT the same rule — only
# the Asia anchor is consistent across feeds. The TP is clamped to sit
# INSIDE the range (never beyond it); if the clamp kills the rr floor the
# trade is skipped, not taken at a payout the day's structure forbids.
TP_CLAMP_TO_ASIA = True

# --- adaptive timeframe (owner's rule 2026-09-21, revised) ----------------
# Start at 5m. If the current bar's high-low range is under $8, escalate
# to the next rung. If range >= $8 at the current rung, stay (or drop
# back). Ladder: 5â†’15â†’20â†’25â†’30â†’35m.
MIN_RANGE_USD = 8.0
TF_LADDER_MS = (300_000, 900_000, 1_200_000, 1_500_000,
                1_800_000, 2_100_000)                       # 5,15,20,25,30,35m
CONSEC_BARS_TO_DOWNGRADE = 2

# --- 15m fold ------------------------------------------------------------
FIFTEEN_MS = 900_000
MINUTE_MS = 60_000


def asia_range_at(t1: np.ndarray, h1: np.ndarray, l1: np.ndarray,
                  day0_ms: int) -> tuple[float, float] | None:
    """Today's Asia session (07:00-15:00 MYT) high/low as of the 1m tail.

    One definition shared by the live evaluator and the backtest walker:
    `day0_ms` is the MYT day anchor (the session map's `_day0_ms` live, the
    entry bar's day in the walk). Returns None when the session has not
    produced bars yet — the caller then leaves the TP unclamped rather
    than guessing a range.
    """
    offset = 8 * 3_600_000
    days = (t1 + offset) // 86_400_000
    hours = ((t1 // 3_600_000 + 8) % 24)
    m = (days == ((day0_ms + offset) // 86_400_000)) \
        & (hours >= 7) & (hours < 15)
    if not m.any():
        return None
    return float(h1[m].max()), float(l1[m].min())


def _closed_fresh(minute: pd.DataFrame | None, now_ms: int) -> pd.DataFrame:
    """Closed rows of a live 1m pull, cast for appending to the repo tail.

    The sweep lives on CLOSED bars only (the forming minute never produces
    a fire), and `_frame` reads the database, which the slow cycle refreshes
    every `interval_s`. Passing the fast loop's live 1m pull lets the frame
    see a bar the moment it closes instead of at the next DB sync â€” that is
    the difference between a notification 5 seconds after the sweep bar and
    one minutes later.
    """
    if minute is None or minute.empty:
        return pd.DataFrame()
    df = minute[["open_time", "open", "high", "low", "close", "volume"]].copy()
    for col in ("open_time",):
        df[col] = df[col].astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype("float64")
    return df[df["open_time"] + MINUTE_MS <= now_ms]


def detect_swings(high: np.ndarray, low: np.ndarray, n: int = SWING_LOOKBACK
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Fractal swing points: a bar whose high/low is extreme vs n bars
    either side. Returns (swing_high_indices, swing_low_indices)."""
    count = len(high)
    sh = []
    sl_ = []
    for i in range(n, count - n):
        if high[i] == high[i - n:i + n + 1].max():
            sh.append(i)
        if low[i] == low[i - n:i + n + 1].min():
            sl_.append(i)
    return np.array(sh, dtype="int64"), np.array(sl_, dtype="int64")


@dataclass
class SweepState:
    """Tracks repeated quiet breaks of one swing level (either side).

    For a swing LOW (sweep pattern): breaks go BELOW, returns come back
    ABOVE. For a swing HIGH (fake-break pattern): breaks go ABOVE, returns
    come back BELOW. Same machine, opposite directions.
    """
    swing_price: float          # the swing level being tested
    side: str = "low"           # "low" = sweep/long, "high" = fake-break/short
    break_count: int = 0        # how many times it's been broken + returned
    extreme: float = 0.0        # furthest point reached across all breaks
    last_break_bar: int = -1
    returned: bool = True       # False while price is beyond the level

    @property
    def ready(self) -> bool:
        """Double break + currently returned = trade signal."""
        return self.break_count >= MIN_BREAKS and self.returned

    @property
    def zone_width(self) -> float:
        return max(abs(self.swing_price - self.extreme), 1e-9)


def nearest_opposing_swing(entry: float, is_long: bool,
                           swings: np.ndarray, hi: np.ndarray,
                           lo: np.ndarray) -> float | None:
    """The CLOSEST opposing swing price beyond entry, or None.

    Only swings the walker/live evaluator could already see are passed in
    (the caller filters by index â‰¤ entry bar â€” a swing that forms after
    the entry is lookahead and was the old bug). Among those visible, the
    nearest in PRICE is the real target; the old loop took the OLDEST
    (lowest index), which could sit far away and made the TP spread
    uncontrolled.
    """
    best: float | None = None
    for si in swings:
        px = float(hi[int(si)] if is_long else lo[int(si)])
        if is_long and px > entry:
            if best is None or px < best:
                best = px
        elif not is_long and px < entry:
            if best is None or px > best:
                best = px
    return best


def sweep_bracket(state: "SweepState", entry: float) -> tuple[float, float]:
    """(sl, tp) for a ready sweep state â€” one definition, shared by the
    live evaluator and the backtest walker so the two can never disagree.

    TP: nearest opposing swing beyond entry; fallback swing Â± 3Ã— zone.
    SL: beyond the extreme by SL_ZONE_MULT Ã— zone width.
    """
    zone_w = state.zone_width
    is_long = state.side == "low"
    tp = (state.swing_price + 3 * zone_w) if is_long \
        else (state.swing_price - 3 * zone_w)
    sl = (state.extreme - SL_ZONE_MULT * zone_w) if is_long \
        else (state.extreme + SL_ZONE_MULT * zone_w)
    return sl, tp


def run_state_machine(swing_price: float, side: str,
                      h: np.ndarray, l: np.ndarray, c: np.ndarray,
                      v: np.ndarray, start: int, end: int,
                      vol_med: float | None = None):
    """Run the break/return state machine over one swing level.

    Breaks and returns are CLOSE-based (mutually exclusive per bar):
    for a swing LOW, a break bar closes BELOW and a return bar closes
    AT or ABOVE. The wick determines the extreme. Quiet = the break
    bar's volume is below the trailing median Ã— ratio â€” a ROLLING
    per-bar median by default (the reference volume drifts with the
    session; one median frozen at the last bar judged breaks from 80
    bars earlier against the wrong yardstick).

    Returns (SweepState, last_return_bar, filter_reason) where
    filter_reason is None for a pass, or a string naming WHICH gate
    rejected it â€” so the panel can say "sweep detected but zone $5.2
    < $8 floor" instead of silently hiding the signal.

    Module-level on purpose: the backtest walker calls THIS function, so
    the walk and the live loop are one machine, not two that look alike.
    Note it fires ONCE per swing (returns at the first ready return) â€”
    the old walker re-fired the same swing, which is where its
    break_count=3/4 buckets came from; live never does that.
    """
    state = SweepState(swing_price=swing_price, side=side)
    last_return_bar = -1
    for j in range(start, min(start + SWING_TAIL_BARS, end)):
        close = float(c[j])

        if side == "low":
            breaking = close < swing_price
            returning = close >= swing_price
        else:
            breaking = close > swing_price
            returning = close <= swing_price

        if breaking and INVALIDATE_ON_LOUD_BREAK:
            ref0 = vol_med
            if ref0 is None:
                ref0 = float(np.median(
                    v[max(0, j - VOL_MEDIAN_BARS):j])) \
                    if j > VOL_MEDIAN_BARS else 0.0
            if ref0 > 0 and float(v[j]) >= ref0 * LOUD_VOL_RATIO:
                return None     # the level really broke on volume
        if EXTREME_TRACK_FULL and not state.returned:
            # still beyond the level, or the return bar itself: its wick
            # is part of the sweep excursion
            if side == "low":
                state.extreme = min(state.extreme, float(l[j]))
            else:
                state.extreme = max(state.extreme, float(h[j]))

        if breaking and state.returned:
            vol = float(v[j])
            ref = vol_med
            if ref is None:   # rolling trailing median at this bar
                ref = float(np.median(
                    v[max(0, j - VOL_MEDIAN_BARS):j])) \
                    if j > VOL_MEDIAN_BARS else 0.0
            quiet = vol < ref * QUIET_VOL_RATIO_MAX if ref > 0 else False
            if quiet:
                state.break_count += 1
                state.returned = False
                state.last_break_bar = j
                if side == "low":
                    state.extreme = min(state.extreme or swing_price,
                                        float(l[j]))
                else:
                    state.extreme = max(state.extreme or swing_price,
                                        float(h[j]))
        elif returning and not state.returned:
            state.returned = True
            last_return_bar = j
            if state.ready:
                # Zone-width floors â€” with the reason stated.
                if state.zone_width < MIN_ZONE_WIDTH_USD:
                    return (state, last_return_bar,
                            f"zone ${state.zone_width:.1f} < "
                            f"${MIN_ZONE_WIDTH_USD:.0f} hard floor")
                local_scale = abs(
                    swing_price - float(np.median(
                        l[j - VOL_MEDIAN_BARS:j]
                        if side == "low"
                        else h[j - VOL_MEDIAN_BARS:j]))) \
                    if j >= VOL_MEDIAN_BARS else 0.0
                if local_scale > 0 and state.zone_width \
                        < MIN_ZONE_WIDTH_OF_MEDIAN * local_scale:
                    return (state, last_return_bar,
                            f"zone ${state.zone_width:.1f} < "
                            f"{MIN_ZONE_WIDTH_OF_MEDIAN:.0%} of local "
                            f"scale ${local_scale:.1f}")
                return state, last_return_bar, None
    return None


class Signal3:
    """The double-sweep/fake-break reversal (15m) + ExpD fallback."""

    def __init__(self, log_path: Path, signal7=None,
                 cfg: "Signal3Config | None" = None) -> None:
        from .signal7 import Signal7
        self.cfg = cfg or Signal3Config()
        self.log_path = Path(log_path)
        self.s7 = signal7 or Signal7(
            log_path=Path(log_path).parent / "signals7.jsonl")
        self._events: list[dict] = []
        self._emitted: set[tuple[str, int, str]] = set()
        self._seeded = False
        self._ctx: _FrameCtx | None = None
        self._ctx_built_at = 0
        self._f15: pd.DataFrame | None = None
        self._tf_level: int = 0          # adaptive TF ladder position
        self._current_tf_ms: int = 900_000  # the TF actually in use

    def _frame(self, db: Database,
               minute: pd.DataFrame | None = None):
        """(ctx, arrays at the ADAPTIVE timeframe). Cached per TF bar close.

        The 1-hour cache was the bug that made signals appear only after
        a restart: the arrays were frozen at build time, so every sweep
        that completed in the following hour was invisible to the live
        evaluator (measured: 10 walker-detected trades on 2026-09-22
        01:00-06:45 with ZERO live fires â€” they appeared only after the
        watcher restarted and rebuilt the frame).

        Now the cache expires when the NEXT bar of the current TF opens.
        Rebuild cost: ~1s (60-day 1m tail fold), once per TF bar.

        A live 1m pull (`minute`) is folded into the tail as well, so a
        frame built between DB syncs still sees the bars that closed since
        the last cycle. The cache is invalidated the moment a brand-new
        closed minute appears, so the sweep fires at the bar's real close
        rather than the next `interval_s` boundary.
        """
        now = int(time.time() * 1000)
        tf_ms = TF_LADDER_MS[getattr(self, "_tf_level", 0)]
        cache_ms = tf_ms  # one full TF bar, not a fixed hour
        fresh = _closed_fresh(minute, now)
        fresh_last = int(fresh["open_time"].max()) if len(fresh) else 0
        if self._ctx is not None and now - self._ctx_built_at < cache_ms \
                and self._f15 is not None \
                and fresh_last <= getattr(self, "_ctx_last_ms", 0):
            return (self._ctx,
                    self._f15["open_time"].to_numpy(dtype="int64"),
                    self._f15["high"].to_numpy(dtype="float64"),
                    self._f15["low"].to_numpy(dtype="float64"),
                    self._f15["close"].to_numpy(dtype="float64"),
                    self._f15["volume"].to_numpy(dtype="float64"))

        repo = CandleRepository(db)
        tail = repo.load_tail(self.cfg.symbol, "1m",
                              60 * 1440).reset_index(drop=True)
        if tail.empty:
            return None, None, None, None, None, None

        if len(fresh):
            last = int(tail["open_time"].iloc[-1])
            add = fresh[fresh["open_time"] > last]
            if len(add):
                tail = pd.concat([tail, add], ignore_index=True)

        # --- pick the adaptive timeframe --------------------------------
        f5 = _fold(tail, 300_000)
        tf_ms = self._pick_timeframe(f5)
        self._current_tf_ms = tf_ms

        f_tf = _fold(f5, tf_ms)
        self._ctx = _FrameCtx(f5, _fold(tail, 3_600_000),
                              IndicatorConfig())
        self._f15 = f_tf  # named _f15 for compat; actual tf may be 25/30/35m
        self._ctx_built_at = now
        self._ctx_last_ms = int(tail["open_time"].iloc[-1])
        # Today's Asia H/L off the same tail the sweep reads — the TP
        # clamp needs the day's anchor and it must come from the same
        # data the rest of the frame uses, not a second pull.
        self._asia_hl = asia_range_at(
            tail["open_time"].to_numpy(dtype="int64"),
            tail["high"].to_numpy(dtype="float64"),
            tail["low"].to_numpy(dtype="float64"),
            self._ctx_last_ms)
        return (self._ctx,
                f_tf["open_time"].to_numpy(dtype="int64"),
                f_tf["high"].to_numpy(dtype="float64"),
                f_tf["low"].to_numpy(dtype="float64"),
                f_tf["close"].to_numpy(dtype="float64"),
                f_tf["volume"].to_numpy(dtype="float64"))

    def _pick_timeframe(self, f5: pd.DataFrame) -> int:
        """Walk the ladder: start at 5m, escalate when the current bar's
        range is under $8, drop back after 2 consecutive wide bars.

        The 5m base means: when 5m bars already have $8+ of range, the
        swing detection stays on the fastest grid (owner: å¦‚æžœ5åˆ†é’Ÿå·®å¤§äºŽ8
        å°±ä¸éœ€ç­‰15). Only when 5m is too quiet does it escalate.
        """
        level = getattr(self, "_tf_level", 0)

        # check the current level's latest bar
        tf_ms = TF_LADDER_MS[level]
        bars = _fold(f5, tf_ms)
        if bars.empty:
            return tf_ms
        h = bars["high"].to_numpy(dtype="float64")
        l = bars["low"].to_numpy(dtype="float64")
        n = len(bars)

        # downgrade: 2 consecutive bars with range >= $8 â†’ drop one rung
        # (or all the way to 5m if already fast enough)
        if n >= CONSEC_BARS_TO_DOWNGRADE and level > 0:
            last_two_ok = all(
                h[n - 1 - i] - l[n - 1 - i] >= MIN_RANGE_USD
                for i in range(CONSEC_BARS_TO_DOWNGRADE))
            if last_two_ok:
                self._tf_level = max(0, level - 1)
                self._consec_ok = 0
                return TF_LADDER_MS[self._tf_level]

        # upgrade: current bar too quiet â†’ escalate one rung at a time
        latest_range = h[n - 1] - l[n - 1]
        if latest_range < MIN_RANGE_USD and level < len(TF_LADDER_MS) - 1:
            # try the next rung; if that bar is also quiet, keep going
            for next_level in range(level + 1, len(TF_LADDER_MS)):
                nb = _fold(f5, TF_LADDER_MS[next_level])
                if nb.empty:
                    break
                nh = nb["high"].to_numpy(dtype="float64")
                nl = nb["low"].to_numpy(dtype="float64")
                if nh[-1] - nl[-1] >= MIN_RANGE_USD:
                    self._tf_level = next_level
                    return TF_LADDER_MS[next_level]
            # none met the bar â€” stay at the highest tried
            self._tf_level = len(TF_LADDER_MS) - 1
            return TF_LADDER_MS[-1]

        self._tf_level = level
        return tf_ms

    def evaluate(self, db: Database, block: dict | None,
                 spot: float | None = None,
                 minute: pd.DataFrame | None = None) -> dict:
        """Two-tier: sweep/fake-break pattern first, then ExpD gate.

        `minute` is the fast loop's live 1m pull; it lets the sweep frame
        reach bars that closed since the last DB sync, so a fire lands on
        the bar's real close instead of the next cycle boundary.
        """
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "name": "signal3",
            "symbol": c.symbol, "broker_symbol": c.broker_symbol,
            "anchor": "15m", "side": "both",
            "pattern": c.label, "bracket": c.bracket_label,
            "measured": False, "fired": False, "legs": {}, "blocking": [],
            "mode": None,
        }
        if not isinstance(block, dict) or block.get("error"):
            out["reason"] = "no session map available"
            return out
        if block.get("symbol") and block["symbol"] != c.symbol:
            out["reason"] = f"session map is for {block['symbol']}"
            return out

        out["ts_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        out["bar_myt"] = block.get("as_of_myt", "")
        out["day_myt"] = block.get("day_myt", "")

        # --- try the sweep/fake-break pattern (primary, adaptive TF) -----
        sweep_rejected = None
        if db is not None:
            sweep_result = self._check_sweep(db, block, spot, minute=minute)
            if sweep_result is not None and sweep_result.get("fired"):
                out.update(sweep_result)
                out["mode"] = "sweep"
                self._record_event(out)
                return out
            if sweep_result is not None:
                # detected but filtered â€” carry the reason forward so the
                # panel can say WHY, instead of silently hiding the signal
                sweep_rejected = sweep_result

        # --- fallback: Signal 7's ExpD gate (5m) ---------------------------
        lv = block.get("levels") or {}
        if lv.get("asia_high") is not None and self.s7 is not None:
            s7_out = self.s7.evaluate(db, block, spot=spot)
            if s7_out.get("fired"):
                out.update({
                    "fired": True, "mode": "expd",
                    "side": s7_out["side"],
                    # The sweep leg is not a âœ— here â€” the fallback fired on
                    # its own gate, so the sweep pattern was simply not the
                    # path this bar took. A truthy string keeps the panel's
                    # "fired == every leg passed" contract intact and reads
                    # as N/A rather than as a failed condition.
                    "legs": {"sweep pattern": "not required â€” fallback fired",
                             "ExpD gate": True},
                    "blocking": [],
                    "event_type": s7_out.get("event_type"),
                    "entry_ref": s7_out.get("entry_ref"),
                    "sl": s7_out.get("sl"), "tp": s7_out.get("tp"),
                    "bar_open_ms": s7_out.get("bar_open_ms"),
                    "lots": c.lots,
                    "p_win": s7_out.get("p_win"),
                    "threshold": s7_out.get("threshold"),
                    "regime": s7_out.get("regime"),
                    "reason": f"ExpD fallback: {s7_out.get('reason', '')}",
                })
                self._record_event(out)
                return out
            else:
                out.update({
                    "fired": False, "mode": "expd",
                    "legs": {"sweep pattern": False, "ExpD gate": False},
                    "blocking": ["sweep pattern", "ExpD gate"],
                    "reason": f"neither: {s7_out.get('reason', 'quiet')}",
                })
                # If the sweep was detected but filtered, say so â€” a
                # signal hidden by a filter is information, not silence.
                if sweep_rejected is not None:
                    out["sweep_filtered"] = sweep_rejected.get("filtered_by")
                    out["sweep_detail"] = {
                        "event_type": sweep_rejected.get("event_type"),
                        "side": sweep_rejected.get("side"),
                        "swing_level": sweep_rejected.get("swing_level"),
                        "zone_width": sweep_rejected.get("zone_width"),
                        "break_count": sweep_rejected.get("break_count"),
                        "adaptive_tf": sweep_rejected.get("adaptive_tf"),
                    }
                    out["reason"] = (f"sweep filtered: "
                                     f"{sweep_rejected.get('filtered_by')}"
                                     f"; ExpD: "
                                     f"{s7_out.get('reason', 'quiet')}")
                if s7_out.get("contributions"):
                    out["contributions"] = s7_out["contributions"]
                    out["p_win"] = s7_out.get("p_win")
                    out["threshold"] = s7_out.get("threshold")
                    out["regime"] = s7_out.get("regime")
        else:
            out["reason"] = "no Asia range yet"
            out["legs"] = {"sweep pattern": False, "ExpD gate": False}
            out["blocking"] = ["no Asia range"]
        return out

    def _check_sweep(self, db: Database, block: dict, spot: float | None,
                     minute: pd.DataFrame | None = None
                     ) -> dict | None:
        """Both symmetric patterns on the ADAPTIVE timeframe grid."""
        result = self._frame(db, minute=minute)
        if result[0] is None or result[1] is None:
            return None
        _ctx, t15, h15, l15, c15, v15 = result
        # The fold includes the FORMING last bar (the live 1m tail folds
        # into an unfinished tf bar). A "return" judged on that bar's
        # mid-bar close can un-happen before the bar ends — the backtest
        # only ever sees CLOSED bars, so the machine must stop one short
        # of the forming bar or the two are not the same strategy.
        last_closed = len(t15) - 2
        now_bar = last_closed
        if now_bar < SWING_TAIL_BARS:
            return None

        sh_idx, sl_idx = detect_swings(
            h15[max(0, now_bar - SWING_TAIL_BARS):],
            l15[max(0, now_bar - SWING_TAIL_BARS):])

        best: dict | None = None
        rejected: dict | None = None
        lo_i = max(0, now_bar - SWING_TAIL_BARS)
        tf_label = f"{self._current_tf_ms // 60_000}m"

        # --- check swing LOWs for the sweep/long pattern -----------------
        for si in sl_idx:
            abs_i = int(si) + lo_i
            if abs_i > now_bar - 12:
                continue
            r = self._run_state_machine(
                swing_price=float(l15[abs_i]), side="low",
                h=h15, l=l15, c=c15, v=v15,
                start=abs_i + SWING_LOOKBACK, end=now_bar + 1,)
            if r is None:
                continue
            state, j, reason = r
            if state.ready and j >= now_bar - MAX_ENTRY_LAG_BARS:
                if reason is None:
                    cand = self._sweep_signal(
                        state, "long", spot, c15[j], sh_idx, lo_i, h15,
                        l15, t15[j])
                    if cand.get("fired"):
                        best = cand
                        break
                    if rejected is None:
                        rejected = cand
                elif rejected is None:
                    rejected = {
                        "event_type": "double_sweep_low",
                        "side": "long",
                        "swing_level": round(state.swing_price, 2),
                        "zone_width": round(state.zone_width, 2),
                        "break_count": state.break_count,
                        "filtered_by": reason,
                    }

        if best is None:
            # --- check swing HIGHs for the fake-break/short pattern ------
            for hi in sh_idx:
                abs_i = int(hi) + lo_i
                if abs_i > now_bar - 12:
                    continue
                r = self._run_state_machine(
                    swing_price=float(h15[abs_i]), side="high",
                    h=h15, l=l15, c=c15, v=v15,
                    start=abs_i + SWING_LOOKBACK, end=now_bar + 1,)
                if r is None:
                    continue
                state, j, reason = r
                if state.ready and j >= now_bar - MAX_ENTRY_LAG_BARS:
                    if reason is None:
                        cand = self._sweep_signal(
                            state, "short", spot, c15[j], sl_idx, lo_i,
                            h15, l15, t15[j])
                        if cand.get("fired"):
                            best = cand
                            break
                        if rejected is None:
                            rejected = cand
                    elif rejected is None:
                        rejected = {
                            "event_type": "double_sweep_high",
                            "side": "short",
                            "swing_level": round(state.swing_price, 2),
                            "zone_width": round(state.zone_width, 2),
                            "break_count": state.break_count,
                            "filtered_by": reason,
                        }
        if best is not None:
            best["adaptive_tf"] = tf_label
            return best
        if rejected is not None:
            rejected["adaptive_tf"] = tf_label
            rejected["fired"] = False
            rejected["reason"] = (f"sweep detected but filtered: "
                                  f"{rejected['filtered_by']}")
            return rejected
        return None

    def _run_state_machine(self, swing_price: float, side: str,
                           h: np.ndarray, l: np.ndarray, c: np.ndarray,
                           v: np.ndarray, start: int, end: int,
                           vol_med: float | None = None):
        """Thin method wrapper â€” the machine itself is module-level so the
        backtest walker runs the exact same code the live loop does."""
        return run_state_machine(swing_price, side, h, l, c, v, start, end,
                                 vol_med)

    def _sweep_signal(self, state: SweepState, direction: str,
                      spot: float | None, close_price: float,
                      target_swings: np.ndarray, lo_i: int,
                      h: np.ndarray, l: np.ndarray,
                      bar_ms: int) -> dict:
        """Build the fired dict from a ready SweepState."""
        zone_w = state.zone_width
        entry = float(spot) if spot else close_price
        is_long = direction == "long"

        # SL/TP from the one shared bracket definition â€” the backtest
        # walker calls the same function, so the two cannot drift.
        sl, tp = sweep_bracket(state, entry)

        # TP override: the CLOSEST opposing swing beyond entry that was
        # already CONFIRMED by the entry bar (fractal needs SWING_LOOKBACK
        # bars after it). The old loop took the oldest swing and never
        # checked confirmation â€” a target that only formed later is
        # lookahead, and a far-old swing made the payout spread wild.
        confirmed = np.array(
            [si for si in target_swings
             if int(si) + lo_i + SWING_LOOKBACK <= len(h) - 1],
            dtype="int64")
        near = nearest_opposing_swing(entry, is_long, confirmed, h, l)
        if near is not None:
            tp = near

        # rr ceiling: a TP beyond MAX_RR x the stop distance is a swing
        # too far to be a sensible goal — truncated to the cap, not
        # skipped (the pattern is valid; the target is just nearer).
        sl_d0 = abs(entry - sl)
        if abs(tp - entry) > MAX_RR * sl_d0:
            tp = entry + MAX_RR * sl_d0 if is_long else entry - MAX_RR * sl_d0

        # Asia-range clamp: the day's anchor. A TP beyond today's Asia
        # high/low is a breakout bet the session rarely pays (see
        # TP_CLAMP_TO_ASIA note) — pulled INSIDE the range instead. If
        # the entry itself is already beyond the range (a real breakout
        # in progress), the clamp is skipped: that trade is judged by
        # the rr rules alone.
        asia = getattr(self, "_asia_hl", None) if TP_CLAMP_TO_ASIA else None
        asia_filtered = None
        if asia is not None:
            a_hi, a_lo = asia
            if a_lo < entry < a_hi:
                if is_long and tp > a_hi:
                    tp = a_hi
                elif not is_long and tp < a_lo:
                    tp = a_lo
            elif is_long and tp > a_hi:
                asia_filtered = (
                    f"TP ${tp:.1f} beyond Asia high {a_hi:.1f} with entry "
                    f"outside the range — breakout regime, skipped")
            elif not is_long and tp < a_lo:
                asia_filtered = (
                    f"TP ${tp:.1f} beyond Asia low {a_lo:.1f} with entry "
                    f"outside the range — breakout regime, skipped")

        kind = "double_sweep_low" if is_long else "fake_break_high"
        if asia_filtered is not None:
            return {
                "fired": False,
                "side": "long" if is_long else "short",
                "event_type": kind,
                "swing_level": round(state.swing_price, 2),
                "sweep_extreme": round(state.extreme, 2),
                "zone_width": round(zone_w, 2),
                "break_count": state.break_count,
                "entry_ref": round(entry, 2),
                "sl": round(sl, 2), "tp": round(tp, 2),
                "filtered_by": asia_filtered,
            }
        # rr floor: TP must be at least MIN_RR Ã— the stop distance. The
        # trade is skipped with the reason stated, never taken at a
        # worse-than-MIN_RR payout.
        tp_d, sl_d = abs(tp - entry), abs(entry - sl)
        if tp_d < MIN_RR * sl_d:
            return {
                "fired": False,
                "side": "long" if is_long else "short",
                "event_type": kind,
                "swing_level": round(state.swing_price, 2),
                "sweep_extreme": round(state.extreme, 2),
                "zone_width": round(zone_w, 2),
                "break_count": state.break_count,
                "entry_ref": round(entry, 2),
                "sl": round(sl, 2), "tp": round(tp, 2),
                "filtered_by": (
                    f"TP ${tp_d:.1f} < {MIN_RR:g}Ã— SL ${sl_d:.1f} "
                    f"(rr {tp_d / sl_d:.2f} < {MIN_RR:g})"),
            }
        return {
            "fired": True,
            "side": "long" if is_long else "short",
            "legs": {
                f"{'sweep' if is_long else 'fake-break'} "
                f"({'low' if is_long else 'high'})": True,
                "quiet volume": True,
                "break count": state.break_count,
                "returned": True,
            },
            "blocking": [],
            "event_type": kind,
            "swing_level": round(state.swing_price, 2),
            "sweep_extreme": round(state.extreme, 2),
            "zone_width": round(zone_w, 2),
            "entry_ref": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "lots": self.cfg.lots,
            "bar_open_ms": int(bar_ms),
            "reason": (
                f"double quiet {'sweep' if is_long else 'fake-break'} of "
                f"{state.swing_price:.2f} (x{state.break_count}), "
                f"{'reclaimed' if is_long else 'fallen back'} â€” "
                f"{'LONG' if is_long else 'SHORT'}"),
        }

    def _record_event(self, out: dict) -> None:
        if not out.get("fired"):
            return
        key = (out["symbol"], int(out.get("bar_open_ms") or 0),
               str(out.get("event_type") or ""))
        if key in self._emitted:
            out["fired"] = False
            return
        self._emitted.add(key)
        bar_ms = out.get("bar_open_ms")
        # The message time is the ENTRY bar, not the moment the evaluate ran.
        # `out["bar_myt"]` is the block's as-of (wall clock); the sweep carries
        # its own `bar_open_ms`, and a notification on a phone must show the
        # same time the panel shows as the trade's entry.
        bar_myt = (datetime.fromtimestamp(int(bar_ms) / 1000, MYT)
                   .strftime("%Y-%m-%d %H:%M")
                   if bar_ms else out.get("bar_myt", ""))
        self._events.append({
            "id": f"s3:{out['symbol']}@{out.get('bar_open_ms', 0)}:"
                  f"{out.get('event_type', '')}",
            "name": "signal3", "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"],
            "bar_open_ms": out.get("bar_open_ms"),
            "bar_myt": bar_myt,
            "entry_ref": out.get("entry_ref"),
            "sl": out.get("sl"), "tp": out.get("tp"),
            "lots": out.get("lots", self.cfg.lots),
            "bracket": out["bracket"], "measured": False,
            "pattern": out["pattern"], "event_type": out.get("event_type"),
            "mode": out.get("mode"),
            "p_win": out.get("p_win"),
        })
        log.warning("SIGNAL3 %s %s @ %s  SL %s  TP %s  (%s, mode=%s)",
                    out["symbol"], out["side"].upper(),
                    out.get("entry_ref"), out.get("sl"), out.get("tp"),
                    out.get("event_type"), out.get("mode"))

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
            log.debug("signal3 could not seed: %s", exc)

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
                                   ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            log.debug("signal3 log write failed: %s", exc)
            return False


@dataclass(frozen=True)
class Signal3Config:
    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    lots: float = 0.01
    measured: bool = False

    @property
    def label(self) -> str:
        return ("Double quiet sweep/fake-break reversal (15m swings, "
                "symmetric long+short) + ExpD regime gate fallback")

    @property
    def bracket_label(self) -> str:
        return ("Sweep/fake-break: SL beyond the extreme by 1Ã— zone "
                "width, TP nearest opposing swing, skip when TP < "
                f"{MIN_RR:g}Ã— SL (rr floor) Â· å†²é«˜å›žè½ç ´ zone = flip "
                "Â· Fallback: Signal 7 ExpD P(win) gate. UNMEASURED as a "
                "combined system.")


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
