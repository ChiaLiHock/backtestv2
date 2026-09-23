"""The CONTEXT block the Copy-analysis mandate reads before anything else.

`analysis/brief.py` formats; this module gathers. Everything here exists because
a rule in the mandate needs it and would otherwise have to answer "數據不足" on a
value the database already holds:

| mandate rule | what it needs | where it comes from |
|---|---|---|
| ADX slope | last 4 ADX readings, not one | `adx_14` off the anchor frame |
| Volume banding | multiple of the 20-bar average | `relative_volume` (period 20, matches) |
| Participation divergence | trend strength vs that band | `directional_score` |
| Funding baseline | rate MINUS the venue's neutral | `funding` table |
| OI four-quadrant | ΔOI against Δprice | `open_interest` table |
| Session rules | which session, and is it a weekend | `engine/features.py` |

**Two things are deliberately absent and are reported as absent**, because
inventing them is worse than admitting them:

* **There is no economic calendar in this project.** No `NEXT IMPORTANT EVENT`
  exists to grade, so the brief says so and points the reader at their own
  verification authority rather than emitting an empty field that reads like
  "nothing is scheduled".
* **A single snapshot has no previous snapshot.** ΔOI is computed from the OI
  SERIES in the database rather than from a remembered prior blob, which is both
  more reliable and available on the first run — but if the series has fewer than
  two points the quadrant is reported as uncomputable rather than guessed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from ..data.db import INTERVAL_MS, CandleRepository, Database, MetaRepository
from ..engine import features as F
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

# The engine's own volume bands. NOT the "below 1.0x is weak" convention — the
# engine has no 1.0 line, and reading one in changes the verdict on every bar
# that sits between 0.8 and 1.2.
VOLUME_BANDS = (
    (1.5, "強勢參與 / strong participation"),
    (1.2, "有參與 / participating"),
    (0.8, "中性偏弱 / neutral-to-weak"),
    (0.5, "明顯偏弱 / clearly weak"),
    (0.0, "極度縮量 / extremely thin"),
)

# ADX below this is a ranging market and the engine grants NO trend at all.
# Above it the directional vote is linear to ADX_FULL, not a threshold.
ADX_RANGING = 20.0
ADX_FULL = 40.0

# Bybit's default perpetual funding is 0.01% per 8h. "Crowded" is a statement
# about the DEVIATION from that, not about the absolute number — at +0.0100% a
# long is paying exactly the venue's neutral rate and nothing is crowded.
FUNDING_BASELINE_PCT = 0.0100
FUNDING_CROWDED_PCT = 0.0200

# Trend-strength above this with weak participation is the divergence the
# mandate asks to be flagged.
TREND_STRONG = 85.0

ADX_SLOPE_BARS = 4


def _band(rel: float | None) -> str:
    if rel is None or rel != rel:
        return "unknown"
    for floor, label in VOLUME_BANDS:
        if rel >= floor:
            return label
    return VOLUME_BANDS[-1][1]


def _adx_regime(adx: float | None) -> str:
    if adx is None or adx != adx:
        return "unknown"
    if adx < ADX_RANGING:
        return "ranging — the engine grants NO trend below 20"
    w = min(1.0, max(0.0, (adx - ADX_RANGING) / (ADX_FULL - ADX_RANGING)))
    return f"trending, directional vote weighted {w * 100:.0f}% (linear 20→40)"


def _f(v, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if x != x else x


def gather(
    db: Database,
    symbol: str,
    anchor: str = "15m",
    cfg: IndicatorConfig | None = None,
    spot: float | None = None,
) -> dict:
    """Everything the mandate's interpretation rules read. Never raises."""
    cfg = cfg or IndicatorConfig()
    repo = CandleRepository(db)
    out: dict = {"symbol": symbol, "anchor": anchor}

    df = repo.load_tail(symbol, anchor, 400)
    if df.empty or len(df) < 30:
        out["error"] = f"no {anchor} candles for {symbol}"
        return out
    ind = compute_indicators(df, anchor, cfg)
    i = len(df) - 1
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bar_ms = int(df["open_time"].iloc[i])

    # --- session -----------------------------------------------------------
    out["session"] = {
        "bar_myt": datetime.fromtimestamp(bar_ms / 1000, MYT).strftime("%Y-%m-%d %H:%M"),
        "weekday_myt": datetime.fromtimestamp(bar_ms / 1000, MYT).strftime("%A"),
        "label": F.session_myt(bar_ms),
        "gold_market_open": bool(F.is_gold_session(bar_ms)),
        "is_weekend_myt": not F.is_weekday_myt(bar_ms),
        # The timing rules are written in MYT and only apply on a weekday with a
        # cash session; the brief states the verdict rather than making the
        # reader re-derive it.
        "timing_rules_apply": bool(F.is_weekday_myt(bar_ms)
                                   and F.is_gold_session(bar_ms)),
    }

    # --- session liquidity map ---------------------------------------------
    # The intraday framework as data: Asia range, Europe sweeps, US
    # confirmation. One definition (engine/session_map.py), read here so the
    # brief and the cockpit cannot disagree about what the Asia high is.
    # The watcher overrides this with its fresher fast-loop copy when it has
    # one; this path is what standalone callers get.
    try:
        from ..engine import session_map as _sm
        minute = repo.load_tail(symbol, "1m", 3 * 1440 + 60)
        out["sessions"] = _sm.build_map(minute, as_of_ms=now_ms)
    except Exception as exc:
        log.debug("session map failed for %s: %s", symbol, exc)

    # --- ADX, with slope ---------------------------------------------------
    adx_series = ind["adx_14"].to_numpy(dtype="float64")
    # float() at the boundary: these are numpy scalars, and a numpy bool
    # derived from them is not JSON-serialisable — which fails at write time,
    # long after the value looked correct.
    tail = [round(float(v), 1) for v in adx_series[-ADX_SLOPE_BARS:] if v == v]
    adx = _f(adx_series[i])
    slope = None
    if len(tail) >= 2:
        slope = round(tail[-1] - tail[0], 2)
    out["adx"] = {
        "now": None if adx is None else round(adx, 1),
        "last_bars": tail,
        "slope": slope,
        "rising": None if slope is None else bool(slope > 0),
        "regime": _adx_regime(adx),
        # An ADX of 20 on the way up and an ADX of 20 on the way down are
        # opposite signals; a single reading cannot tell them apart.
        "note": ("slope over the last "
                 f"{len(tail)} bars of {anchor}" if slope is not None
                 else "slope unknown — fewer than 2 finite ADX readings"),
    }

    # --- volume, with the divergence test ----------------------------------
    rel = _f(ind["relative_volume"].iloc[i])
    dscore = _f(ind["directional_score"].iloc[i], 0.0) or 0.0
    trend_strength = round(abs(dscore) * 100, 1)
    band = _band(rel)
    weak = rel is not None and rel < 1.2
    out["volume"] = {
        "relative_to_20bar": None if rel is None else round(rel, 2),
        "band": band,
        "period": cfg.volume_period,
        "trend_strength": trend_strength,
        "trend_direction": "bullish" if dscore > 0 else "bearish" if dscore < 0 else "flat",
        "participation_divergence": bool(trend_strength > TREND_STRONG and weak),
        "divergence_note": (
            "trend strength is high while participation is not — the score is "
            "likely carried by structure (EMA, UT, multi-timeframe agreement) "
            "rather than by real volume"
            if (trend_strength > TREND_STRONG and weak) else None),
    }

    out["bb_percent_b"] = _f(ind["bb_percent_b"].iloc[i])
    out["atr_percent"] = _f(ind["atr_percent"].iloc[i])
    out["volatility_band"] = int(_f(ind["volatility_band"].iloc[i], 0) or 0)

    # --- multi-timeframe alignment -----------------------------------------
    align = []
    for tf in ("5m", "15m", "30m", "1h", "4h"):
        d = repo.load_tail(symbol, tf, 400)
        if d.empty or len(d) < 2:
            continue
        f = compute_indicators(d, tf, cfg)
        k = len(d) - 1
        align.append({
            "tf": tf,
            "ut_bias": int(_f(f["ut_bias"].iloc[k], 0) or 0),
            "ema_alignment": int(_f(f["ema_alignment"].iloc[k], 0) or 0),
            "confirmed_bias": int(_f(f["confirmed_bias"].iloc[k], 0) or 0),
            "adx": _f(f["adx_14"].iloc[k]),
            "percent_b": _f(f["bb_percent_b"].iloc[k]),
            "rel_volume": _f(f["relative_volume"].iloc[k]),
        })
    biases = [a["confirmed_bias"] for a in align]
    out["alignment"] = align
    out["alignment_summary"] = {
        "bullish_tfs": sum(1 for b in biases if b > 0),
        "bearish_tfs": sum(1 for b in biases if b < 0),
        "flat_tfs": sum(1 for b in biases if b == 0),
        "unanimous": bool(biases) and len({int(np.sign(b)) for b in biases}) == 1,
    }

    meta = MetaRepository(db)

    # --- funding, against the venue's neutral -------------------------------
    try:
        fund = meta.load_funding(symbol, now_ms - 14 * 86_400_000, now_ms)
    except Exception as exc:
        log.debug("funding read failed: %s", exc)
        fund = None
    if fund is not None and len(fund):
        rate_pct = float(fund["rate"].iloc[-1]) * 100.0
        dev = rate_pct - FUNDING_BASELINE_PCT
        out["funding"] = {
            "rate_pct": round(rate_pct, 5),
            "baseline_pct": FUNDING_BASELINE_PCT,
            "deviation_pct": round(dev, 5),
            "crowded_threshold_pct": FUNDING_CROWDED_PCT,
            # The whole point of the baseline: at exactly +0.0100% nobody is
            # crowded, however emphatic a label elsewhere might be.
            "verdict": ("longs crowded" if dev > FUNDING_CROWDED_PCT else
                        "shorts crowded" if dev < -FUNDING_CROWDED_PCT else
                        "neutral vs baseline"),
            "as_of_myt": datetime.fromtimestamp(
                int(fund["funding_time"].iloc[-1]) / 1000, MYT).strftime("%Y-%m-%d %H:%M"),
            "recent_pct": [round(float(r) * 100, 5) for r in fund["rate"].tail(6)],
        }
    else:
        out["funding"] = {"available": False,
                          "why": "no funding rows stored for this symbol"}

    # --- open interest, and the four quadrants ------------------------------
    #
    # OI is STORED on one grid only (`OI_INTERVAL`). Asking for it at the
    # anchor's interval -- which this did -- returns nothing whenever the anchor
    # is not 15m, and the blob then reported "fewer than 2 stored OI points" for
    # BTC and ETH while 16,320 points sat in the table. A wrong stated reason is
    # worse than a missing section: it tells the reader to stop looking.
    #
    # So the stored series is read on its own grid and then folded ONTO the
    # anchor's bars, last-point-in-bar, by the same aligner the chart uses. That
    # also makes the like-for-like comparison the quadrant needs real rather
    # than merely intended: before, a 15m OI move was being set against the
    # anchor's price move, which for a 1h anchor is the exact mismatch the
    # comment below warns about.
    from ..engine.risk_feed import OI_INTERVAL
    from .report import oi_on_bars

    try:
        raw = meta.load_open_interest(
            symbol, OI_INTERVAL, now_ms - 7 * 86_400_000, now_ms)
        span = meta.open_interest_coverage(OI_INTERVAL)
    except Exception as exc:
        log.debug("open interest read failed: %s", exc)
        raw, span = None, []

    folded = None
    if raw is not None and not raw.empty:
        folded = oi_on_bars(
            df["open_time"].to_numpy(dtype="int64"), INTERVAL_MS[anchor],
            raw["ts"].to_numpy(dtype="int64"), raw["oi"].to_numpy(dtype="float64"))

    # The two most recent bars that actually carry a reading, and the SAME two
    # bars' closes -- so a hole in the record shifts both axes together instead
    # of pairing a fresh OI with a stale price.
    pair: list[int] = []
    if folded is not None:
        for k in range(i, -1, -1):
            if folded[k] is not None:
                pair.append(k)
                if len(pair) == 2:
                    break

    total = next((n for sym, _iv, n, _lo, _hi in span if sym == symbol), 0)
    history = {}
    for sym, _iv, n, lo, _hi in span:
        if sym == symbol:
            history = {
                "points_total": int(n),
                "since_myt": datetime.fromtimestamp(lo / 1000, MYT).strftime("%Y-%m-%d"),
                "grid": OI_INTERVAL,
            }

    if len(pair) == 2:
        # Rounded FIRST, then subtracted. A reader who takes the two printed
        # levels and subtracts them must get the printed delta; computing the
        # delta from full precision and printing rounded levels made the blob
        # contradict itself by a cent, which is exactly the kind of internal
        # inconsistency the mandate asks an analyst to stop and flag.
        now_i, prev_i = pair[0], pair[1]
        cur = round(float(folded[now_i]), 2)
        prev = round(float(folded[prev_i]), 2)
        d_oi = round(cur - prev, 2)
        oi_ts = int(df["open_time"].iloc[now_i])
        # Price change over the SAME two bars, so the quadrant compares like
        # with like rather than a 15-minute OI move against a 4-hour price move.
        close = df["close"].to_numpy(dtype="float64")
        d_px = round(float(close[now_i] - close[prev_i]), 2)
        if d_px > 0 and d_oi > 0:
            q = "價漲 + OI 增 = 新多進場（真延續 / genuine continuation）"
        elif d_px > 0 and d_oi < 0:
            q = "價漲 + OI 減 = 空頭回補（彈藥有限，易衰竭 / short covering, limited fuel）"
        elif d_px < 0 and d_oi > 0:
            q = "價跌 + OI 增 = 新空進場（真確認 / genuine confirmation）"
        elif d_px < 0 and d_oi < 0:
            q = "價跌 + OI 減 = 多頭清算（常見於低點附近 / long liquidation）"
        else:
            q = "no move on one axis — quadrant undefined"
        out["open_interest"] = {
            "now": cur, "prev": prev,
            "delta": d_oi,
            "delta_pct": round(d_oi / prev * 100, 3) if prev else None,
            "price_delta": d_px,
            "interval": anchor,
            "quadrant": q,
            "as_of_myt": datetime.fromtimestamp(oi_ts / 1000, MYT).strftime("%Y-%m-%d %H:%M"),
            **history,
        }
    else:
        # Say which of the two genuinely different absences this is, because
        # they call for different responses from a reader.
        if folded is None and total:
            why = (f"open interest is published on a {OI_INTERVAL} grid and the "
                   f"{anchor} anchor is finer than that — folding it onto these "
                   f"bars would repeat one reading and draw a move that was "
                   f"never observed. {total:,} points ARE stored; read the "
                   f"quadrant on {OI_INTERVAL} or coarser.")
        else:
            why = ("fewer than 2 stored OI points — ΔOI cannot be computed, so "
                   "the four-quadrant read is unavailable (not merely uncertain)")
        out["open_interest"] = {"available": False, "why": why, **history}

    # --- freshness ----------------------------------------------------------
    step = INTERVAL_MS[anchor]
    out["freshness"] = {
        "last_closed_bar_ms": bar_ms,
        "bar_age_ms": now_ms - (bar_ms + step),
        "stale": bool((now_ms - (bar_ms + step)) > 3 * step),
    }

    # --- what this project does not have ------------------------------------
    out["absent"] = {
        "economic_calendar": (
            "This project has NO economic calendar and NO event feed. There is no "
            "NEXT IMPORTANT EVENT field to grade because none is collected. If an "
            "event matters for this decision window, verify it externally — do "
            "not read its absence here as 'nothing is scheduled'."),
        "order_book": (
            "No depth data of any kind. Liquidity-wall reasoning has no input; "
            "the KEY LEVELS zones are past defended prices, not resting size."),
        "long_short_ratio": "not collected on this feed",
    }
    return out
