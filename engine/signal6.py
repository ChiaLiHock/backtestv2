"""SIGNAL 6 — the session framework's entries, mechanised. WALKER-VERIFIED.

**This is a port of the owner's own framework, refined against the full 1m
history** (`XAUUSD_黄金日内交易口诀与框架.md`). Unlike the rule (n=588 against
a matched random-entry null) and signal5 (out-of-sample validated), the
geometry below was CHOSEN BY LOOKING AT THIS SAME DATA — the shipped "C3"
was the robust winner of ~30 configurations read, stable across
chronological halves but not an out-of-sample measurement. Every surface
that shows it says so.

## What trades: the 扫盘→突破 sequence, volume confirmed

A break of the Asia level trades only when BOTH hold:

1. **the same side was swept earlier in the day** (扫盘→突破 — the
   liquidity grab before the run, the framework's third layer read
   literally: Sweep → Break → Continuation);
2. two consecutive 5m closes beyond the level AND volume above the trailing
   median (`vol_ok` — 突破有量才看延续).

    break_high -> LONG    SL level-0.50R, TP level+1.0R
    break_low  -> SHORT   SL level+0.50R, TP level-1.0R

Entry is the bar AFTER the second close. The stop sits INSIDE the range at
the broken level less 0.50R of air (回踩不破 is the invalidation; 0.15-0.30R
gets whipsawed by the retest it waits for, and the buffer curve is a plateau
from 0.35R to 0.50R). The target is the measured move, one range beyond.

Filters, framework-faithful and load-bearing in the walker:

* confirmation **15:00-22:00 MYT** — the pattern only exists while the
  sessions that create it are open (欧盘扫流动，美盘定方向);
* a minimum Asia range (0.1% of price) — a degenerate range is noise.

## What does NOT trade: the sweep reversal

v1 traded sweep_high/sweep_low as reversals; the walker rejected it in EVERY
geometry tested (TP mid vs far side, SL at the wick vs the level, buffers
0.15R-0.60R — the sweep shapes stayed net negative throughout, best single
shape ≈ flat). Sweeps remain map events and `session`-channel notifications
— structure worth seeing — and `trade_sweeps=True` puts them back if that
is ever wanted. 扫盘不追单 is the framework's own instruction; the data
agrees with it.

## The out-of-sample check: MT5:GOLD spot, 17 months, rules untouched

The same shipped rules walked over `MT5:GOLD` 5m (2025-03 → 2026-08, a
different feed at a different price, mostly before the tuning window):

    201 trades · 47.8% vs 42.1% break-even · net +1204 · halves 44.0/51.5
    pre-Bybit (fully independent, n=127): 43.3% · +336
    overlap with the tuning window (n=74): 55.4% · +868

Read honestly: the pattern's edge is REAL but THIN — net positive on data
that never influenced its selection, yet ~15pp below the in-sample win
rate. The 62.9% below is inflated by ~30-config selection; the truth to
carry into live expectations is "slightly above break-even, in the same
direction as the tuning data" (2025 44.1% → 2026 52.2%). Caveats: the MT5
walk runs on a 5m path (coarser TP/SL resolution than 1m) and broker tick
volume feeds the volume gate.

## The shipped record (full history, as of 2026-09-20)

**A-grade only, unlimited per day** — the owner asked for at least one trade
every weekday; measured honestly, that is not achievable with quality:

    A only, unlimited (SHIPPED): 105 trades · 62.9% · net +1333.71
        halves 61.5% / 64.2% · Europe 54 · 66.7% · US 47 · 59.6%
    A only, 1/day:               61 · 63.9% · +866   (47% of weekdays)
    + C tier (any break ≥19:00): 65 · 61.5% · +803   (C itself 9 · 33%)
    + B + C (the daily ladder):  85 · 55.3% · +690   (B itself 25 · 32%)
    + D at 20:00:                 5 · 40%   — the early-sweep edge (77.8% on
        no-break days) does not survive entering hours after the sweep.

~25% of weekdays produce no structure to trade at all, and every tier below
A trades under its break-even. The ladder stays as CONFIG
(`daily_ladder=True`) for coverage at the documented cost; the shipped
default takes every A-grade sequence the day offers (≈0.6/day, 1.7 on a
signal day) and nothing else. Selection caveat: ~30 configs were read to
choose this; the mechanism's prior plausibility (it is the 口诀's own
sequence) and the halves-stability are the defences, not proof.

## Entry timing, measured every way that was proposed

`break_entry` selects among three modes, all walked on both datasets:

* **"confirm" (SHIPPED)** — enter the bar after the second volume-confirmed
  close. In-sample 105 · 62.9% · +1334; MT5 OOS 201 · 47.8% · +1204. The
  only mode net-positive in BOTH datasets.
* **"retest"** (回踩不破 at the broken Asia level) — in-sample 52.4% /
  +857 on the pre-C3 baseline; misses the 25% of breaks that never pull
  back. Worse than confirm in-sample; not re-validated OOS.
* **"zone"** — the pullback magnet is the project's own short-term S/R
  (nearest KEY-LEVELS zone between entry and stop, as-of the confirmation).
  In-sample this looked beautiful — 28 trades · 75.0% · +36.7pp — and on
  the MT5:GOLD 17-month out-of-sample it INVERTED: 30 trades · 30.0% ·
  net −232. A textbook small-sample selection artifact: kept implemented
  and documented, never shipped. Do not be seduced by the in-sample number.

The shipped default therefore enters the bar after the confirmation close.
The machinery stays config-driven so every measurement above stays
reproducible.
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

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Signal6Config:
    """The framework's brackets. Design constants, not fitted evidence.

    The knobs exist so the geometry can be TESTED against the history walker
    and then frozen — v1's brackets (TP at the mid, SL beyond the wick) were
    measured at 23% / net -233 over 30 days, and the fix chosen here is the
    one liquidity traders actually use: stop at the defended LEVEL (the wick
    was already refused; it does not need to be exceeded again to invalidate
    the thesis), target the OPPOSITE side of the range (where the pool being
    hunted lives), and only take the session pattern while the sessions that
    create it are still open.
    """

    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"
    anchor: str = "5m"
    side: str = "both"
    # The granularity the walker loads for the path. "1m" is exact; "5m"
    # lets the same rules be walked over longer histories that were only
    # stored at 5m (MT5:GOLD has 17 months of it), at some resolution loss
    # in the TP/SL walk.
    data_interval: str = "1m"

    # --- geometry, in R = today's Asia range -----------------------------
    sweep_tp: str = "far"        # "mid" | "far" (far = the opposite edge)
    sweep_sl: str = "level"      # "wick" | "level" (the defended level)
    sl_buffer_of_range: float = 0.50   # stop beyond the anchor, in R
    break_tp_of_range: float = 1.0     # break target beyond the level, in R

    # --- filters -----------------------------------------------------------
    # 欧盘扫亚洲：the reversal only has the counterparty to run it while the
    # sessions that create the pattern are open. None disables the filter.
    confirm_hours_myt: tuple[int, int] | None = (15, 22)
    first_sweep_only: bool = True       # the first sweep per side per day
    min_range_of_price: float = 0.001   # a degenerate Asia range is noise
    # Sweeps stay MAP EVENTS and notifications, but do not trade: measured on
    # the full 1m history, the sweep-reversal entry lost money in EVERY
    # geometry tested (best single shape still net negative), while the
    # volume-confirmed break carried the whole result. 扫盘不追单 is the
    # framework's own instruction; the walker agrees with it.
    trade_sweeps: bool = False

    # --- pattern selection (measured on the full history; see docstring) ---
    # require_volume: only volume-confirmed breaks (突破有量).
    # require_prior_sweep: the 扫盘→突破 sequence — a break only trades if
    #   the SAME side's level was swept earlier in the day. THE C3 DEFAULT:
    #   the single biggest win-rate factor found (+8pp alone), and the
    #   framework's own third layer read literally (Sweep → Break →
    #   Continuation).
    # first_break_only: after a reclaim, a re-break of the same side does
    #   not trade — measured NOT helpful (48.4%); kept as a knob only.
    require_volume: bool = True
    require_prior_sweep: bool = True
    first_break_only: bool = False
    # One trade a day, by the owner's choice. 0 = unlimited. The rule is
    # strictly causal: the FIRST qualifying event of the day takes the slot;
    # later events the same day are announced as structure but do not trade.
    max_trades_per_day: int = 0

    # --- the daily ladder: a COVERAGE mode for "at least one trade per
    # weekday", off by default because every fallback tier measured below
    # break-even. Tiers by quality: A = prior-sweep sequence + volume,
    # B = volume alone, C = any break — lower tiers accepted only later in
    # the day (B from ladder_b_h, C from ladder_c_h), so the morning stays
    # reserved for the A-grade pattern. D (`last_resort`) acts at 20:00 on
    # an early sweep when the day produced no break at all.
    #
    # Measured, causally, on the full history:
    #     A only, unlimited (SHIPPED): 105 trades · 62.9% · +1334
    #     A + C from 19:00:             65 · 61.5% · +803   (C tier 9 · 33%)
    #     A + B + C:                    85 · 55.3% · +690   (B tier 25 · 32%)
    #     + D at 20:00:             +5 trades · 40%
    # Every step toward "a trade every day" trades quality for coverage, and
    # ~25% of weekdays have no structure to trade at all. Turn the ladder on
    # for coverage at the documented cost.
    daily_ladder: bool = False
    ladder_b_h: int = 17     # B (volume break) accepted from this MYT hour
    ladder_c_h: int = 19     # C (any break) accepted from this MYT hour
    ladder_d_h: int = 20     # D (last-resort sweep) acted on from this hour
    d_sweep_h: int = 17      # D only uses sweeps confirmed BY this hour
    last_resort: bool = False

    # --- entry timing ------------------------------------------------------
    # "confirm": enter the bar after the second volume-confirmed close
    #           (buys the break itself — the shipped default).
    # "retest":  wait, up to `retest_wait_bars`, for a pullback that TOUCHES
    #           the broken level (within `retest_touch_r` of it) and CLOSES
    #           back on the far side — the 口诀's 回踩不破 — and enter the bar
    #           after that hold. No retest within the window, or a close back
    #           through the level first, means no trade.
    break_entry: str = "confirm"
    retest_touch_r: float = 0.15
    retest_wait_bars: int = 12        # 5m bars = one hour of patience
    # "zone": the pullback magnet is the project's OWN short-term S/R — the
    # nearest KEY-LEVELS zone below the entry price (long) / above it
    # (short), read as-of the break's confirmation. The touch/hold/fail
    # semantics are the same as the level retest; only the magnet differs.
    zone_wait_bars: int = 12

    lots: float = 0.01
    measured: bool = False

    @property
    def label(self) -> str:
        return ("Sweep-then-break of the Asia range, volume confirmed, "
                "session windows (session framework port)")

    @property
    def bracket_label(self) -> str:
        window = (f"{self.confirm_hours_myt[0]:02d}:00-"
                  f"{self.confirm_hours_myt[1]:02d}:00 MYT"
                  if self.confirm_hours_myt else "any time")
        sweeps = "traded" if self.trade_sweeps else "observed, not traded"
        seq = ("requires a prior same-side sweep (扫盘→突破)"
               if self.require_prior_sweep else "no sequence requirement")
        mode = ("daily coverage ladder (B/C/D fallbacks, measured cheaper "
                "than they look)" if self.daily_ladder else
                ("1 trade/day" if self.max_trades_per_day == 1
                 else "every qualifying sequence"))
        return (f"Break: SL level-{self.sl_buffer_of_range:g}R, TP level+"
                f"{self.break_tp_of_range:g}R · R = today's Asia range · "
                f"confirmed {window} · {seq} · sweeps {sweeps} · {mode}. "
                "In-sample walker: 105 trades, 62.9%; OUT-OF-SAMPLE "
                "(MT5:GOLD 17m, untouched rules): 201 trades, 47.8% vs "
                "42.1% break-even, net positive but thin. In-sample "
                "selection — see module notes.")


def nearest_zone_level(resistance, support, price: float,
                       is_long: bool) -> float | None:
    """The pullback magnet from the project's own short-term S/R.

    ``resistance``/``support`` are the snapshot's zone tuples (lo, hi,
    strength, sources). For a LONG the magnet is the nearest zone band
    wholly below ``price`` (its near edge = the band's high); for a SHORT,
    the nearest band wholly above (its near edge = the band's low). Any
    side counts — a resistance under price is a broken level and pulls
    backfills the same way a support does.
    """
    best = None
    for lo, hi, _s, _src in list(resistance or ()) + list(support or ()):
        try:
            lo, hi = float(lo), float(hi)
        except (TypeError, ValueError):
            continue
        if not lo < hi:
            continue
        if is_long:
            if hi < price and (best is None or hi > best):
                best = hi
        else:
            if lo > price and (best is None or lo < best):
                best = lo
    return best


def _myt_hour(ms: int) -> int:
    return ((int(ms) // 3_600_000) + 8) % 24


def myt_day_of(ms: int) -> int:
    """Midnight MYT of the day containing ``ms``, as epoch ms."""
    offset = 8 * 3_600_000
    return ((int(ms) + offset) // 86_400_000) * 86_400_000 - offset


def _in_window(hh: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    a, b = window
    return (a <= hh < b) if a <= b else (hh >= a or hh < b)


def retest_fill(pt, ph, pl, pc, po, start_t: int, level: float, r: float,
                is_long: bool,
                cfg: Signal6Config | None = None):
    """(entry_time, entry_price) for a 回踩不破 entry, or None if no retest.

    Walks 5-minute buckets folded from the 1m path — CLOSED bars only, the
    same CONFIRMED rule everywhere else uses. For a break_high (long):

    * a bucket that CLOSES back below the level before any retest-held
      bucket is a failed break — no trade;
    * the first bucket whose LOW comes within ``retest_touch_r``·R of the
      level AND whose close is back at/above it is the hold;
    * entry is the open of the NEXT bucket — at the level rather than at
      the break, which is the whole point of waiting.

    Mirrored for break_low (short): the pullback is UP to the level.
    """
    import numpy as np

    c = cfg or Signal6Config()
    touch = c.retest_touch_r * r
    window_end = start_t + c.retest_wait_bars * 300_000
    i0 = int(np.searchsorted(pt, start_t))
    i1 = int(np.searchsorted(pt, window_end, side="right"))
    if i0 >= i1:
        return None

    t = pt[i0:i1]
    hi = ph[i0:i1]
    lo = pl[i0:i1]
    cl = pc[i0:i1]
    key = (t // 300_000) * 300_000
    n = len(t)
    i = 0
    while i < n:
        b = key[i]
        j = i
        hi_b, lo_b, last_close = hi[i], lo[i], cl[i]
        while j < n and key[j] == b:
            hi_b = max(hi_b, hi[j])
            lo_b = min(lo_b, lo[j])
            last_close = cl[j]
            j += 1
        if is_long:
            if last_close < level:
                return None          # closed back through: the break failed
            if lo_b <= level + touch and last_close >= level:
                k = int(np.searchsorted(pt, b + 300_000))
                if k >= len(pt):
                    return None
                return (int(pt[k]), float(po[k]))
        else:
            if last_close > level:
                return None
            if hi_b >= level - touch and last_close <= level:
                k = int(np.searchsorted(pt, b + 300_000))
                if k >= len(pt):
                    return None
                return (int(pt[k]), float(po[k]))
        i = j
    return None      # ran away without ever retesting: no trade


def tradable_events(block: dict, cfg: Signal6Config | None = None) -> list[dict]:
    """The day's events that carry an entry, after every filter.

    One definition shared by the live evaluator and the history walker.

    With ``daily_ladder`` (the default) the day takes exactly one trade,
    chosen CAUSALLY by a quality ladder with time floors:

        A  prior-sweep sequence + volume break   accepted from 15:00
        B  volume break alone                    accepted from 17:00
        C  any break                             accepted from 19:00

    "Causal" means each event is judged when it happens — an early B cannot
    fire because it cannot know whether an A is coming, but from 17:00 a B
    is good enough. The first ACCEPTED event wins the day; later events stay
    structure, announced but not traded.

    Without the ladder, the plain filters apply (kind gates, hour window,
    first-per-side rules) capped by ``max_trades_per_day``.

    D — the last resort for a day with NO break at all — is not an event
    here: it is a time trigger (act at 20:00 on an early sweep), resolved by
    `last_resort_sweep` so the evaluator and the walker share one definition.
    """
    c = cfg or Signal6Config()
    out: list[dict] = []
    taken_sweep: set[str] = set()
    taken_break: set[str] = set()
    seen_sweep: set[str] = set()          # any sweep today, tradable or not
    for raw in block.get("events") or []:
        kind = str(raw.get("type") or "")
        if kind in ("sweep_high", "sweep_low"):
            seen_sweep.add(kind)
        if not wants(raw, block, c):
            continue
        confirm = int(raw.get("confirm_ms") or raw.get("ts_ms") or 0)
        hour = _myt_hour(confirm)
        if not _in_window(hour, c.confirm_hours_myt):
            continue
        if kind in ("sweep_high", "sweep_low"):
            if c.first_sweep_only and kind in taken_sweep:
                continue
            taken_sweep.add(kind)
        elif kind in ("break_high", "break_low"):
            side = kind.removeprefix("break_")
            vol = bool(raw.get("vol_ok"))
            if c.daily_ladder:
                seq = f"sweep_{side}" in seen_sweep
                if not (seq and vol) and hour < (
                        c.ladder_b_h if vol else c.ladder_c_h):
                    continue      # the morning stays reserved for A-grade
                if c.max_trades_per_day and len(out) >= c.max_trades_per_day:
                    continue
                out.append(raw)
                continue
            if c.require_prior_sweep and f"sweep_{side}" not in seen_sweep:
                continue
            if c.first_break_only:
                if kind in taken_break:
                    continue
                taken_break.add(kind)
        if c.max_trades_per_day and len(out) >= c.max_trades_per_day:
            continue
        out.append(raw)
    return out


def last_resort_sweep(block: dict, cfg: Signal6Config | None = None):
    """(event, act_after_ms) for the D tier, or None.

    Only on a day whose ladder produced NOTHING (no break of any tier
    traded) and whose FIRST sweep confirmed early (欧盘扫流动 — a late sweep
    is drift, measured 18.2%): the sweep reversal is acted on from
    ``ladder_d_h``. Returning the event plus the acting time keeps the
    decision causal — at the sweep itself nobody can know a break will not
    come later; at 20:00 they can.
    """
    c = cfg or Signal6Config()
    if not c.daily_ladder or not c.last_resort or block.get("weekend"):
        return None
    if tradable_events(block, c):
        return None                 # the ladder already has today's trade
    lv = block.get("levels") or {}
    if lv.get("asia_high") is None or lv.get("asia_low") is None:
        return None
    for raw in block.get("events") or []:
        kind = str(raw.get("type") or "")
        if kind not in ("sweep_high", "sweep_low"):
            continue
        confirm = int(raw.get("confirm_ms") or raw.get("ts_ms") or 0)
        if _myt_hour(confirm) > c.d_sweep_h:
            continue                # late sweep: drift, not liquidity
        day0 = myt_day_of(confirm)
        act = day0 + c.ladder_d_h * 3_600_000
        if act >= day0 + 86_400_000:
            return None             # acting time would fall into tomorrow
        return raw, act
    return None


def wants(event: dict, block: dict,
          cfg: Signal6Config | None = None) -> bool:
    """Does this session event carry an entry, before the day-level filters?

    Kind gates only: sweeps always, breaks only with volume, reclaims and
    quiet breaks never, and nothing on a weekend — the framework's own first
    rule is that weekend flow is drift, not price discovery. The hour window
    and first-sweep rule live in `tradable_events`, which needs the whole day.
    """
    c = cfg or Signal6Config()
    kind = str(event.get("type") or "")
    if block.get("weekend"):
        return False
    lv = block.get("levels") or {}
    hi, lo = lv.get("asia_high"), lv.get("asia_low")
    if hi is None or lo is None:
        return False
    if c.min_range_of_price > 0:
        mid = (float(hi) + float(lo)) / 2.0
        r = float(hi) - float(lo)
        if mid <= 0 or r < c.min_range_of_price * abs(mid):
            return False
    if kind in ("sweep_high", "sweep_low"):
        return bool(c.trade_sweeps)
    if kind in ("break_high", "break_low"):
        return bool(event.get("vol_ok")) if c.require_volume else True
    return False


def brackets_for(event: dict, block: dict,
                 cfg: Signal6Config | None = None) -> tuple[str, float, float] | None:
    """(side, sl, tp) for an event, from the map's levels. None if untradable.

    The pure heart of the signal: every consumer — live evaluation, the
    Telegram push, the history walker — reads THIS, so they cannot disagree
    about what a sweep's stop is.

    Sweep geometry, the liquidity-trader version:

    * **SL at the defended LEVEL** (default), not beyond the wick. The wick
      was the market's own refusal — price does not need to exceed the
      extreme a second time for the reversal thesis to be dead; closing back
      THROUGH the level is the invalidation. A stop beyond the wick pays for
      violence that already happened.
    * **TP at the opposite edge** (default): a sweep of the high is the market
      hunting the pool above it and turning toward the pool below — the far
      side of the range is where that move ends, which is the口诀's own read
      (扫高 → 跌回 → 持续走弱). The mid takes half the move at the same risk.
    """
    c = cfg or Signal6Config()
    lv = block.get("levels") or {}
    hi, lo = lv.get("asia_high"), lv.get("asia_low")
    if hi is None or lo is None:
        return None
    r = float(hi) - float(lo)
    if not r > 0:
        return None
    mid = (float(hi) + float(lo)) / 2.0
    buf = c.sl_buffer_of_range * r
    kind = str(event.get("type") or "")
    extreme = event.get("price")
    if kind == "sweep_high" and extreme is not None:
        sl = float(hi) + buf if c.sweep_sl == "level" \
            else float(extreme) + buf
        tp = float(lo) if c.sweep_tp == "far" else mid
        return ("short", sl, tp)
    if kind == "sweep_low" and extreme is not None:
        sl = float(lo) - buf if c.sweep_sl == "level" \
            else float(extreme) - buf
        tp = float(hi) if c.sweep_tp == "far" else mid
        return ("long", sl, tp)
    if kind == "break_high":
        return ("long", float(hi) - buf, float(hi) + c.break_tp_of_range * r)
    if kind == "break_low":
        return ("short", float(lo) + buf, float(lo) - c.break_tp_of_range * r)
    return None


def _round(v: Any, dp: int = 2) -> Any:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, dp)


class Signal6:
    """Evaluates the framework on a fresh session map; logs every fire."""

    def __init__(self, log_path: Path, cfg: Signal6Config | None = None) -> None:
        self.cfg = cfg or Signal6Config()
        self.log_path = Path(log_path)
        self._events: list[dict] = []
        # (symbol, confirm_ms, type) — the same event re-delivered by a later
        # map rebuild must not fire twice.
        self._emitted: set[tuple[str, int, str]] = set()
        self._seeded = False

    def evaluate(self, block: dict | None, spot: float | None = None) -> dict:
        """The newest tradable event on the map, as a signal row.

        ``block`` is a session map as built by `session_map.build_map` — the
        fast loop's fresh copy. Event-driven like the map itself: ``fired`` is
        true only on the evaluation where a tradable event is first seen; the
        panel otherwise shows the standing conditions and what it waits for.
        """
        c = self.cfg
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION, "name": "signal6",
            "symbol": c.symbol, "broker_symbol": c.broker_symbol,
            "anchor": c.anchor, "side": c.side,
            "pattern": c.label, "bracket": c.bracket_label,
            "measured": c.measured, "fired": False, "legs": {}, "blocking": [],
        }
        if not isinstance(block, dict) or block.get("error"):
            out["reason"] = "no session map available"
            return out
        if block.get("symbol") and block["symbol"] != c.symbol:
            out["reason"] = f"session map is for {block['symbol']}"
            return out

        lv = block.get("levels") or {}
        has_range = lv.get("asia_high") is not None and lv.get("asia_low") is not None
        legs = {
            "asia range defined": bool(has_range),
            "weekday (not weekend)": not bool(block.get("weekend")),
            "tradable session event": False,
        }
        fired: dict[str, Any] | None = None
        if has_range and not block.get("weekend"):
            # Chronological first-unseen: with unlimited trades several
            # events can pend at once (a stalled feed), and they should fire
            # in the order they happened, same as the history walker.
            for ev in tradable_events(block, c):
                key = (c.symbol,
                       int(ev.get("confirm_ms") or ev.get("ts_ms") or 0),
                       str(ev.get("type")))
                if key in self._emitted:
                    continue
                bracket = brackets_for(ev, block, c)
                if bracket is None:
                    continue
                fired = {"event": ev, "bracket": bracket, "tier": None}
                break
            if fired is None and c.last_resort:
                # D: act on an early sweep at 20:00 when the day had no
                # break — the same rule the walker resolves.
                d = last_resort_sweep(block, c)
                if d is not None:
                    ev, act = d
                    if int(block.get("as_of_ms") or 0) >= act:
                        bracket = brackets_for(ev, block, c)
                        if bracket is not None and spot:
                            side, sl, tp = bracket
                            r = ((lv.get("asia_high") or 0)
                                 - (lv.get("asia_low") or 0))
                            if (sl + 0.1 * r) < float(spot) < (tp - 0.1 * r):
                                fired = {"event": ev, "bracket": bracket,
                                         "tier": "D", "act_ms": act}

        out.update({
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bar_open_ms": block.get("as_of_ms"),
            "bar_myt": block.get("as_of_myt", ""),
            "day_myt": block.get("day_myt", ""),
            "legs": legs,
        })
        if fired is None:
            out["blocking"] = [k for k, v in legs.items() if not v]
            out["reason"] = ("session event pending — sweeps and volume-confirmed "
                             "breaks fire on confirmation")
            return out

        side, sl, tp = fired["bracket"]
        ev = fired["event"]
        entry_ref = float(spot) if spot else float(ev.get("level") or 0.0)
        confirm = int(fired.get("act_ms")
                      or ev.get("confirm_ms") or ev.get("ts_ms") or 0)
        legs["tradable session event"] = True
        out.update({
            "fired": True, "side": side,
            "legs": legs, "blocking": [],
            "event_type": ev.get("type"),
            "tier": fired.get("tier") or "A",
            "event_ts_ms": int(ev.get("ts_ms") or confirm),
            "bar_open_ms": confirm,
            "bar_myt": ev.get("ts_myt", ""),
            "entry_ref": _round(entry_ref), "sl": _round(sl), "tp": _round(tp),
            "lots": c.lots,
            "asia_range": _round((lv.get("asia_high") or 0) - (lv.get("asia_low") or 0)),
            "reason": f"{ev.get('type')} confirmed — {side} per framework",
        })
        self._record_event(out)
        return out

    def _record_event(self, out: dict) -> None:
        key = (out["symbol"], int(out["bar_open_ms"]), str(out["event_type"]))
        if key in self._emitted:
            out["fired"] = False
            return
        self._emitted.add(key)
        self._events.append({
            "id": f"s6:{out['symbol']}@{out['bar_open_ms']}:{out['event_type']}",
            "name": "signal6", "ts_utc": out["ts_utc"],
            "ts_ms": int(time.time() * 1000),
            "symbol": out["symbol"], "broker_symbol": out["broker_symbol"],
            "side": out["side"], "bar_open_ms": out["bar_open_ms"],
            "bar_myt": out["bar_myt"], "entry_ref": out["entry_ref"],
            "sl": out["sl"], "tp": out["tp"], "lots": out["lots"],
            "bracket": out["bracket"], "measured": False,
            "pattern": out["pattern"], "event_type": out["event_type"],
        })
        log.warning("SIGNAL6 %s %s @ %s  SL %s  TP %s  (%s, UNMEASURED)",
                    out["symbol"], out["side"].upper(), out["entry_ref"],
                    out["sl"], out["tp"], out["event_type"])

    def events(self) -> list[dict]:
        return list(self._events)

    def seed_from_log(self) -> None:
        """A restart must not re-fire events already in the log."""
        if self._seeded:
            return
        self._seeded = True
        try:
            for row in read_log(self.log_path):
                if row.get("fired") and row.get("bar_open_ms") is not None:
                    self._emitted.add((row.get("symbol", ""),
                                       int(row["bar_open_ms"]),
                                       str(row.get("event_type") or "")))
        except Exception as exc:
            log.debug("signal6 could not seed emitted events: %s", exc)

    def write(self, evaluation: dict) -> bool:
        """Only FIRING rows are logged, same policy as signal 5."""
        if not evaluation.get("fired"):
            return False
        bar = evaluation.get("bar_open_ms")
        if bar is None:
            return False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            for row in read_log(self.log_path):
                if (row.get("symbol") == evaluation.get("symbol")
                        and int(row.get("bar_open_ms", -1)) == int(bar)
                        and row.get("event_type") == evaluation.get("event_type")):
                    return False
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evaluation, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as exc:
            log.debug("signal6 log write failed: %s", exc)
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
