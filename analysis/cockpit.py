"""The second screen: how much room is there, and how hard is it pushing.

The chart page answers "what happened". This answers the only question you have
with a position half-open: **wait, or go now.** That decomposes into two things
the chart shows badly because they are numbers, not shapes:

* **Room.** How far to the first thing that will stop this move, each way, in
  dollars AND in ATR. Dollars are what you get paid; ATR is the only unit that
  compares today's 12 points to last month's 12 points. A target 0.3 ATR away is
  not a trade no matter how good the setup looks.
* **Force.** Whether the push behind price is building or fading -- ADX and its
  slope, EMA alignment on every timeframe at once, participation, and where the
  positioning is.

## What this module does and does not compute

Almost nothing here is new. `analysis/context.py` already gathers the momentum,
participation, funding and open-interest layer for the Copy-analysis mandate, and
it is reused verbatim: a second definition of "is ADX rising" that disagreed with
the first would be worse than not having this page. `indicators/zones.py` already
builds the levels and already scores their strength.

What is genuinely added is the **geometry between price and those levels** --
distance, distance in ATR, where in the range price is sitting, and the
reward-to-risk that follows from it. That is the part nobody had computed.

## The staleness rule

`zones.latest_cached` never computes; it reads the cache `cli zones` fills. A
level from three days ago is not a level, so the age of the snapshot is carried
out and shown. A page that renders old levels as though they were current is
worse than a page that says it has none.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..data.db import CandleRepository, Database
from ..indicators import zones
from ..indicators.base import IndicatorConfig
from . import context

log = logging.getLogger(__name__)

# A zone this far from price in ATR terms is not in play for an intraday
# decision; it is still returned, but flagged so the page can fade it.
IN_PLAY_ATR = 2.0
HOUR_MS = 3_600_000


def _split(snap, price: float, atr: float
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                      list[dict[str, Any]]]:
    """Re-classify every zone against the CURRENT price. Above / below / inside.

    This is not the same as trusting the snapshot's own labels, and the
    difference is not cosmetic. A snapshot is built on a CLOSED H1 bar and
    classified at that bar's close; price keeps moving inside the hour. On
    2026-08-28 the cached snapshot called 4637.95 "support" and spot was 4597 --
    price had broken through and the level was, right then, resistance. Reading
    the stored label would have put a broken level under price on a page whose
    entire job is to say what is above and below.

    A zone whose side no longer matches its label is marked `flipped`, which is
    worth seeing in its own right: support that price has fallen through is the
    level a bounce has to fight, and it is usually the first one it reaches.
    """
    above: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    inside: list[dict[str, Any]] = []
    for was, raw in (("resistance", snap.resistance), ("support", snap.support)):
        for z in raw or ():
            try:
                low, high = float(z[0]), float(z[1])
                strength, sources = float(z[2]), z[3]
            except (TypeError, ValueError, IndexError):
                continue
            if price < low:
                side, edge, bucket = "resistance", low, above
            elif price > high:
                side, edge, bucket = "support", high, below
            else:
                side, edge, bucket = "inside", price, inside
            dist = abs(edge - price)
            bucket.append({
                "low": round(low, 2), "high": round(high, 2),
                "mid": round((low + high) / 2.0, 2),
                "edge": round(edge, 2),
                "width": round(high - low, 2),
                "strength": round(strength, 3),
                "sources": list(sources or ()),
                "n_sources": len(sources or ()),
                "dist": round(dist, 2),
                "dist_atr": (round(dist / atr, 2)
                             if atr and atr == atr and atr > 0 else None),
                "in_play": bool(atr and atr > 0 and dist / atr <= IN_PLAY_ATR),
                "side": side,
                "was": was,
                "flipped": bool(side != "inside" and side != was),
            })
    above.sort(key=lambda r: r["dist"])
    below.sort(key=lambda r: r["dist"])
    inside.sort(key=lambda r: -r["strength"])
    return above, below, inside


def _room(res: list[dict[str, Any]], sup: list[dict[str, Any]],
          inside: list[dict[str, Any]], price: float,
          atr: float) -> dict[str, Any]:
    """Distance to the first obstacle each way, and what that implies.

    `position` is where price sits between the nearest support and the nearest
    resistance: 0.0 at support, 1.0 at resistance. It is the one number that
    answers "am I buying at the bottom of the range or the top of it", and it is
    the reason this page exists.

    The two reward:risk figures are geometry, NOT a recommendation -- they say
    how much room there is before the next obstacle, and say nothing at all
    about whether the move will get there.
    """
    # A zone price is standing INSIDE is the nearest obstacle in BOTH
    # directions -- its far edge is what price must clear to leave it. Measured
    # on 2026-08-28: spot 4595.61 sat inside a 4566.82-4637.95 zone of strength
    # 22.2, while the nearest zone entirely above was 4644.60 at strength 2.8.
    # Reporting 4644.60 as "next resistance" pointed at the weaker level 49
    # points away and skipped the far stronger edge 42 points away that price
    # was already pressed against.
    def edges(zs, key, sign):
        out = []
        for z in zs:
            e = z[key]
            d = sign * (e - price)
            if d < 0:
                continue
            out.append({**z, "edge": round(e, 2), "dist": round(d, 2),
                        "dist_atr": (round(d / atr, 2) if atr and atr > 0 else None),
                        "containing": True})
        return out

    ups = sorted(list(res) + edges(inside, "high", 1), key=lambda r: r["dist"])
    dns = sorted(list(sup) + edges(inside, "low", -1), key=lambda r: r["dist"])
    up = ups[0] if ups else None
    dn = dns[0] if dns else None
    span = ((up["edge"] - dn["edge"]) if (up and dn) else None)
    position = None
    if span and span > 0:
        position = round((price - dn["edge"]) / span, 3)
    def rr(reward, risk):
        if not reward or not risk or risk <= 0:
            return None
        return round(reward / risk, 2)
    return {
        "up": up, "down": dn,
        # The full ladder, so the page can draw what is behind the first one.
        "ups": ups[:6], "downs": dns[:6],
        "span": round(span, 2) if span else None,
        "span_atr": round(span / atr, 2) if (span and atr and atr > 0) else None,
        "position": position,
        "long_rr": rr(up["dist"] if up else None, dn["dist"] if dn else None),
        "short_rr": rr(dn["dist"] if dn else None, up["dist"] if up else None),
        # Price sitting INSIDE a zone is its own state, and the most common one
        # to get wrong: there is no clean distance in either direction, and the
        # "room" below is measured past a level price has not left yet. Named
        # rather than folded away, so the page can say so instead of quoting a
        # confident number that skips the zone you are standing in.
        "inside": inside[0] if inside else None,
        "inside_count": len(inside),
    }


def _levels(db: Database, symbol: str, price: float,
            cfg: IndicatorConfig, now_ms: int) -> dict[str, Any]:
    try:
        snap = zones.latest_cached(db, symbol, cfg)
    except Exception as exc:
        log.debug("zone read failed for %s: %s", symbol, exc)
        return {"available": False, "why": f"{type(exc).__name__}: {exc}"}
    if snap is None:
        return {"available": False,
                "why": "no cached zone snapshots — run `cli zones` for this symbol"}
    atr = float(snap.reference_atr)
    if not (atr == atr and atr > 0):          # NaN-safe
        atr = 0.0
    age_h = round((now_ms - int(snap.ts)) / HOUR_MS, 1)
    res, sup, inside = _split(snap, price, atr)
    return {
        "available": True,
        "snapshot_ms": int(snap.ts),
        "snapshot_price": round(float(snap.price), 2),
        # How far price has travelled since the levels were drawn. More useful
        # than the age alone: 3 hours in a dead range is fine, 3 hours through
        # two zones is not.
        "drift": round(price - float(snap.price), 2),
        "drift_atr": (round((price - float(snap.price)) / atr, 2)
                      if atr else None),
        "age_hours": age_h,
        # The cache is written per CLOSED H1 bar, so ~1-2h of age is normal and
        # anything beyond that means the cache stopped being filled.
        "stale": age_h > 3.0,
        "reference_atr": round(atr, 3) if atr else None,
        "resistance": res,
        "support": sup,
        "inside": inside,
        "flipped": [z for z in (res + sup) if z["flipped"]],
        "room": _room(res, sup, inside, price, atr),
    }


def market_clock(now_ms: int, symbol: str = "") -> dict[str, Any]:
    """How long the market has left, or how long until it reopens.

    Added because the page failed a real question. On 2026-08-29 at 03:36 MYT
    the panel said "Saturday · ny session · market open" -- all true, and all
    useless: gold shut 2.4 hours later at 06:01 MYT. Every number on the page
    was about a market that was closing, and nothing said so. "Wait or go" has
    no answer without it, since a position taken then either gets closed inside
    two hours or carries a weekend gap.

    Walks `engine.features.is_gold_session` rather than restating its rules, so
    the countdown cannot disagree with the flag next to it.

    The horizon is 8 days, not 3: gold's open stretch runs Sunday 21:00 UTC to
    Friday 22:00 UTC, so from a Monday morning the next close is ~117 hours
    away. A 3-day walk gave up before reaching it and reported `None` all
    Monday and Tuesday -- the countdown silently vanishing on the two days it
    has least to say is still a countdown that does not work.
    """
    from ..engine.features import is_gold_session

    # WHICH market. `is_gold_session` describes the MT5 broker's gold CFD
    # hours. It was being applied to every symbol, so ETHUSDT read "market
    # CLOSED" on a Saturday morning -- measured 2026-08-29: all three Bybit
    # symbols, XAUUSDT included, printed 94 hourly bars with non-zero volume
    # inside that same "shut" window. The venue never closes; the broker does.
    #
    # For a gold-tracking symbol the cash session is still worth knowing, since
    # a perp whose underlying is shut drifts on thin flow -- but it is reported
    # as `cash_gold_open`, not as the venue being closed.
    mt5 = symbol.upper().startswith("MT5:")
    tracks_gold = mt5 or symbol.upper().startswith("XAU")
    cash_open = bool(is_gold_session(now_ms))

    if not mt5:
        out: dict[str, Any] = {
            "venue": "24/7", "open": True, "hours": None, "changes_ms": None,
            "closing_soon": False,
            "cash_gold_open": cash_open if tracks_gold else None,
        }
        if tracks_gold and not cash_open:
            out["note"] = ("the venue trades through the weekend, but spot gold "
                           "is shut — this is thin drift, not price discovery")
        return out

    step = 300_000
    limit = now_ms + 8 * 86_400_000
    open_now = cash_open
    t = now_ms
    while t < limit and bool(is_gold_session(t)) == open_now:
        t += step
    if t >= limit:
        return {"venue": "broker", "open": open_now, "changes_ms": None,
                "hours": None, "closing_soon": False,
                "cash_gold_open": cash_open}
    hours = (t - now_ms) / HOUR_MS
    return {
        "venue": "broker",
        "open": open_now,
        "changes_ms": int(t),
        "hours": round(hours, 1),
        # Inside this, an intraday position cannot be given room to work.
        "closing_soon": bool(open_now and hours <= 3.0),
        "cash_gold_open": cash_open,
    }


def _spot(db: Database, symbol: str, tf: str = "5m") -> float | None:
    """Newest close available. The forming bar is fine here -- this page is
    explicitly a live read, not a signal input."""
    try:
        df = CandleRepository(db).load_tail(symbol, tf, 2)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])
    except Exception:
        return None


def build(db: Database, symbol: str, cfg: IndicatorConfig | None = None,
          spot: float | None = None, anchor: str = "15m",
          channels: list[dict[str, Any]] | None = None,
          now_ms: int | None = None) -> dict[str, Any]:
    """The whole cockpit block. Never raises -- a dead panel is still a panel."""
    cfg = cfg or IndicatorConfig()
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    price = spot if spot is not None else _spot(db, symbol)

    out: dict[str, Any] = {
        "symbol": symbol,
        "spot": round(price, 2) if price is not None else None,
        "as_of_ms": now_ms,
        "anchor": anchor,
    }
    if price is None:
        out["error"] = f"no candles for {symbol}"
        return out

    out["levels"] = _levels(db, symbol, price, cfg, now_ms)

    # Momentum, participation, funding, OI and session all come from the block
    # the Copy-analysis mandate already reads, unchanged. One definition.
    try:
        ctx = context.gather(db, symbol, anchor=anchor, cfg=cfg, spot=price)
    except Exception as exc:
        log.debug("context gather failed for %s: %s", symbol, exc)
        ctx = {"error": f"{type(exc).__name__}: {exc}"}
    for key in ("session", "adx", "volume", "alignment", "alignment_summary",
                "funding", "open_interest", "atr_percent", "volatility_band",
                "bb_percent_b", "freshness", "sessions", "error"):
        if key in ctx:
            out[key] = ctx[key]

    out["clock"] = market_clock(now_ms, symbol)
    out["channels"] = channels or []
    return out
