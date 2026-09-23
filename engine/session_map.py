"""Session liquidity map — the intraday framework, mechanised.

亚盘定范围，欧盘扫流动，美盘定方向 (Asia sets the range, Europe sweeps the
liquidity, America sets the direction), plus the price-action flow the same
framework reads: **Sweep → Break → Retest → Continuation**.

**This is an observation, not a signal.** Nothing here enters a trade, fires
the rule, or feeds the risk gate. It names what already happened — where the
Asia range is, whether Europe took the liquidity above it or below it, whether
a break is holding — because the framework's own first rule is 先判断「市场在
哪里」before anything else. The sweep/break vocabulary is the owner's
(`XAUUSD_黄金日内交易口诀与框架.md` §1-3, §6): a poke that returns is a sweep,
a break that returns is a reclaim, and neither is a direction on its own.

## Session windows — the single definition

MYT wall-clock hours, fixed (Malaysia has no DST):

    asia    07:00-15:00    the range this map draws
    europe  15:00-20:00    where the sweep/break reading starts
    us      20:00-05:00    where direction is confirmed or reversed
    off     05:00-07:00

This settles OPEN_QUESTIONS Q26 for THIS feature's purposes: `features.
session_myt` (backtest feature labels) and `signal4.is_asia` (mining inputs)
keep their own windows because changing them would change what was measured;
`session_map` is the definition anything user-facing reads, and it is here.

## Detection semantics (5m closes, today only, no lookahead)

Per side of the Asia range, from 15:00 MYT onward:

* **sweep** — price poked beyond the level and came back: either a single bar
  whose wick exceeded it but closed back inside, or one close beyond followed
  by a close back inside. 扫盘不追单.
* **break** — two consecutive 5m closes beyond the level. The volumes of the
  breaking bars are compared with the trailing 20-bar median and the event
  carries `vol_ok`; 突破有量 is the framework's own confirmation, so a
  low-volume break is still reported but flagged.
* **reclaim** — after a break, a close back inside the range. A failed
  breakout is information, not silence.

`as_of_ms` bounds what the map may read: bars that had not CLOSED by that
instant do not exist to it, which is what makes `build_map(df, as_of=T)`
bit-identical to `build_map(df[:first bar after T], as_of=T)` — the property
`tests/test_session_map.py` pins. The forming minute never produces an event,
for the same reason the signals never read it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

MYT = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
MIN_MS = 60_000
STEP_5M = 300_000

# Windows in MYT hours [start, end). US wraps midnight.
ASIA_H, ASIA_END_H = 7, 15
EUROPE_H, EUROPE_END_H = 15, 20
US_H, US_END_H = 20, 5

# Detection thresholds. Not fitted — they are the framework's own words given
# numbers, and they are stated on every surface that shows an event.
BREAK_CLOSES = 2          # consecutive 5m closes beyond the level = a break
BREAK_VOL_RATIO = 1.2     # break bars' median volume vs trailing 20-bar median
VOL_MEDIAN_BARS = 20
US_MOVE_SHARE_OF_RANGE = 0.15   # US move must exceed this share of the range

# The day verdict unlocks half an hour into the US session (20:30 MYT) — the
# owner's own timing: enough time for New York to have shown its hand, not so
# much that the day is already over. Direction gates, in R units:
EUROPE_MIN_MOVE_R = 0.15   # Europe must travel this far off the mid to "choose"
US_FLIP_R = 0.25           # a reversal only flips the day if it clears the mid
VERDICT_AT = (20, 30)      # MYT hour/minute the full-day verdict unlocks

SCHEMA_VERSION = 1


# -- time helpers (MYT is UTC+8 with no DST, so plain integer arithmetic) ----

def myt_day_ms(ts_ms):
    """Midnight MYT of the day containing ``ts_ms``, as epoch ms.

    Scalar or array: the build folds a whole frame's days at once, and doing
    it per-bar with datetimes is thousands of fromtimestamp calls where one
    integer expression serves.
    """
    offset = 8 * 3_600_000
    t = np.asarray(ts_ms, dtype="int64")
    day = ((t + offset) // DAY_MS) * DAY_MS - offset
    return day if day.ndim else int(day)


def myt_hour(ts_ms: int) -> int:
    return ((int(ts_ms) // 3_600_000) + 8) % 24


def phase(ts_ms: int) -> str:
    """"asia" / "europe" / "us" / "off" — one definition for every surface."""
    h = myt_hour(ts_ms)
    if ASIA_H <= h < ASIA_END_H:
        return "asia"
    if EUROPE_H <= h < EUROPE_END_H:
        return "europe"
    if h >= US_H or h < US_END_H:
        return "us"
    return "off"


def _fmt_myt(ts_ms: int, pat: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts_ms / 1000, MYT).strftime(pat)


def _round(v: Any, dp: int = 2) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, dp)


def _fold_5m(minute: pd.DataFrame) -> pd.DataFrame:
    """1-minute bars -> 5-minute bars. Exact for OHLCV; holes stay holes."""
    bucket = (minute["open_time"] // STEP_5M) * STEP_5M
    out = minute.groupby(bucket).agg(
        open_time=("open_time", "min"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)
    return out


def _median(seq: np.ndarray, i: int, n: int) -> float:
    """Median of ``seq[i-n:i]`` — the bars BEFORE i, never including it."""
    window = seq[max(0, i - n):i]
    return float(np.median(window)) if window.size else float("nan")


# -- the state machine --------------------------------------------------------

def _side_events(bars: pd.DataFrame, start_i: int, level: float, side: str,
                 vol: np.ndarray) -> list[dict]:
    """Sweep / break / reclaim events for one side of the Asia range.

    Walks today's 5m bars from ``start_i`` (the first bar of the Europe
    window) chronologically. A break, once emitted, only ends in a reclaim;
    sweeps resume afterwards. Consecutive-bar duplicate sweeps are suppressed
    (a run of wick pokes is one visit, not five events).
    """
    sign = 1 if side == "high" else -1
    events: list[dict] = []
    broken = False
    run = 0                  # consecutive closes beyond the level
    run_first = -1
    last_sweep_ts = -1

    def beyond(v: float) -> bool:
        return v > level if sign > 0 else v < level

    def back_inside(v: float) -> bool:
        return v < level if sign > 0 else v > level

    close = bars["close"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    ts = bars["open_time"].to_numpy(dtype="int64")

    def emit(kind: str, i: int, price: float, vol_ok: bool | None = None) -> None:
        ev = {"ts_ms": int(ts[i]), "ts_myt": _fmt_myt(int(ts[i])),
              "type": f"{kind}_{side}", "price": _round(price),
              "level": _round(level)}
        if vol_ok is not None:
            ev["vol_ok"] = bool(vol_ok)
        events.append(ev)

    def emit_at(kind: str, at: int, confirmed_by: int, price: float,
                vol_ok: bool | None = None) -> None:
        """Structure time ``at``; ``confirmed_by`` is the bar that made it known.

        A sweep's ts is the POKE bar — where it belongs on a chart — but it only
        became a fact when a later bar closed back inside. Signal 6 enters on
        the bar AFTER confirmation, so the event carries both timestamps and
        nothing has to guess which one an entry may read.
        """
        emit(kind, at, price, vol_ok)
        ev = events[-1]
        ev["confirm_ms"] = int(ts[confirmed_by])

    for i in range(start_i, len(bars)):
        c = close[i]
        if not np.isfinite(c):
            continue
        if broken:
            if back_inside(c):
                emit_at("reclaim", i, i, c)
                broken = False
                run = 0
            continue
        if beyond(c):
            run += 1
            if run == 1:
                run_first = i
            if run >= BREAK_CLOSES:
                # Volume is the framework's own confirmation, carried on the
                # event rather than silently deciding what gets reported.
                brk = vol[run_first:run_first + BREAK_CLOSES]
                base = _median(vol, run_first, VOL_MEDIAN_BARS)
                ratio_ok = bool(
                    np.isfinite(base) and base > 0
                    and float(np.median(brk)) > BREAK_VOL_RATIO * base
                ) if np.all(np.isfinite(brk)) and brk.size else False
                emit_at("break", run_first, i, c, vol_ok=ratio_ok)
                broken = True
        else:
            if run == 1:
                # One close beyond, now back inside: a close-confirmed sweep.
                emit_at("sweep", run_first, i,
                        float(np.max(high[run_first:i + 1]) if sign > 0
                              else np.min(low[run_first:i + 1])))
                last_sweep_ts = int(ts[run_first])
            elif run == 0:
                wick = high[i] if sign > 0 else low[i]
                if beyond(float(wick)) and int(ts[i]) - last_sweep_ts > STEP_5M:
                    emit_at("sweep", i, i, float(wick))
                    last_sweep_ts = int(ts[i])
            run = 0
    return events


def build_map(minute: pd.DataFrame, as_of_ms: int | None = None) -> dict:
    """The session map as of ``as_of_ms`` (default: every bar given is closed).

    ``minute`` is 1m bars with the columns the repository serves. Only bars
    that had CLOSED by ``as_of_ms`` are read; the forming minute does not
    exist to this function. Never raises for short data — missing levels are
    None and the caller says so.
    """
    if minute is None or minute.empty:
        return {"schema": SCHEMA_VERSION, "error": "no 1m bars",
                "as_of_ms": int(as_of_ms or 0)}

    df = minute[minute["open_time"] + MIN_MS <= int(as_of_ms)] \
        if as_of_ms is not None else minute
    if df.empty:
        return {"schema": SCHEMA_VERSION, "error": "no closed 1m bars",
                "as_of_ms": int(as_of_ms or 0)}

    as_of = int(as_of_ms) if as_of_ms is not None \
        else int(df["open_time"].iloc[-1]) + MIN_MS
    day0 = myt_day_ms(as_of)
    day = myt_day_ms(df["open_time"].to_numpy(dtype="int64"))

    # --- levels ----------------------------------------------------------
    def hi(lo_of: np.ndarray) -> Any:
        return float(df["high"].to_numpy()[lo_of].max()) if lo_of.any() else None

    def lo(where: np.ndarray) -> Any:
        return float(df["low"].to_numpy()[where].min()) if where.any() else None

    hour = ((df["open_time"].to_numpy(dtype="int64") // 3_600_000) + 8) % 24
    today = day == day0
    prev = day == day0 - DAY_MS
    asia = today & (hour >= ASIA_H) & (hour < ASIA_END_H)
    # Yesterday's US session spans midnight: [prev 20:00, today 05:00).
    in_prev_us = ((day == day0 - DAY_MS) & (hour >= US_H)) | \
                 ((day == day0) & (hour < US_END_H))

    asia_high, asia_low = hi(asia), lo(asia)
    prev_high, prev_low = hi(prev), lo(prev)
    pus_high, pus_low = hi(in_prev_us), lo(in_prev_us)
    day_open = float(df["open"].to_numpy()[today][0]) if today.any() else None

    levels = {
        "asia_high": _round(asia_high), "asia_low": _round(asia_low),
        "asia_range": _round(asia_high - asia_low)
                      if asia_high is not None and asia_low is not None else None,
        # The range is definitive once the Europe window has opened; before
        # that it is "so far", which the Asia session itself is happy to be.
        "asia_done": as_of >= day0 + ASIA_END_H * 3_600_000,
        "prev_day_high": _round(prev_high), "prev_day_low": _round(prev_low),
        "prev_us_high": _round(pus_high), "prev_us_low": _round(pus_low),
        "day_open": _round(day_open),
    }

    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "as_of_ms": as_of,
        "as_of_myt": _fmt_myt(as_of),
        "day_myt": _fmt_myt(day0, "%Y-%m-%d"),
        "phase": phase(as_of),
        "weekend": datetime.fromtimestamp(as_of / 1000, MYT).weekday() >= 5,
        "windows_myt": {"asia": "07:00-15:00", "europe": "15:00-20:00",
                        "us": "20:00-05:00"},
        "levels": levels,
        "events": [],
    }

    if asia_high is None or asia_low is None:
        out["state"] = "waiting_asia"
        out["us"] = None
        out["bias"] = {"phase": phase(as_of), "bias": "undecided",
                       "ready": False,
                       "verdict_myt": f"{VERDICT_AT[0]:02d}:{VERDICT_AT[1]:02d}",
                       "basis": ["Asia has not built a range yet — nothing "
                                 "to read (waiting for 07:00 MYT bars)"],
                       "close_r": None}
        return out

    # --- sweep / break / reclaim, today's 5m bars from Europe on ---------
    five = _fold_5m(df[df["open_time"] >= day0]).reset_index(drop=True)
    # A bucket whose five minutes have not all elapsed by ``as_of`` is a
    # forming 5m bar — its close is a mid-bucket print that can un-change
    # before the bar ends, so it is excluded exactly like the forming minute
    # itself. The whole engine's CONFIRMED rule, at one remove.
    five = five[five["open_time"] + STEP_5M <= as_of].reset_index(drop=True)
    events: list[dict] = []
    if not five.empty:
        ts5 = five["open_time"].to_numpy(dtype="int64")
        start_i = int(np.searchsorted(ts5, day0 + EUROPE_H * 3_600_000, "left"))
        vol = five["volume"].to_numpy(dtype="float64")
        ev_h = _side_events(five, start_i, float(asia_high), "high", vol)
        ev_l = _side_events(five, start_i, float(asia_low), "low", vol)
        events = sorted(ev_h + ev_l, key=lambda e: e["ts_ms"])
    out["events"] = events

    # --- the day's state, as of the last closed bar ----------------------
    broke = {"high": False, "low": False}
    swept: str | None = None
    for e in events:
        t = e["type"]
        if t == "break_high":
            broke["high"] = True
        elif t == "break_low":
            broke["low"] = True
        elif t == "reclaim_high":
            broke["high"] = False
        elif t == "reclaim_low":
            broke["low"] = False
        elif t in ("sweep_high", "sweep_low"):
            swept = t.removeprefix("sweep_")
    last_close = float(df["close"].to_numpy()[-1])
    if broke["high"]:
        out["state"] = "broke_high"
    elif broke["low"]:
        out["state"] = "broke_low"
    elif swept is not None:
        out["state"] = f"swept_{swept}"
    elif last_close > asia_high:
        out["state"] = "above_range"
    elif last_close < asia_low:
        out["state"] = "below_range"
    else:
        out["state"] = "in_range"

    # --- the US read: did America confirm Europe or reverse it -----------
    out["us"] = _us_read(five, day0, last_close, asia_high, asia_low, as_of)

    # --- the day bias: 亚盘定范围 → 欧盘表态 → 美盘判定 -------------------
    out["bias"] = _bias_read(
        state=out["state"], events=events, asia_high=asia_high,
        asia_low=asia_low, day_open=day_open, last_close=last_close,
        as_of=as_of, day0=day0, us=out["us"])
    return out


def _us_read(five: pd.DataFrame, day0: int, last_close: float,
             asia_high: float, asia_low: float, as_of: int) -> dict | None:
    """Europe's move vs the US move after 20:00 — the 美盘定方向 read.

    Reported once the US window has opened; before that it is None, because
    "which way did New York take it" has no answer at 16:00.
    """
    if as_of < day0 + US_H * 3_600_000 or five.empty:
        return None
    ts5 = five["open_time"].to_numpy(dtype="int64")
    c5 = five["close"].to_numpy(dtype="float64")
    e_end = day0 + EUROPE_END_H * 3_600_000
    in_eu = (ts5 >= day0 + EUROPE_H * 3_600_000) & (ts5 < e_end)
    if not in_eu.any():
        return None
    europe_close = float(c5[in_eu][-1])
    asia_mid = (asia_high + asia_low) / 2.0
    thr = US_MOVE_SHARE_OF_RANGE * (asia_high - asia_low)
    eu_move = int(np.sign(europe_close - asia_mid))
    us_move = int(np.sign(last_close - europe_close))
    move = abs(last_close - europe_close)
    # A flat Europe leaves nothing to confirm or reverse; the verdict says
    # "flat" rather than inventing a reversal out of a zero baseline.
    verdict = "flat"
    if eu_move and us_move and move >= thr:
        verdict = "confirms" if us_move == eu_move else "reverses"
    return {
        "europe_close": _round(europe_close),
        "europe_move": eu_move if eu_move else None,
        "us_move": us_move if us_move else None,
        "verdict": verdict,
        "move": _round(move),
        "threshold": _round(thr),
        # The same moves in R units, which is what the day bias reasons in:
        # "Europe closed +0.4R above the mid" reads; "Europe closed +4.4"
        # does not, and changes meaning with every volatility regime.
        "europe_move_r": _round((europe_close - asia_mid) / (asia_high - asia_low), 2)
        if asia_high > asia_low else None,
        "us_move_r": _round((last_close - europe_close) / (asia_high - asia_low), 2)
        if asia_high > asia_low else None,
    }


def _bias_read(state: str, events: list[dict], asia_high: float,
               asia_low: float, day_open: float | None, last_close: float,
               as_of: int, day0: int, us: dict | None) -> dict:
    """The phase-aware day read: 亚盘 range, 欧盘 statement, 美盘 verdict.

    Every bias comes with its ``basis`` — the facts it was read from, in
    order, so the number is never asked to be trusted on its own. The three
    sessions answer different questions by design (the framework's own
    split): Asia establishes the range, Europe tips its hand through what it
    does to that range, and the US verdict — which unlocks at 20:30 MYT,
    half an hour into New York — says whether today is LONG, SHORT or RANGE.

    ``ready`` means "this is the day's verdict". Asia and Europe reads are
    honest interim statements (ready False): they can be overwritten by the
    next session, and saying so is the point.
    """
    r = asia_high - asia_low
    mid = (asia_high + asia_low) / 2.0
    close_r = (last_close - mid) / r if r > 0 else 0.0
    ph = phase(as_of)
    verdict_at = day0 + VERDICT_AT[0] * 3_600_000 + VERDICT_AT[1] * 60_000

    basis: list[str] = [
        f"Asia {_round(asia_low)}–{_round(asia_high)} (R={_round(r)})"]
    out = {"phase": ph, "bias": "undecided", "ready": False,
           "verdict_myt": f"{VERDICT_AT[0]:02d}:{VERDICT_AT[1]:02d}",
           "basis": basis, "close_r": _round(close_r, 2)}

    def ev_note(e: dict, what: str) -> str:
        return f"{e.get('ts_myt', '?')[-5:]} {what}"

    # ---- the key structural facts, in time order -------------------------
    last_sweep: dict | None = None
    for e in events:
        t = str(e.get("type") or "")
        if t in ("break_high", "break_low"):
            vol = "volume confirmed" if e.get("vol_ok") else "QUIET"
            basis.append(ev_note(e, f"{t} ({vol})"))
            last_sweep = None          # a break supersedes an earlier sweep
        elif t in ("reclaim_high", "reclaim_low"):
            basis.append(ev_note(e, f"{t} — the break failed"))
        elif t in ("sweep_high", "sweep_low"):
            last_sweep = e
    if last_sweep is not None:
        basis.append(ev_note(last_sweep, f"{last_sweep['type']} — liquidity "
                                         f"taken, returned"))

    # ---- 亚盘: the range is the answer, by definition ---------------------
    if ph == "asia":
        out["bias"] = "range"
        out["ready"] = False
        basis.append("亚盘只定范围 — the day's direction is not called in "
                     "Asia (口诀)")
        if day_open is not None and abs(last_close - day_open) >= 0.5 * r:
            basis.append(f"one-sided Asia ({_round(last_close - day_open)} "
                         f"from the open) — expect Europe to sweep or extend")
        return out

    # ---- 欧盘: what Europe did to the range is its statement -------------
    if ph == "europe":
        out["ready"] = False
        if state == "broke_high":
            out["bias"] = "long"
            basis.append("break holding above the range — 突破延续")
        elif state == "broke_low":
            out["bias"] = "short"
            basis.append("break holding below the range — 跌破延续")
        elif state == "swept_high":
            if close_r < -0.10:
                out["bias"] = "short"
                basis.append("price back under the mid — 空头取得主动权")
            else:
                out["bias"] = "range"
                basis.append("sweep inconclusive: price still above the mid")
        elif state == "swept_low":
            if close_r > 0.10:
                out["bias"] = "long"
                basis.append("price back above the mid — 买盘出现")
            else:
                out["bias"] = "range"
                basis.append("sweep inconclusive: price still below the mid")
        elif state in ("above_range", "below_range"):
            out["bias"] = "long" if state == "above_range" else "short"
            basis.append(f"close {state.replace('_', ' ')} without a "
                         "confirmed break — tentative")
        else:
            out["bias"] = "range"
            basis.append("nothing swept, nothing broke — Europe has not "
                         "tipped its hand")
        basis.append("欧盘表态 — can be overwritten by the US session")
        return out

    # ---- 美盘: the day verdict, from 20:30 -------------------------------
    if as_of < verdict_at:
        # Before the verdict time the read is still Europe's; say so rather
        # than presenting an interim view as the day's answer.
        basis.append(f"US verdict at {out['verdict_myt']} MYT — 美盘开始"
                     "半小时后判定")
        eu = _europe_statement(state, close_r, basis)
        out["bias"] = eu
        return out
    out["ready"] = True

    emr = (us or {}).get("europe_move_r")
    umr = (us or {}).get("us_move_r")
    if emr is not None:
        basis.append(f"Europe closed {emr:+.2f}R vs the Asia mid")
    if umr is not None:
        basis.append(f"US since Europe's close: {umr:+.2f}R")
    basis.append(f"net now {close_r:+.2f}R vs the mid")

    verdict = str((us or {}).get("verdict") or "flat")
    if us is None or emr is None:
        out["bias"] = "range"
        basis.append("no Europe session bars to read — 震荡日 by absence")
        return out
    if abs(emr) < EUROPE_MIN_MOVE_R:
        out["bias"] = "range"
        basis.append("Europe never travelled far enough to choose a side "
                     f"(<{EUROPE_MIN_MOVE_R}R) — 震荡日")
        return out
    eu_dir = "long" if emr > 0 else "short"
    if verdict == "confirms":
        out["bias"] = eu_dir
        basis.append("US confirmed Europe's direction — 美盘定方向")
    elif verdict == "reverses" and umr is not None and umr * emr < 0 \
            and close_r * (1 if emr > 0 else -1) < -US_FLIP_R:
        out["bias"] = "long" if eu_dir == "short" else "short"
        basis.append("US reversal has carried price through the mid — "
                     "the day flipped")
    elif verdict == "reverses":
        out["bias"] = "range"
        basis.append("US reversed Europe but not through the mid — "
                     "two-sided day reads as a range")
    else:
        # verdict "flat": US has not moved enough to confirm or reverse.
        if abs(close_r) >= US_FLIP_R:
            out["bias"] = eu_dir
            basis.append("US quiet so far — carrying Europe's read, price "
                         "still beyond the mid")
        else:
            out["bias"] = "range"
            basis.append("US quiet and price back near the mid — range")
    return out


def _europe_statement(state: str, close_r: float, basis: list[str]) -> str:
    """Europe's standing statement, reused before the US verdict time."""
    if state in ("broke_high", "above_range"):
        return "long"
    if state in ("broke_low", "below_range"):
        return "short"
    if state == "swept_high" and close_r < -0.10:
        return "short"
    if state == "swept_low" and close_r > 0.10:
        return "long"
    return "range"
