"""Assembles `risk_engine.RiskInputs` from this project's database.

`engine/risk_engine.py` is pure and knows nothing about SQLite, pandas or this
repo's column names. This module is the only place the two meet, so the engine
stays testable against hand-built inputs and the adapter stays replaceable when
the feed changes.

**Closed bars only.** Every value below is read off the last CLOSED bar of each
timeframe. The forming bar is display-only, the same CONFIRMED/LIVE split the
whole engine keeps (`docs/OPEN_QUESTIONS.md` Q28) — a wick that looks like a
rejection can fill back in before the bar ends.

**The sim runs on the sim timeframe, the factors run on the anchor.** The Kotlin
uses one 5m series for everything. Here the danger factors read the anchor
timeframe the rule trades (15m for MT5:GOLD), while the double-barrier sim reads
a faster series so it has enough analogous bars to be worth blending. Both are
stated in the returned metadata rather than left implicit.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..indicators import zones as zones_mod
from ..indicators.base import IndicatorConfig
from ..indicators.context import rsi as context_rsi
from ..indicators.registry import compute_indicators
from . import risk_engine as R

log = logging.getLogger(__name__)

# Timeframes that vote in the wick / bias tally, slowest last.
VOTE_TFS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h")

# How many bars the sim searches, and the series it searches. 5m gives ~4-5x the
# analogue count of 15m over the same wall-clock window, which matters because
# SIM_MIN_MATCHES is 8 and a thin match set is dropped rather than blended.
SIM_TF = "5m"
SIM_LOOKBACK_BARS = 6000

# Enough for EMA-200 plus the sim's warm-up and horizon, and small enough that
# compute_indicators over it is a few milliseconds.
FACTOR_TAIL = 1200


def _tail(repo: CandleRepository, symbol: str, tf: str, n: int) -> pd.DataFrame:
    return repo.load_tail(symbol, tf, n)


def _ema(close: np.ndarray, period: int) -> np.ndarray:
    """SMA-seeded EMA series; entries before the seed are NaN.

    `indicators/trend.ema` is the parity-checked implementation and is used for
    every period the registry already computes (7/14/28). EMA 50 and 200 are not
    in the registry — the app has no such lines — so they are computed here with
    the same seeding rule rather than added to the shared registry, which would
    change what every other consumer sees.
    """
    from ..indicators.trend import ema as _trend_ema
    return _trend_ema(np.asarray(close, dtype="float64"), period)


def _zone_pair(db: Database, symbol: str, cfg: IndicatorConfig,
               spot: float) -> tuple[tuple[R.Zone, ...], tuple[R.Zone, ...], int | None]:
    """The newest cached KEY LEVELS snapshot, as engine Zones.

    Zones live on the H1 grid and are never newer than the last closed hour, so
    the staleness is returned rather than hidden — on a 15m anchor the card can
    be up to 59 minutes old and the UI says so.

    **Cached snapshots only, and this is load-bearing.** `snapshot_series` fills
    the cache as a side effect, and on `MT5:GOLD` that is 81,915 H1 bars at ~3 ms
    each — minutes, not milliseconds, inside what is supposed to be a 5-second
    tick. So the cache table is read directly and never written. A cold cache
    simply means no zones this tick, which `data_completeness` already scores
    down; `cli report`/`build_payload` is what populates it.
    """
    s = zones_mod.latest_cached(db, symbol, cfg)
    if s is None:
        return (), (), None
    res = tuple(R.Zone(low=z[0], high=z[1], strength=z[2], sources=z[3])
                for z in s.resistance)
    sup = tuple(R.Zone(low=z[0], high=z[1], strength=z[2], sources=z[3])
                for z in s.support)
    stale_ms = int(time.time() * 1000) - (int(s.ts) + INTERVAL_MS[zones_mod.ANCHOR_TF])
    return res, sup, max(0, stale_ms)


# Bars of open interest the deviation is measured against. The Kotlin's
# OI_REF_PCT is 40, i.e. a 40% deviation saturates the "aggression" term, so the
# baseline has to be a recent mean rather than an all-time one — OI trends over
# months and a long baseline would report a permanent deviation.
OI_BASELINE_BARS = 96

# The ONE grid open interest is stored and read on. Bybit publishes OI on
# 5min/15min/30min/1h/4h; picking per-symbol meant the writer and the reader
# could choose differently and the reader would silently find nothing. OI is a
# slow aggregate, so the grid barely matters — agreeing on it does.
OI_INTERVAL = "15m"

# How far back Bybit will still serve 15m open interest. Measured, not from the
# docs: asking for 200 days on 2026-08-25 returned 16,257 points beginning
# 2026-03-09, i.e. ~170 days. Older OI does not exist at the source any more, so
# it can only be kept by having stored it while it was still being served --
# which is the whole reason this table is never pruned.
OI_RETENTION_DAYS = 170


def _oi_deviation_pct(db: Database, symbol: str, interval: str) -> float | None:
    """Open interest now, as a % deviation from its own recent mean.

    The Kotlin reads `snapshot.oiDeviationPct` from a venue that publishes it.
    Bybit publishes the level, not the deviation, so the baseline is computed
    here: the mean of the last `OI_BASELINE_BARS` points. Returns None when
    there is not enough history, which lowers Confidence rather than reading as
    zero danger — see `risk_engine`'s `_momentum_danger`.
    """
    try:
        rows = db.conn.execute(
            "SELECT oi FROM open_interest WHERE symbol = ? AND interval = ? "
            "ORDER BY ts DESC LIMIT ?",
            (symbol, interval, OI_BASELINE_BARS)).fetchall()
    except Exception as exc:
        log.debug("open interest read failed for %s: %s", symbol, exc)
        return None
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < 12:
        return None
    now = vals[0]                     # DESC, so the newest is first
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return None
    return (now - mean) / mean * 100.0


def build_inputs(
    db: Database,
    symbol: str,
    anchor: str = "15m",
    cfg: IndicatorConfig | None = None,
    spot: float | None = None,
    with_zones: bool = True,
) -> tuple[R.RiskInputs, dict] | tuple[None, dict]:
    """Read the DB and produce engine inputs. Returns ``(inputs, meta)``.

    ``spot`` is the live (forming) price when the caller has one — the Kotlin's
    `snapshot.lastPrice`. It is used ONLY for distance-to-zone and EMA-side
    questions, never to produce a signal; every indicator below still comes off
    closed bars.
    """
    cfg = cfg or IndicatorConfig()
    repo = CandleRepository(db)
    meta: dict = {"symbol": symbol, "anchor": anchor, "sim_tf": SIM_TF}

    df = _tail(repo, symbol, anchor, FACTOR_TAIL)
    if df.empty or len(df) < 30:
        meta["error"] = f"no {anchor} candles for {symbol}"
        return None, meta

    ind = compute_indicators(df, anchor, cfg)
    i = len(df) - 1
    close = df["close"].to_numpy(dtype="float64")
    last_close = float(close[i])
    px = float(spot) if spot and spot > 0 else last_close

    # --- per-timeframe votes ------------------------------------------------
    tf_signals: list[R.TfCandleSignal] = []
    for tf in VOTE_TFS:
        d = _tail(repo, symbol, tf, 400)
        if d.empty or len(d) < 2:
            continue
        f = compute_indicators(d, tf, cfg)
        k = len(d) - 1
        tf_signals.append(R.TfCandleSignal(
            label=tf,
            wick_bias=R.wick_bias(float(d["open"].iloc[k]), float(d["high"].iloc[k]),
                                  float(d["low"].iloc[k]), float(d["close"].iloc[k])),
            relative_volume=_f(f["relative_volume"].iloc[k], 1.0),
            ut_bias=int(_f(f["ut_bias"].iloc[k], 0)),
            confirmed_bias=int(_f(f["confirmed_bias"].iloc[k], 0)),
            usable=bool(f["usable"].iloc[k]) if "usable" in f else len(d) >= 60,
        ))

    # --- the sim series -----------------------------------------------------
    sim = _tail(repo, symbol, SIM_TF, SIM_LOOKBACK_BARS)
    if sim.empty or len(sim) < R.SIM_WARMUP + 60:
        # Fall back to the anchor rather than dropping the sim entirely; fewer
        # analogues, but `SIM_MIN_MATCHES` still decides whether it is used.
        sim = df
        sim_tf = anchor
    else:
        sim_tf = SIM_TF
    meta["sim_tf"] = sim_tf
    sim_close = sim["close"].to_numpy(dtype="float64")
    # RSI directly, NOT compute_indicators. The sim only reads RSI and EMA28, and
    # the full registry over 6,000 bars folds swing structure incrementally
    # (`SwingDetector.kt:93` replaces pivots in place, so it cannot be
    # vectorised) — that one column was ~2 s of a 3.5 s call. Same function the
    # registry uses, so the values are identical.
    sim_rsi = context_rsi(sim_close, cfg.rsi_period)

    # NOTE: zones are attached here for a cold build, but `cached_inputs`
    # re-reads them on every call — see there for why.
    res, sup, stale = ((), (), None)
    if with_zones:
        res, sup, stale = _zone_pair(db, symbol, cfg, px)

    inputs = R.RiskInputs(
        spot=px,
        open_=sim["open"].to_numpy(dtype="float64"),
        high=sim["high"].to_numpy(dtype="float64"),
        low=sim["low"].to_numpy(dtype="float64"),
        close=sim_close,
        ema_7=_f(ind["ema_7"].iloc[i]),
        ema_14=_f(ind["ema_14"].iloc[i]),
        ema_28=_f(ind["ema_28"].iloc[i]),
        ema_50=_last(_ema(close, 50)),
        ema_200=_last(_ema(close, 200)),
        ema_28_series=_ema(sim_close, 28),
        rsi_series=sim_rsi,
        atr=_f(ind["atr_14"].iloc[i]),
        resistance=res,
        support=sup,
        tf_signals=tuple(tf_signals),
        # Open interest IS collected now (one request per cycle, see
        # `watch.Watcher._sync_context`), so the momentum factor's OI term is
        # live whenever there is enough history. Long/short ratio still is not,
        # and `None` there lowers confidence rather than reading as "no danger".
        oi_deviation_pct=_oi_deviation_pct(db, symbol, OI_INTERVAL),
        long_ratio=None,
        short_ratio=None,
        zones_are_stale_by_ms=stale,
    )

    step = INTERVAL_MS[anchor]
    meta.update({
        "last_closed_bar_ms": int(df["open_time"].iloc[i]),
        "last_closed_bar_close": round(last_close, 4),
        "spot": round(px, 4),
        "atr_14": _r(ind["atr_14"].iloc[i], 4),
        "rsi_14": _r(ind["rsi_14"].iloc[i], 1),
        "adx_14": _r(ind["adx_14"].iloc[i], 1),
        "ut_level": _r(ind["ut_level"].iloc[i], 4),
        "ut_bias": int(_f(ind["ut_bias"].iloc[i], 0)),
        "ema_alignment": int(_f(ind["ema_alignment"].iloc[i], 0)),
        "bars": int(len(df)),
        "sim_bars": int(len(sim)),
        "zones_stale_ms": stale,
        "bar_age_ms": int(time.time() * 1000) - (int(df["open_time"].iloc[i]) + step),
    })
    return inputs, meta


def settings_for(tp_atr: float = 5.0, sl_atr: float = 2.5,
                 time_stop_bars: int = 96, anchor: str = "15m",
                 sim_tf: str = SIM_TF,
                 tp_usd: float | None = None,
                 sl_usd: float | None = None) -> R.RiskSettings:
    """A `RiskSettings` whose sim horizon matches the trade being described.

    The Kotlin fixes the horizon at 48 bars of 5m (4 h). The shipped system's
    time stop is 96 anchor bars (24 h at 15m), so a sim that stopped at 4 h would
    report the odds of a *different* trade. The horizon is therefore converted
    into sim-timeframe bars, and the label says what it is.
    """
    horizon_ms = time_stop_bars * INTERVAL_MS[anchor]
    bars = max(1, int(round(horizon_ms / INTERVAL_MS[sim_tf])))
    hours = horizon_ms / 3_600_000
    return R.RiskSettings(
        tp_atr=tp_atr, sl_atr=sl_atr, tp_usd=tp_usd, sl_usd=sl_usd,
        lookback_bars=SIM_LOOKBACK_BARS,
        lookback_label=f"{SIM_LOOKBACK_BARS:,} {sim_tf} bars",
        sim_horizon=bars,
        horizon_label=f"{hours:g}h",
    )


# Bar-derived inputs, keyed by (symbol, anchor, last closed bar). Everything in
# `RiskInputs` except `spot` is a function of CLOSED bars, so it cannot change
# until one closes — but the live loop asks every 5 s. Without this the tick
# spends ~1.5 s per symbol recomputing values that are identical to last time.
# One entry per symbol; a new bar evicts the old one.
_INPUT_CACHE: dict[tuple[str, str], tuple[int, "R.RiskInputs", dict]] = {}


def cached_inputs(
    db: Database, symbol: str, anchor: str, cfg: IndicatorConfig | None,
    spot: float | None,
) -> tuple[R.RiskInputs | None, dict]:
    """`build_inputs`, but recomputed only when a new anchor bar has closed.

    `spot` is swapped in on every call, so the live price still moves the
    zone-distance and EMA-side terms between bars — it is the only input that is
    allowed to, because it is the only one that is not read off a closed bar.
    """
    import dataclasses

    repo = CandleRepository(db)
    key = (symbol, anchor)
    latest = repo.load_tail(symbol, anchor, 1)
    if latest.empty:
        return None, {"symbol": symbol, "anchor": anchor,
                      "error": f"no {anchor} candles for {symbol}"}
    bar_ms = int(latest["open_time"].iloc[-1])

    hit = _INPUT_CACHE.get(key)
    if hit is not None and hit[0] == bar_ms:
        inputs, meta = hit[1], dict(hit[2])
        meta["cached"] = True
    else:
        inputs, meta = build_inputs(db, symbol, anchor, cfg, spot)
        if inputs is None:
            return None, meta
        _INPUT_CACHE[key] = (bar_ms, inputs, meta)
        meta = dict(meta)
        meta["cached"] = False

    px = float(spot) if spot and spot > 0 else inputs.spot
    replace: dict = {}
    if px != inputs.spot:
        replace["spot"] = px
        meta["spot"] = round(px, 4)

    # Zones are re-read every call, not cached with the rest. They live on the H1
    # grid and so change on a schedule of their own, and `cli zones` can fill the
    # cache at any moment — pinning them to the anchor bar meant a freshly built
    # cache stayed invisible, and the page kept saying it was cold, until the
    # next bar closed. One indexed row, so it costs nothing to be right.
    res, sup, stale = _zone_pair(db, symbol, cfg or IndicatorConfig(), px)
    replace.update(resistance=res, support=sup, zones_are_stale_by_ms=stale)
    meta["zones_stale_ms"] = stale

    # Open interest publishes on its own grid and is topped up by the slow loop,
    # so it is re-read like zones rather than pinned to the anchor bar. One
    # indexed query.
    oi_dev = _oi_deviation_pct(db, symbol, OI_INTERVAL)
    replace["oi_deviation_pct"] = oi_dev
    meta["oi_deviation_pct"] = None if oi_dev is None else round(oi_dev, 3)
    if replace:
        inputs = dataclasses.replace(inputs, **replace)
    step = INTERVAL_MS[anchor]
    meta["bar_age_ms"] = int(time.time() * 1000) - (bar_ms + step)
    return inputs, meta


def snapshot(
    db: Database,
    symbol: str,
    anchor: str = "15m",
    cfg: IndicatorConfig | None = None,
    spot: float | None = None,
    settings: R.RiskSettings | None = None,
) -> dict:
    """Build inputs, evaluate, and return a JSON-ready dict for the page."""
    inputs, meta = cached_inputs(db, symbol, anchor, cfg, spot)
    if inputs is None:
        return {"error": meta.get("error", "no data"), **meta}
    s = settings or settings_for(anchor=anchor, sim_tf=meta["sim_tf"])
    snap = R.evaluate(inputs, s)
    out = snap.as_dict()
    out["meta"] = meta
    out["tf_votes"] = [
        {"tf": t.label, "wick": t.wick_bias, "ut_bias": t.ut_bias,
         "bias": t.confirmed_bias, "rel_vol": round(t.relative_volume, 2),
         "usable": t.usable}
        for t in inputs.tf_signals
    ]
    out["zones"] = {
        "resistance": [[z.low, z.high, z.strength, list(z.sources)]
                       for z in inputs.resistance],
        "support": [[z.low, z.high, z.strength, list(z.sources)]
                    for z in inputs.support],
        "stale_ms": inputs.zones_are_stale_by_ms,
    }
    return out


def _f(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def _last(arr) -> float | None:
    a = np.asarray(arr, dtype="float64")
    fin = np.flatnonzero(np.isfinite(a))
    return float(a[fin[-1]]) if fin.size else None


def _r(v, dp=4):
    f = _f(v)
    return None if f is None else round(f, dp)
