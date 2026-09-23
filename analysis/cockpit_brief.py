"""Formats the cockpit block as text, for pasting into a chat.

`analysis/brief.py` serves the Copy-analysis mandate and is long by design.
This is the short one: where price sits between its levels, how strong those
levels are, and what the force behind it is doing. That is what a reply needs to
say "wait" or "go", and nothing else earns its place in a message a person has
to read on a phone.

Three things are stated rather than left implied, because each one silently
changes what the numbers mean:

* **How old the levels are, and how far price has moved since.** A level drawn
  two hours and one ATR ago is a different object from one drawn on this bar.
* **That price is inside a zone**, when it is. There is no clean distance in
  either direction from inside a zone, and quoting the next one past it reads as
  more room than exists.
* **Which channels are mined.** Four of the five carry no measured evidence, and
  a list of five names with no marking would imply they are alike.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8))
# `ema_alignment` is Ema.kt's four-state enum, not a sign: 2 is MIXED, and
# printing it raw put a bare "2" in the timeframe column. `confirmed_bias` IS
# a sign, so the two need different maps.
ALIGN = {1: "up", -1: "down", 2: "mixed", 0: "n/a"}
BIAS = {1: "up", -1: "down", 0: "flat"}


def _n(v: Any, dp: int = 2) -> str:
    """Indicators arrive at full float precision; 17 digits of ADX is noise."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return "—" if f != f else f"{f:.{dp}f}"


def _ts(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, MYT).strftime("%Y-%m-%d %H:%M MYT")
    except (TypeError, ValueError):
        return "?"


def _zone_line(z: dict[str, Any], tag: str) -> str:
    if not z:
        return f"  {tag}: none in the cache"
    bits = [f"  {tag}: {z['edge']:.2f}",
            f"({z['dist']:.2f} away, {z['dist_atr']} ATR)",
            f"strength {z['strength']}",
            f"agreed by {z['n_sources']}: {', '.join(z['sources'][:4])}"]
    if z.get("flipped"):
        bits.append(f"** FLIPPED — was {z['was']}, price has broken it **")
    return "  ".join(bits)


def build(block: dict[str, Any]) -> str:
    """The block as a pasteable message. Never raises."""
    if not block or block.get("error"):
        return f"cockpit unavailable: {(block or {}).get('error', 'no data')}"

    L = block.get("levels") or {}
    room = (L.get("room") or {}) if L.get("available") else {}
    out: list[str] = []
    a = out.append

    a(f"=== COCKPIT  {block.get('symbol')}  spot {block.get('spot')} ===")
    a(f"as of {_ts(block.get('as_of_ms'))}")
    a("")

    # --- levels ------------------------------------------------------------
    a("-- ROOM --")
    if not L.get("available"):
        a(f"  levels unavailable: {L.get('why')}")
    else:
        a(f"  levels drawn {L['age_hours']}h ago at {L['snapshot_price']}; "
          f"price has moved {L['drift']:+.2f} ({L['drift_atr']} ATR) since"
          + ("   ** STALE — the zone cache stopped updating **"
             if L.get("stale") else ""))
        a(f"  reference ATR {L.get('reference_atr')}")
        a(_zone_line(room.get("up"), "next resistance"))
        a(_zone_line(room.get("down"), "next support   "))
        # What is BEHIND the first level, which the first level alone cannot
        # say. On BTCUSDT 2026-08-29 the nearest support was 0.76 ATR away at
        # strength 19.2 -- a tight, well-defined stop -- and the next thing of
        # any substance was 22.78 ATR below it. "Risk 0.76 ATR" and "0.76 ATR
        # then nothing" are different trades, and only the ladder tells them
        # apart. Reading it required a separate API call; that is the gap.
        for tag, key in (("above", "ups"), ("below", "downs")):
            rest = (room.get(key) or [])[1:4]
            if rest:
                a(f"  behind it, {tag}: " + " | ".join(
                    f"{r['edge']} ({r['dist_atr']} ATR, str {r['strength']})"
                    for r in rest))
        if room.get("span"):
            a(f"  range {room['span']:.2f} ({room['span_atr']} ATR); "
              f"price sits at {room['position']} of it "
              f"(0 = on support, 1 = on resistance)")
        if room.get("inside"):
            z = room["inside"]
            a(f"  ** price is INSIDE a zone {z['low']}-{z['high']} "
              f"(strength {z['strength']}) — there is no clean distance either "
              f"way until it leaves **")
        a(f"  room-to-obstacle ratio: long {room.get('long_rr')}, "
          f"short {room.get('short_rr')}  (geometry only — says nothing about "
          f"whether price gets there)")
        flip = L.get("flipped") or []
        if flip:
            a(f"  {len(flip)} level(s) have flipped side since being drawn: "
              + ", ".join(str(z["edge"]) for z in flip[:5]))
    a("")

    # --- session map --------------------------------------------------------
    # The intraday framework the owner trades by, as numbers: where the Asia
    # range is, what Europe did to it, whether New York confirmed. Stated as
    # facts — a sweep is not a direction.
    ses = block.get("sessions")
    if isinstance(ses, dict) and not ses.get("error"):
        a("-- SESSION MAP (亚盘范围 / 欧盘扫盘 / 美盘确认) --")
        lv = ses.get("levels") or {}
        w = ses.get("windows_myt") or {}
        a(f"  phase {ses.get('phase')} ({w.get('asia')}/{w.get('europe')}/"
          f"{w.get('us')} MYT) · day {ses.get('day_myt')}"
          + (" · ** WEEKEND — thin flow, not price discovery **"
             if ses.get("weekend") else ""))
        if lv.get("asia_high") is None:
            a("  Asia range: no bars yet today")
        else:
            a(f"  Asia {lv['asia_high']} / {lv['asia_low']}"
              + ("" if lv.get("asia_done") else "  (still building)")
              + f"  range {lv.get('asia_range')}")
        a(f"  prev day {lv.get('prev_day_high')} / {lv.get('prev_day_low')}"
          f" · prev US {lv.get('prev_us_high')} / {lv.get('prev_us_low')}")
        a(f"  state: {ses.get('state')}")
        b = ses.get("bias") or {}
        if isinstance(b, dict):
            lbl = {"long": "看多 LONG", "short": "看空 SHORT",
                   "range": "震荡 RANGE", "undecided": "未定"}.get(b.get("bias"),
                                                                b.get("bias"))
            when = {"asia": "Asia: range by definition",
                    "europe": "欧盘表态 — US verdict later",
                    }.get(b.get("phase"),
                          f"当日判定" if b.get("ready")
                          else f"US verdict at {b.get('verdict_myt','20:30')}")
            a(f"  日内倾向: {lbl}   ({when})")
            for line in (b.get("basis") or [])[:5]:
                a(f"    · {line}")
        us = ses.get("us")
        if us:
            a(f"  US read: {us.get('verdict')} — Europe closed "
              f"{us.get('europe_close')}, US move {us.get('move')} vs "
              f"threshold {us.get('threshold')}")
        for e in (ses.get("events") or [])[-6:]:
            vol = "" if e.get("vol_ok") is None else f" vol_ok={e['vol_ok']}"
            a(f"    {_ts(e.get('ts_ms'))}  {e.get('type')} @ "
              f"{_n(e.get('price'))} (level {_n(e.get('level'))}){vol}")
        a("")

    # --- force -------------------------------------------------------------
    a("-- FORCE --")
    adx = block.get("adx") or {}
    if adx:
        a(f"  ADX {adx.get('now')} slope {adx.get('slope')} "
          f"({'rising' if adx.get('rising') else 'falling'}) — {adx.get('regime')}")
    for row in block.get("alignment") or []:
        a(f"  {row['tf']:>4}  ema {ALIGN.get(row['ema_alignment'], '?'):>5}"
          f"  confirmed {BIAS.get(row['confirmed_bias'], '?'):>4}"
          f"  adx {_n(row['adx'], 1)}  %B {_n(row['percent_b'], 2)}"
          f"  relvol {_n(row['rel_volume'], 2)}")
    s = block.get("alignment_summary") or {}
    if s:
        a(f"  timeframes: {s.get('bullish_tfs')} up / {s.get('bearish_tfs')} down "
          f"/ {s.get('flat_tfs')} flat"
          + ("  (unanimous)" if s.get("unanimous") else ""))
    vol = block.get("volume") or {}
    if vol:
        a(f"  volume {_n(vol.get('relative_to_20bar'), 2)}x the "
          f"{vol.get('period', 20)}-bar average — {vol.get('band')}")
    a("")

    # --- positioning -------------------------------------------------------
    oi = block.get("open_interest") or {}
    fund = block.get("funding") or {}
    if oi.get("available") or fund.get("available"):
        a("-- POSITIONING --")
        if oi.get("available"):
            a(f"  OI {oi.get('quadrant')}: {oi.get('interpretation', '')}".rstrip())
        if fund.get("available"):
            a(f"  funding {fund.get('rate')} vs neutral {fund.get('neutral')} "
              f"-> {fund.get('vs_neutral')}")
        a("")

    # --- when --------------------------------------------------------------
    sess = block.get("session") or {}
    if sess:
        a("-- WHEN --")
        clock = block.get("clock") or {}
        # `session.gold_market_open` is the MT5 broker's gold hours applied to
        # whatever symbol was asked for. On a 24/7 venue it is simply wrong --
        # ETHUSDT read "market CLOSED" on a Saturday while printing hourly bars
        # with real volume. The clock knows which venue this is; the session
        # block does not, so the clock wins here.
        venue_247 = clock.get("venue") == "24/7"
        state = ("open 24/7" if venue_247
                 else "open" if sess.get("gold_market_open") else "CLOSED")
        a(f"  {sess.get('weekday_myt')} {sess.get('bar_myt')}, "
          f"{sess.get('label')} session, market {state}")
        if venue_247 and clock.get("cash_gold_open") is False:
            a(f"  ** spot gold is shut — this contract keeps trading, but what "
              f"you are watching is thin weekend drift, not price discovery **")
        if clock.get("hours") is not None:
            if clock.get("open"):
                a(f"  market closes in {clock['hours']}h "
                  f"({_ts(clock['changes_ms'])})"
                  + ("   ** CLOSING SOON — an intraday position taken now gets "
                     "under 3 hours to work, or carries the weekend gap **"
                     if clock.get("closing_soon") else ""))
            else:
                a(f"  market is SHUT; reopens in {clock['hours']}h "
                  f"({_ts(clock['changes_ms'])}) — every price above is the "
                  f"last print before the close, not a live quote")
        a("")

    # --- channels ----------------------------------------------------------
    ch = block.get("channels") or []
    if ch:
        a("-- CHANNELS (right now) --")
        for c in ch:
            tag = "measured" if c["measured"] else "MINED"
            if c["fired"]:
                a(f"  {c['name']} ({tag}): FIRING {c.get('side','')}")
            else:
                blocking = ", ".join(c["blocking"][:3]) or "—"
                a(f"  {c['name']} ({tag}): quiet; waiting on {blocking}")
        a("")

    a("-- WHAT I WANT BACK --")
    a("A short read in Chinese: wait or enter, which level is the one that")
    a("matters, and what would change your mind. Say plainly if the levels are")
    a("too stale or price is mid-zone to call it.")
    return "\n".join(out)
