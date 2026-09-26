"""Self-contained interactive HTML report for one run.

No CDN, no network: candles, indicators and trades are embedded directly, so the
file works offline and keeps working after the DB moves on.

Size is kept sane by exploiting a fact the sync layer already guarantees — every
stored series is a uniform grid with zero gaps — so timestamps are encoded as
``t0 + i * step`` instead of being written per bar.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from ..data import mt5
from ..data.db import INTERVAL_MS, CandleRepository, Database, MetaRepository
from ..indicators.base import IndicatorConfig
from ..indicators import zones
from ..indicators.registry import compute_indicators
from ..engine.symbols import SYMBOL_LABEL
from .metrics import summarise

log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).with_name("templates") / "report.html"
COCKPIT_TEMPLATE = Path(__file__).with_name("templates") / "cockpit.html"

# Series embedded per timeframe, with the decimal places each actually needs.
# Every extra column is ~76k more numbers across the five timeframes, so the
# precision is chosen per series rather than defaulted to 2dp everywhere.
OVERLAY_COLUMNS: dict[str, int] = {
    "ema_7": 2,
    "ema_14": 2,
    "ema_28": 2,
    "ut_level": 2,
    "vwap": 2,
    "rsi_14": 1,
    "atr_14": 3,
    "macd": 3,
    "macd_signal": 3,
    "macd_hist": 3,
    # The rest of the app's TECHNICAL STATE card, so hovering a bar can show what
    # the dashboard would have said at that moment rather than only price.
    # `ema_alignment` is an enum (trend.ALIGN_*); the others are plain numbers.
    # UT bias and "VWAP above/below" are derived in the page from close vs the
    # level, exactly as the app does, so they need no column of their own.
    "ema_alignment": 0,
    "adx_14": 1,
    "bb_percent_b": 3,
    "relative_volume": 2,
}


def _clean(values: np.ndarray, dp: int = 2) -> list[Any]:
    """Round and turn NaN into None so JSON stays valid and gaps stay visible."""
    out = np.round(np.asarray(values, dtype="float64"), dp)
    return [None if v != v else float(v) for v in out]


def _sparse(flags: np.ndarray) -> list[int]:
    """Boolean series -> list of set indices.

    UT crosses are ~2% of bars, so storing 76k booleans as an array would be
    almost all zeros. The indices are a fraction of the size.
    """
    return [int(i) for i in np.flatnonzero(np.asarray(flags, dtype=bool))]


# Bars of history computed BEFORE the newest `max_bars`, then discarded. Every
# column in OVERLAY_COLUMNS converges, so a value inside the kept window is
# identical whether the pass started here or at bar zero — `tests/test_live.py`
# pins 1,500 for the same columns and this is a further margin on top. It must
# stay comfortably above the largest lookback any of them uses (the volatility
# and BB-width percentiles, and the 500-bar app window).
SERIES_WARMUP_BARS = 2500


def _series_block(
    repo: CandleRepository,
    symbol: str,
    timeframes: tuple[str, ...],
    cfg: IndicatorConfig,
    max_bars: int | None = None,
) -> dict[str, Any]:
    """One encoded entry per timeframe. Shared by the run and live payload paths.

    ``max_bars`` keeps only the newest N bars of each timeframe. A backtest report
    wants all of them — the trades are spread over the whole panel. A LIVE page
    does not: `MT5:GOLD` holds 81,915 H1 bars back to 2001, and computing over all
    of them took 23 s to produce a 25 MB payload describing a chart nobody
    scrolls to.

    **Warm-up is kept, then thrown away.** Indicators are computed over
    ``max_bars + SERIES_WARMUP_BARS`` and only then sliced, because slicing first
    would restart the path-dependent UT trailing stop and every EMA from a cold
    seed exactly at the window edge — the most-looked-at part of a live chart.
    """
    out: dict[str, Any] = {}
    for tf in timeframes:
        df = (repo.load_tail(symbol, tf, max_bars + SERIES_WARMUP_BARS)
              if max_bars else repo.load(symbol, tf))
        if df.empty:
            continue
        ind = compute_indicators(df, tf, cfg)
        if max_bars and len(df) > max_bars:
            df = df.tail(max_bars).reset_index(drop=True)
            ind = ind.tail(max_bars).reset_index(drop=True)
        step = INTERVAL_MS[tf]
        ot = df["open_time"].to_numpy(dtype="int64")

        # The uniform-grid assumption is load-bearing for the encoding, so it is
        # checked rather than trusted; a gappy series falls back to explicit times.
        uniform = bool(ot.size < 2 or np.all(np.diff(ot) == step))

        entry = {
            "step": step,
            "t0": int(ot[0]),
            "n": int(ot.size),
            "o": _clean(df["open"].to_numpy()),
            "h": _clean(df["high"].to_numpy()),
            "l": _clean(df["low"].to_numpy()),
            "c": _clean(df["close"].to_numpy()),
        }
        if not uniform:
            entry["t"] = [int(v) for v in ot]
        for col, dp in OVERLAY_COLUMNS.items():
            entry[col] = _clean(ind[col].to_numpy(), dp)
        entry["v"] = _clean(df["volume"].to_numpy(), 0)
        # This timeframe's OWN UT crosses, stored sparsely.
        entry["ut_buy"] = _sparse(ind["ut_buy"].to_numpy())
        entry["ut_sell"] = _sparse(ind["ut_sell"].to_numpy())
        # Set by the live feed once a forming bar is appended; the static report
        # only ever holds closed bars, so it starts null.
        entry["forming_t"] = None
        out[tf] = entry
    return out


def oi_on_bars(
    opens: np.ndarray, step: int, ts: np.ndarray, vals: np.ndarray
) -> list[Any] | None:
    """Open interest per bar, or None when the bars are finer than the OI grid.

    Shared by the full payload build and the live tick. Two copies of this
    arithmetic would be two chances to disagree about which bar a point belongs
    to, and the disagreement would surface as an OI line that visibly jumps the
    moment a rebuilt payload replaced the patched one.
    """
    from ..engine.risk_feed import OI_INTERVAL

    n = int(len(opens))
    if step < INTERVAL_MS[OI_INTERVAL] or n == 0:
        return None
    opens = np.asarray(opens, dtype="int64")
    # Which bar each OI point falls in. searchsorted handles a gappy grid the
    # same as a uniform one, so this does not rely on the bars being evenly
    # spaced.
    idx = np.searchsorted(opens, ts, side="right") - 1
    ok = (idx >= 0) & (idx < n)
    # A point past a bar's CLOSE belongs to no drawn bar. Bounding by the next
    # bar's open instead would be the same thing only on a gapless grid, and a
    # gappy grid is exactly the case `_series_block` sends explicit times for --
    # there, a point in the missing stretch would be dragged onto the bar before
    # the gap and drawn as if it had been observed there.
    ok &= ts < opens[np.clip(idx, 0, n - 1)] + step
    out: list[Any] = [None] * n
    for i, v in zip(idx[ok], vals[ok]):          # ascending ts, so last wins
        out[int(i)] = round(float(v), 3)
    return out


def _attach_oi(db: Database, symbol: str, series: dict[str, Any]) -> None:
    """Put stored open interest on each timeframe's bar grid, in place.

    Three decisions worth stating, because each has a plausible-looking wrong
    answer:

    **A bar's OI is the LAST point inside it.** Open interest is a level -- the
    number of contracts outstanding right now -- not a flow. Summing it across a
    bar would invent size that never existed; averaging it would smear the
    moment OI actually turned, which is the one thing the pane exists to show.

    **Timeframes finer than the stored grid get nothing, not a forward fill.**
    OI is stored on `OI_INTERVAL` (15m). Repeating one 15m reading across three
    5m bars would draw a flat line indistinguishable from genuinely flat OI --
    a fabricated observation, and worse than an admitted gap.

    **Missing bars stay None.** The series is deliberately left holey where the
    watcher was not running, so a gap in the record reads as a gap rather than
    as a straight line between two distant points.
    """
    from ..engine.risk_feed import OI_INTERVAL

    if not series:
        return
    step_oi = INTERVAL_MS[OI_INTERVAL]
    lo = min(b["t0"] for b in series.values())
    # The last bar's CLOSE, not its open: a 4h bar that opened at 12:00 still
    # wants the OI printed at 15:45, and stopping at the open silently dropped
    # up to a full bar of the newest data on every coarse timeframe.
    hi = max(b["t0"] + b["n"] * b["step"] for b in series.values())

    try:
        df = MetaRepository(db).load_open_interest(
            symbol, OI_INTERVAL, lo - step_oi, hi)
    except sqlite3.Error as exc:               # a missing table must not kill the page
        log.debug("open interest unavailable for %s: %s", symbol, exc)
        df = None

    if df is None or df.empty:
        for blk in series.values():
            blk["oi"] = None
        return

    ts = df["ts"].to_numpy(dtype="int64")
    vals = df["oi"].to_numpy(dtype="float64")

    for blk in series.values():
        n, step = blk["n"], blk["step"]
        opens = (np.asarray(blk["t"], dtype="int64") if "t" in blk
                 else blk["t0"] + np.arange(n, dtype="int64") * step)
        blk["oi"] = oi_on_bars(opens, step, ts, vals)


def _ut_events(series: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    """Cross-timeframe UT crosses as timestamps.

    So any chart can place another timeframe's signal on whichever of its own
    bars contains it. This is the multi-timeframe confluence view: seeing 4H, 1H
    and 15m flip together is a different piece of information from any one alone.
    """
    out: dict[str, dict[str, list[int]]] = {}
    for tf, entry in series.items():
        step, t0 = entry["step"], entry["t0"]
        times = entry.get("t")

        def at(i: int, _t=times, _t0=t0, _s=step) -> int:
            return _t[i] if _t else _t0 + i * _s

        out[tf] = {
            "buy": [at(i) for i in entry["ut_buy"]],
            "sell": [at(i) for i in entry["ut_sell"]],
        }
    return out


def build_payload(
    db: Database,
    run_id: str,
    timeframes: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h"),
    indicator_config: IndicatorConfig | None = None,
) -> dict[str, Any]:
    cfg = indicator_config or IndicatorConfig()
    repo = CandleRepository(db)

    run = db.conn.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise SystemExit(f"no run {run_id!r} — list them with `cli runs`")

    strategy = yaml.safe_load(run["config_yaml"])
    symbol = run["symbol"]

    trades = pd.read_sql_query(
        "SELECT * FROM trades WHERE run_id = ? ORDER BY entry_time",
        db.conn, params=(run_id,),
    )

    series = _series_block(repo, symbol, timeframes, cfg)
    _attach_oi(db, symbol, series)
    ut_events = _ut_events(series)

    trade_rows = []
    for _, t in trades.iterrows():
        trade_rows.append({
            "id": t["trade_id"],
            "side": t["side"],
            "signal": t["signal_id"],
            "entry_time": int(t["entry_time"]),
            "entry_price": round(float(t["entry_price"]), 2),
            "exit_time": int(t["exit_time"]) if pd.notna(t["exit_time"]) else None,
            "exit_price": round(float(t["exit_price"]), 2) if pd.notna(t["exit_price"]) else None,
            "reason": t["exit_reason"],
            "net": round(float(t["net_pnl"]), 2),
            "gross": round(float(t["gross_pnl"]), 2),
            "fees": round(float(t["fees"]), 2),
            "mae": round(float(t["mae"]), 2),
            "mfe": round(float(t["mfe"]), 2),
            "bars": int(t["bars_held"]),
            "ambiguous": int(t["ambiguous"]),
        })

    # Stats come from the raw DB rows, not the trimmed display dicts — the
    # display shape renames columns for the UI and would silently drop fields.
    stats = summarise(trades.to_dict("records")) if len(trades) else {"trades": "0"}

    by_signal: list[dict[str, Any]] = []
    if trade_rows:
        tdf = pd.DataFrame(trade_rows)
        for name, g in tdf.groupby("signal"):
            by_signal.append({
                "signal": name,
                "n": int(len(g)),
                "win_pct": round(float((g["net"] > 0).mean() * 100), 1),
                "net": round(float(g["net"].sum()), 2),
                "gross": round(float(g["gross"].sum()), 2),
            })
        by_signal.sort(key=lambda r: -r["n"])

    return {
        "run_id": run_id,
        "symbol": symbol,
        "zones": _zone_block(db, symbol, cfg),
        "my_trades": _broker_trades(db, symbol),
        "strategy_name": strategy.get("name", "?"),
        "strategy_timeframe": run["timeframe"],
        "config_hash": run["config_hash"],
        "config_yaml": run["config_yaml"],
        # Position size, so the chart can price a measured move in dollars. A
        # cross-symbol book has a different qty per symbol and the readout would
        # otherwise be silently wrong on two of the three.
        "qty": float((strategy.get("sizing") or {}).get("qty", 1.0)),
        "start_ms": int(run["start_ms"]),
        "end_ms": int(run["end_ms"]),
        "timeframes": [tf for tf in timeframes if tf in series],
        "series": series,
        "ut_events": ut_events,
        "trades": trade_rows,
        "stats": stats,
        "by_signal": by_signal,
        # A static backtest has only the measured rule -- the mined channels are
        # a live-page feature. Empty rather than absent so the page can test one
        # key instead of branching on which payload it was handed.
        "channels": [],
    }


def build_live_payload(
    db: Database,
    symbol: str,
    timeframes: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h"),
    indicator_config: IndicatorConfig | None = None,
    max_bars: int = 1500,
    strategy_name: str = "live",
    strategy_timeframe: str = "15m",
    signals: list[dict[str, Any]] | None = None,
    config_yaml: str = "",
    config_hash: str = "",
    qty: float = 1.0,
    signal3_live_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The same page payload, with no backtest behind it.

    `build_payload` starts from a row in `runs` and fills the trades panel from
    that run's `trades`. There is no run here and there is not meant to be: the
    live page answers "what is true now", and re-walking 166 days of history every
    few minutes to answer it was the cost this replaces.

    What fills the trades panel instead is **the signals the rule actually
    emitted** (`reports/signals.jsonl`, `would_enter` rows). Those are real
    decisions taken at a real time, which is strictly better evidence than a
    simulated trade -- and it means the chart's `signal` rail keeps working and
    now shows live entries rather than backtested ones.

    `run_id` is `"live:<newest bar>"` rather than a constant, because the page
    swaps a payload in when that string changes and a constant would freeze it.
    """
    cfg = indicator_config or IndicatorConfig()
    repo = CandleRepository(db)

    series = _series_block(repo, symbol, timeframes, cfg, max_bars=max_bars)
    if not series:
        raise SystemExit(f"no candles for {symbol} on {', '.join(timeframes)}")
    _attach_oi(db, symbol, series)
    ut_events = _ut_events(series)
    window_start = min(blk["t0"] for blk in series.values())

    trade_rows = _signal_trades(db, signals or [], symbol, cfg)
    anchor_tf = strategy_timeframe if strategy_timeframe in series else next(iter(series))
    entry = series[anchor_tf]
    if entry.get("t"):
        newest = entry["t"][-1]
    else:
        newest = entry["t0"] + (entry["n"] - 1) * entry["step"]
    oldest = min(blk["t0"] for blk in series.values())

    # Bound to names first: the channel summary needs the same lists the rail
    # draws from, and recomputing them for the table would let the number in
    # the panel drift from the markers on the chart.
    s2_rows = _signal2_trades(db, symbol, cfg, since_ms=window_start)
    s3_rows = _signal3_trades(db, symbol, cfg, since_ms=window_start,
                              live_events=signal3_live_events)
    s4_block = _signal4_block(db, symbol, cfg, since_ms=window_start)
    s5_rows = _signal5_trades(db, symbol, cfg, since_ms=window_start)
    s7_rows = _signal7_trades(db, symbol, cfg, since_ms=window_start)

    return {
        "run_id": f"live:{newest}",
        "live_mode": True,
        "symbol": symbol,
        "zones": _zone_block(db, symbol, cfg, compute=False, since_ms=window_start),
        "my_trades": _broker_trades(db, symbol),
        "signal2": s2_rows,
        "signal3": s3_rows,
        **s4_block,
        "signal5": s5_rows,
        "signal7": s7_rows,
        "channels": _channel_summary(trade_rows, s2_rows, s3_rows,
                                     s4_block.get("signal4") or [], s5_rows,
                                     s7_rows,
                                     now_ms=newest, price=_last_close(entry)),
        "strategy_name": strategy_name,
        "strategy_timeframe": strategy_timeframe,
        "config_hash": config_hash,
        "config_yaml": config_yaml,
        "qty": float(qty),
        "start_ms": int(oldest),
        "end_ms": int(newest),
        "timeframes": [tf for tf in timeframes if tf in series],
        "series": series,
        "ut_events": ut_events,
        "trades": trade_rows,
        "stats": _live_stats(trade_rows, signals or [], symbol),
        "by_signal": [],
    }


def _signal_trades(db: Database, signals: list[dict[str, Any]], symbol: str,
                   cfg: IndicatorConfig) -> list[dict[str, Any]]:
    """Emitted signals, walked forward to their actual outcome.

    A signal records a decision, not a result. `engine/signal_outcomes.resolve`
    supplies the result: it prices the fill at the NEXT bar's open, re-derives
    the bracket from that fill, and walks 1-minute bars until the target, the
    stop or the 24-hour limit is touched. Ties are losses.

    A signal whose 24 hours have not elapsed stays `resolved: False` with net 0 —
    which means UNKNOWN, not flat, and `summarise` counts it separately rather
    than folding a running position into a win rate.
    """
    from ..engine.signal_outcomes import resolve
    from ..engine.live_signal import RuleConfig

    try:
        rows = resolve(db, signals, symbol, RuleConfig(), cfg)
    except Exception as exc:
        log.warning("could not resolve signal outcomes for %s: %s", symbol, exc)
        return []
    # The gate's numbers ride along so the panel can show WHY a signal was let
    # through or held back, next to what it then did.
    gates = {int(r["bar_open_ms"]): ((r.get("extras") or {}).get("gate") or {})
             for r in signals if r.get("kind") != "link"}
    for t in rows:
        g = gates.get(t.get("signal_bar"), {})
        t["danger"] = g.get("danger")
        t["confidence"] = g.get("confidence")
        t["vetoes"] = g.get("vetoes") or []
    return rows


DAY_MS = 86_400_000
CHANNEL_WINDOWS = (("7d", 7), ("30d", 30), ("90d", 90))


def _tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Settled count, wins, win%, net for one bag of trades.

    Two rules, both of which cost the channel a flattering number:

    * **Unresolved trades stay out of the denominator.** A fire from an hour ago
      has no outcome. Counting it would report it as a loss and make the win
      rate a function of how recently you looked -- it is reported as `open`.
    * **Zero is not a win.** `net > 0` decides, so a tie or an exact-scratch
      exit lands with the losses, the rule the brackets were measured under.
    """
    settled = [r for r in rows if r.get("resolved") is not False]
    wins = sum(1 for r in settled if float(r.get("net") or 0.0) > 0)
    return {
        "n": len(settled),
        "open": len(rows) - len(settled),
        "wins": wins,
        "win_pct": round(100.0 * wins / len(settled), 1) if settled else None,
        "net": round(sum(float(r.get("net") or 0.0) for r in settled), 2),
    }


def _last_close(block: dict[str, Any]) -> float:
    """Newest close in a series block, for pricing the round-trip cost."""
    for px in reversed(block.get("c") or []):
        if px is not None:
            try:
                return float(px)
            except (TypeError, ValueError):
                break
    return 0.0


def _breakeven(tp: float, sl: float, price: float,
               bps: float = 0.85) -> float | None:
    """Win rate at which a bracket breaks even, as a percentage.

    A win nets `tp - cost` and a loss costs `sl + cost`, so the balance point is
    ``(sl + cost) / (tp + sl)``. This is the number a win rate has to be read
    against, and it is NOT 50%: at TP15/SL25 it is 63.5%, so signal 4 winning
    66% is a far thinner edge than signal 2 winning 60% at TP25/SL25.
    """
    try:
        cost = float(price) * float(bps) / 1e4
        span = (float(tp) - cost) + (float(sl) + cost)
        if span <= 0:
            return None
        return round(100.0 * (float(sl) + cost) / span, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _channel_summary(rule: list[dict[str, Any]],
                     s2: list[dict[str, Any]],
                     s3: list[dict[str, Any]],
                     s4: list[dict[str, Any]],
                     s5: list[dict[str, Any]],
                     s7: list[dict[str, Any]] | None = None,
                     now_ms: int = 0,
                     price: float = 0.0) -> list[dict[str, Any]]:
    """One record per signal channel, with rolling windows for the hover card.

    The whole-window number answers "is this pattern any good"; it cannot answer
    "has it stopped working *lately*", which is the question that actually
    decides whether to keep watching a mined channel. So each channel also
    carries 7d / 30d / 90d tallies, sliced by ENTRY time.

    Everything here is the CHART WINDOW, not the fitted period, and `measured`
    rides along so the page can keep saying which one of these channels
    carries real evidence and which do not.
    """
    brackets = {
        "rule":    (25.0, 25.0),
        "signal2": (25.0, 25.0),
        "signal3": None,                  # zone-width structural, no fixed pair
        "signal4": (15.0, 25.0),          # asymmetric -- break-even 63.5%
        "signal5": (10.0, 10.0),
    }
    out: list[dict[str, Any]] = []
    for name, rows, measured in (("rule", rule, True), ("signal2", s2, False),
                                 ("signal3", s3, False), ("signal4", s4, False),
                                 ("signal5", s5, False), ("signal7", s7, False)):
        rows = rows or []
        if brackets.get(name) is not None:
            tp, sl = brackets[name]
            bracket_txt: str | None = f"TP {tp:g} / SL {sl:g}"
            breakeven = _breakeven(tp, sl, price) if price else None
        else:
            # Signals 3/6/7 use structural brackets (zone width, Asia
            # range) — no single (tp, sl) pair exists, and quoting a
            # break-even for one would be invented precision.
            bracket_txt = None
            breakeven = None
        rec: dict[str, Any] = {
            "signal": name,
            "measured": measured,
            "bracket": bracket_txt
            or ("structural (zone-width)" if name == "signal3"
                else "structural (ExpD gate)" if name == "signal7"
                else "structural (range-based)"),
            "breakeven": breakeven,
        }
        rec.update(_tally(rows))
        # Sliced on entry, not exit: a trade belongs to the day it was taken.
        windows: dict[str, Any] = {}
        if now_ms:
            for label, days in CHANNEL_WINDOWS:
                cut = int(now_ms) - days * DAY_MS
                windows[label] = _tally(
                    [r for r in rows if int(r.get("entry_time") or 0) >= cut])
        rec["windows"] = windows
        out.append(rec)
    return out


def _signal2_trades(db: Database, symbol: str, cfg: IndicatorConfig,
                    since_ms: int | None = None) -> list[dict[str, Any]]:
    """Signal 2's whole firing history in the window, walked to its outcome.

    Recomputed from candles rather than read from `signals2.jsonl`, because the
    log only starts when the watcher does and the point of putting this on the
    rail is to see the pattern's *history* -- a lane that begins at the last
    restart would say nothing about whether the pattern is worth watching.

    The fire test comes from `signal2.fire_mask`, the same function the live
    panel uses, so a marker here and a firing panel can never disagree.

    Only bars whose 24 hours have fully elapsed get an outcome; a fire from an
    hour ago is returned with `resolved: False` and net 0, which means UNKNOWN
    and is drawn hollow rather than counted as a flat trade.
    """
    from ..engine.signal2 import Signal2Config, fire_mask
    from ..tools.validate_rule import walk_forward

    s2 = Signal2Config()
    if symbol != s2.symbol:
        return []
    try:
        repo = CandleRepository(db)
        df = repo.load(s2.symbol, s2.anchor).reset_index(drop=True)
        if df.empty:
            return []
        ind = compute_indicators(df, s2.anchor, cfg)
        fires = fire_mask(df, ind, s2)

        ot = df["open_time"].to_numpy(dtype="int64")
        o = df["open"].to_numpy(dtype="float64")
        if since_ms is not None:
            fires = fires & (ot >= int(since_ms))

        path = repo.load(s2.symbol, "1m").reset_index(drop=True)
        pt = path["open_time"].to_numpy(dtype="int64")
        ph = path["high"].to_numpy(dtype="float64")
        pl = path["low"].to_numpy(dtype="float64")
        pc = path["close"].to_numpy(dtype="float64")
        horizon = s2.time_stop_bars * INTERVAL_MS[s2.anchor]
        newest_path = int(pt[-1]) if pt.size else 0

        out: list[dict[str, Any]] = []
        for i in np.flatnonzero(fires):
            if i + 1 >= len(df):
                continue                      # the fill bar has not opened yet
            entry = float(o[i + 1])
            start = int(ot[i + 1])
            sl, tp = s2.bracket(entry)
            done = newest_path >= start + horizon
            reason, px, xt, _amb = walk_forward(
                pt, ph, pl, pc, start, entry, tp, sl, False, horizon)
            if reason == "nodata":
                continue
            resolved = done or reason in ("tp", "sl")
            net = ((entry - float(px)) - entry * 0.85 / 1e4) if resolved else 0.0
            out.append({
                "signal_bar": int(ot[i]),
                "entry_time": start,
                "entry_price": round(entry, 4),
                "exit_time": int(xt) if resolved else None,
                "exit_price": round(float(px), 4) if resolved else None,
                "reason": reason if resolved else "open",
                "resolved": bool(resolved),
                "net": round(net, 2),
                "sl": round(sl, 4), "tp": round(tp, 4),
            })
        return out
    except Exception as exc:                  # a chart extra must never be fatal
        log.warning("could not build signal2 trades for %s: %s", symbol, exc)
        return []


def _signal3_trades(db: Database, symbol: str, cfg: IndicatorConfig,
                    since_ms: int | None = None,
                    live_events: list[dict[str, Any]] | None = None
                    ) -> list[dict[str, Any]]:
    """Signal 3 v2's firing history: the sweep/fake-break pattern, walked
    to its outcome over the chart window — MERGED with any live-fired
    events that the walker hasn't caught up to yet.

    The walker only sees CLOSED 15m bars, so a trade the live evaluator
    fired mid-bar is invisible to it for up to 15 minutes. ``live_events``
    (from Signal3.events()) is merged in so the trades panel shows the
    trade as `open` the moment it fires, not a quarter-hour later.
    XAUUSDT only (the swing framework is a gold framework).
    """
    from ..engine.signal3 import Signal3Config
    import backtest.tools.backtest_signal3 as _bt

    s3 = Signal3Config()
    if symbol != s3.symbol:
        return []
    try:
        repo = CandleRepository(db)
        d1 = repo.load(symbol, "1m").reset_index(drop=True)
        if d1.empty:
            return []
        f15 = _bt.fold(d1, 900_000)
        pt = d1["open_time"].to_numpy(dtype="int64")
        ph = d1["high"].to_numpy(dtype="float64")
        pl = d1["low"].to_numpy(dtype="float64")
        pc = d1["close"].to_numpy(dtype="float64")
        _bt._po = d1["open"].to_numpy(dtype="float64")
        trades = _bt.run_sweep_trades(f15, pt, ph, pl, pc)
        out = []
        for t in trades:
            if since_ms is not None and t["entry_time"] < int(since_ms):
                continue
            out.append({
                "signal_bar": t["entry_time"] - 900_000,
                "entry_time": t["entry_time"],
                "entry_price": round(t["entry_price"], 4),
                "exit_time": None,
                "exit_price": None,
                "reason": t["reason"],
                "resolved": t["resolved"],
                "net": t["net"],
                "sl": round(t.get("sl", 0), 4),
                "tp": round(t.get("tp", 0), 4),
                "side": t["side"],
                "event_type": t.get("event_type"),
            })

        # --- merge live-fired events (real-time, may differ from walker) ----
        # The walker detects the pattern at bar close with the bar's open
        # as entry; the live evaluator detects in real-time with spot as
        # entry and a fresher TP swing. When both fire on the same
        # bar_open_ms, they are the SAME trade seen by two engines — the
        # LIVE version replaces the walker's because its entry price is
        # what the signal actually meant at the moment it fired.
        if live_events:
            from ..tools.validate_rule import walk_forward
            from ..engine.signal3 import MAX_RR
            live_by_key: dict[tuple[int, str], dict[str, Any]] = {}
            for ev in live_events:
                # Two shapes arrive here: in-memory events carry an
                # "s3:<symbol>@..." id; jsonl log rows (evaluation
                # outputs, replayed after a restart) carry no id but DO
                # carry symbol. Either is acceptable — the (bar_open_ms,
                # side) key below is the real identity.
                ev_id = str(ev.get("id") or "")
                ev_sym = str(ev.get("symbol") or "")
                if ev_id and not ev_id.startswith(f"s3:{symbol}") \
                        and ev_sym and ev_sym != symbol:
                    continue
                entry_ms = int(ev.get("bar_open_ms") or 0)
                side = str(ev.get("side") or "")
                if entry_ms <= 0:
                    continue
                entry_px = float(ev.get("entry_ref") or 0)
                ev_sl = float(ev.get("sl") or 0)
                ev_tp = float(ev.get("tp") or 0)
                # Resolve the live row against real 1m price, exactly as
                # the walker resolves its own: a live-fired (or replayed)
                # event that has since reached its TP/SL or 24h limit
                # must NOT sit "open" forever overwriting the walker's
                # settled row. Still-running trades stay open.
                reason_l = "open"
                exit_px = exit_ms = None
                resolved_l = False
                net_l = 0.0
                if entry_px > 0 and ev_sl > 0 and ev_tp > 0:
                    is_long = side == "long"
                    # A pre-cap event can carry a TP beyond MAX_RR; the
                    # cap is applied at emission now, but replayed rows
                    # predate it — truncate the same way before judging.
                    if abs(ev_tp - entry_px) > MAX_RR * abs(entry_px - ev_sl):
                        ev_tp = entry_px + (MAX_RR * abs(entry_px - ev_sl)) \
                            * (1 if is_long else -1)
                    reason_l, px_l, xt_l, _amb = walk_forward(
                        pt, ph, pl, pc, entry_ms, entry_px, ev_tp, ev_sl,
                        is_long, _bt.HORIZON)
                    if reason_l == "nodata":
                        reason_l = "open"
                    else:
                        done_l = (reason_l in ("tp", "sl")
                                  or int(pt[-1]) >= entry_ms + _bt.HORIZON)
                        if done_l:
                            reason_l = reason_l if reason_l in ("tp", "sl") \
                                else "timeout"
                            exit_px = round(float(px_l), 4)
                            exit_ms = int(xt_l)
                            resolved_l = True
                            gross = ((float(px_l) - entry_px) if is_long
                                     else (entry_px - float(px_l)))
                            net_l = round(gross - entry_px * 0.85 / 1e4, 2)
                live_by_key[(entry_ms, side)] = {
                    "signal_bar": entry_ms,
                    "entry_time": entry_ms,
                    "entry_price": round(entry_px, 4),
                    "exit_time": exit_ms,
                    "exit_price": exit_px,
                    "reason": reason_l,
                    "resolved": resolved_l, "net": net_l,
                    "sl": round(ev_sl, 4),
                    "tp": round(ev_tp, 4),
                    "side": side,
                    "event_type": ev.get("event_type"),
                    "live": True,
                }
            # replace walker rows that collide with a live row. The key is
            # tried on BOTH timestamps: the walker's entry is the first 1m
            # open after the signal bar closes, the live event stamps the
            # signal bar itself — same trade, two clocks, and a single-key
            # match let today's 11:11 fire vanish when the walker moved.
            live_by_signal_bar: dict[tuple[int, str], dict[str, Any]] = {
                (int(r["signal_bar"]), r["side"]): r
                for r in live_by_key.values()}
            merged: list[dict[str, Any]] = []
            for r in out:
                key = (r["entry_time"], r.get("side"))
                skey = (r.get("signal_bar") or 0, r.get("side"))
                if key in live_by_key:
                    merged.append(live_by_key.pop(key))
                elif skey in live_by_signal_bar:
                    hit = live_by_signal_bar.pop(skey)
                    live_by_key.pop((hit["entry_time"], hit["side"]), None)
                    merged.append(hit)
                else:
                    merged.append(r)
            # append any live-only rows (walker hasn't caught up)
            merged.extend(live_by_key.values())
            merged.sort(key=lambda r: r["entry_time"])
            out = merged
        return out
    except Exception as exc:
        log.warning("could not build signal3 trades for %s: %s", symbol, exc)
        return []


def _signal4_block(db: Database, symbol: str, cfg: IndicatorConfig,
                   since_ms: int | None = None) -> dict[str, Any]:
    """Signal 4's firing history plus the side it currently trades.

    Returns a dict rather than a list because the rail needs the SIDE too: this
    signal is refitted weekly and may be long one week and short the next, so
    the marker direction cannot be hard-coded the way signals 2 and 3 can.
    """
    from ..engine.signal4 import (SL_USD, TIME_STOP_BARS, TP_USD,
                                  Signal4Config, build_features, bracket,
                                  entry_is_weekday, fire_mask)
    from ..tools.validate_rule import walk_forward

    s4 = Signal4Config.load()
    if s4 is None or not s4.conditions or symbol != s4.symbol:
        return {"signal4": [], "signal4_side": "long"}
    try:
        feat = build_features(db, s4.symbol, s4.anchor, cfg)
        if feat.empty:
            return {"signal4": [], "signal4_side": s4.side}
        step = INTERVAL_MS[s4.anchor]
        ot = feat["open_time"].to_numpy(dtype="int64")
        fires = fire_mask(feat, s4.conditions) & entry_is_weekday(ot, step)
        if since_ms is not None:
            fires = fires & (ot >= int(since_ms))

        o = feat["open"].to_numpy(dtype="float64")
        repo = CandleRepository(db)
        path = repo.load(s4.symbol, "1m").reset_index(drop=True)
        pt = path["open_time"].to_numpy(dtype="int64")
        ph = path["high"].to_numpy(dtype="float64")
        pl = path["low"].to_numpy(dtype="float64")
        pc = path["close"].to_numpy(dtype="float64")
        horizon = TIME_STOP_BARS * step
        newest = int(pt[-1]) if pt.size else 0
        long = s4.side == "long"

        out: list[dict[str, Any]] = []
        for i in np.flatnonzero(fires):
            if i + 1 >= len(feat):
                continue
            entry = float(o[i + 1])
            start = int(ot[i + 1])
            sl, tp = bracket(entry, s4.side)
            done = newest >= start + horizon
            reason, px, xt, _amb = walk_forward(
                pt, ph, pl, pc, start, entry, tp, sl, long, horizon)
            if reason == "nodata":
                continue
            resolved = done or reason in ("tp", "sl")
            gross = (float(px) - entry) if long else (entry - float(px))
            net = (gross - entry * 0.85 / 1e4) if resolved else 0.0
            out.append({
                "signal_bar": int(ot[i]), "entry_time": start,
                "entry_price": round(entry, 4),
                "exit_time": int(xt) if resolved else None,
                "exit_price": round(float(px), 4) if resolved else None,
                "reason": reason if resolved else "open",
                "resolved": bool(resolved), "net": round(net, 2),
                "sl": round(sl, 4), "tp": round(tp, 4),
            })
        return {"signal4": out, "signal4_side": s4.side}
    except Exception as exc:
        log.warning("could not build signal4 trades for %s: %s", symbol, exc)
        return {"signal4": [], "signal4_side": s4.side}


def _signal5_trades(db: Database, symbol: str, cfg: IndicatorConfig,
                    since_ms: int | None = None) -> list[dict[str, Any]]:
    """FAST-C's firing history, at its own concurrency cap of 3.

    The cap is not cosmetic. This condition fires far more often than three
    slots can carry, and drawing every raw fire would show a rail full of
    markers that no account could ever have taken -- and a win rate computed
    over them would not be the 53/55% the pattern was measured at. So the same
    `cap_concurrency` the measurement used is applied here before anything is
    drawn.
    """
    from ..engine.signal5 import (Signal5Config, cap_concurrency, confirm_series,
                                  fire_mask)
    from ..tools.validate_rule import walk_forward

    s5 = Signal5Config()
    if symbol != s5.symbol:
        return []
    try:
        repo = CandleRepository(db)
        df = repo.load(s5.symbol, s5.anchor).reset_index(drop=True)
        if df.empty:
            return []
        ind = compute_indicators(df, s5.anchor, cfg)
        step = INTERVAL_MS[s5.anchor]
        ot = df["open_time"].to_numpy(dtype="int64")
        close_ms = ot + step
        macd30 = confirm_series(db, s5.symbol, "30m", "macd_hist", close_ms, cfg)
        adx4h = confirm_series(db, s5.symbol, "4h", "adx_14", close_ms, cfg)
        fires = fire_mask(ind, macd30, adx4h, s5)
        if since_ms is not None:
            fires = fires & (ot >= int(since_ms))

        o = df["open"].to_numpy(dtype="float64")
        path = repo.load(s5.symbol, "1m").reset_index(drop=True)
        pt = path["open_time"].to_numpy(dtype="int64")
        ph = path["high"].to_numpy(dtype="float64")
        pl = path["low"].to_numpy(dtype="float64")
        pc = path["close"].to_numpy(dtype="float64")
        horizon = s5.time_stop_bars * step
        newest = int(pt[-1]) if pt.size else 0

        # Resolve first, because the concurrency cap needs each trade's EXIT
        # time to know when a slot frees up.
        rows: list[dict[str, Any]] = []
        for i in np.flatnonzero(fires):
            if i + 1 >= len(df):
                continue
            entry = float(o[i + 1])
            start = int(ot[i + 1])
            sl, tp = s5.bracket(entry)
            done = newest >= start + horizon
            reason, px, xt, _amb = walk_forward(
                pt, ph, pl, pc, start, entry, tp, sl, False, horizon)
            if reason == "nodata":
                continue
            resolved = done or reason in ("tp", "sl")
            net = ((entry - float(px)) - entry * 0.85 / 1e4) if resolved else 0.0
            rows.append({
                "signal_bar": int(ot[i]), "entry_time": start,
                "entry_price": round(entry, 4),
                "exit_time": int(xt) if resolved else None,
                "exit_ms_raw": float(xt),
                "exit_price": round(float(px), 4) if resolved else None,
                "reason": reason if resolved else "open",
                "resolved": bool(resolved), "net": round(net, 2),
                "sl": round(sl, 4), "tp": round(tp, 4),
            })
        if not rows:
            return []
        starts = np.array([r["entry_time"] for r in rows], dtype="int64")
        exits = np.array([r["exit_ms_raw"] for r in rows], dtype="float64")
        keep = cap_concurrency(starts, exits, s5.max_concurrent)
        for r in rows:
            r.pop("exit_ms_raw", None)
        return [r for r, k in zip(rows, keep) if k]
    except Exception as exc:
        log.warning("could not build signal5 trades for %s: %s", symbol, exc)
        return []


def _signal7_trades(db: Database, symbol: str, cfg: IndicatorConfig,
                    since_ms: int | None = None) -> list[dict[str, Any]]:
    """Signal 7's walk-forward history — CROSS-FEED by construction.

    Trained on MT5:GOLD (17 months, mostly earlier than the chart feed),
    tested quarter by quarter on the feed being drawn — the exact setup the
    retraining experiments validated at 65.8% on Bybit. Because the training
    data lives on another feed, no in-feed warm-up quarters are lost and the
    lane covers the whole window. Never fitted on what it shows.
    """
    from ..engine.signal6_retrain import (EXP_C_EXTRA, EXP_LEG,
                                          FEATURE_KEYS, LogisticPwin,
                                          build_samples, matrix)
    from ..engine.signal7 import THRESHOLD_CAP
    from ..tools.retrain_signal6 import pick_regime_thresholds, regime_of

    if symbol != "XAUUSDT":
        return []
    try:
        test = build_samples(db, symbol, cfg, data_interval="1m")
        if not test:
            return []
        train_feed = build_samples(db, "MT5:GOLD", cfg, data_interval="5m")
        if len(train_feed) < 60:
            return []
        Xtr_all, ytr_all = matrix(train_feed)
        Xt_all, _yt = matrix(test)
        NEW5 = {"adx_slope", "ema_slope_change", "atr_pctile_change",
                "vol_change_ratio", "break_velocity"}
        M1K = tuple(k for k in FEATURE_KEYS if k not in (NEW5 | set(EXP_LEG)))
        keys = tuple(dict.fromkeys(M1K + EXP_C_EXTRA))
        col = {k: i for i, k in enumerate(FEATURE_KEYS)}
        use = [col[k] for k in keys]
        QUARTER = 90 * 86_400_000
        out: list[dict[str, Any]] = []
        start = test[0]["entry_time"]
        while start < test[-1]["entry_time"]:
            end = start + QUARTER
            tr = [i for i, s in enumerate(train_feed)
                  if s["entry_time"] < start]
            te = [i for i, s in enumerate(test)
                  if start <= s["entry_time"] < end]
            if len(tr) >= 60 and te:
                resolved = np.array([train_feed[i]["label"]["resolved"]
                                     for i in tr])
                m = LogisticPwin().fit(Xtr_all[tr][:, use][resolved],
                                       ytr_all[tr][resolved])
                thresholds = pick_regime_thresholds(
                    [train_feed[i] for i, ok in zip(tr, resolved) if ok],
                    m.predict(Xtr_all[tr][:, use][resolved]))
                thresholds = {k: min(v, THRESHOLD_CAP)
                              for k, v in thresholds.items()}
                p = m.predict(Xt_all[te][:, use])
                for i, pi in zip(te, p):
                    s = test[i]
                    thr = thresholds[regime_of(s)]
                    if pi < thr:
                        continue
                    lab = s["label"]
                    out.append({
                        "signal_bar": s["entry_time"] - 300_000,
                        "entry_time": s["entry_time"],
                        "entry_price": s["entry_price"],
                        "exit_time": (s["entry_time"] + int(
                            lab["minutes_to_outcome"] * 60_000))
                        if lab["resolved"] else None,
                        "exit_price": lab.get("exit_price"),
                        "reason": lab["reason"],
                        "resolved": lab["resolved"],
                        "net": lab["net"],
                        "sl": s["sl"], "tp": s["tp"],
                        "event_type": s["event_type"],
                        "p_win": round(float(pi), 3),
                    })
            start = end
        out.sort(key=lambda r: r["entry_time"])
        return out
    except Exception as exc:
        log.warning("could not build signal7 trades for %s: %s", symbol, exc)
        return []


def _live_stats(trades: list[dict[str, Any]], signals: list[dict[str, Any]],
                symbol: str) -> dict[str, str]:
    """The Summary panel: the live firing rate, then the realised record.

    **Filtered to THIS symbol.** The log is append-only across every config the
    system has ever run, so counting all of it put an `MT5:GOLD` firing rate next
    to an `XAUUSDT` trade list and the two disagreed by a factor of ten.

    The win rate and P/L come from `signal_outcomes.summarise`, over RESOLVED
    signals only — a still-running position is reported separately rather than
    folded in at zero.
    """
    from ..engine.signal_outcomes import summarise

    evals = [r for r in signals
             if r.get("kind") != "link" and r.get("symbol") == symbol]
    fired = [r for r in evals if r.get("fired")]
    entries = [r for r in evals if r.get("would_enter")]
    vetoed = [r for r in entries if (r.get("extras") or {}).get("vetoed")]
    sent = [r for r in entries if (r.get("extras") or {}).get("sent")]

    span_ms = (evals[-1]["bar_open_ms"] - evals[0]["bar_open_ms"]) if len(evals) > 1 else 0
    weeks = span_ms / (7 * 86_400_000) if span_ms else 0.0

    def per_week(n: int) -> str:
        return f"{n / weeks:.1f}/wk" if weeks >= 0.5 else "-"

    out = {
        "bars evaluated": f"{len(evals):,}",
        "all legs true": f"{len(fired)} \u00b7 {per_week(len(fired))}",
        "would enter": f"{len(entries)} \u00b7 {per_week(len(entries))}",
        "sent": str(len(sent)),
    }
    if evals:
        # The measured panel rate, so a live number far from it is visible as a
        # disagreement rather than mistaken for the expected behaviour.
        out["expected (panel)"] = "15.2/wk raw \u00b7 5.0/wk entries"
    # The realised record. Keys are prefixed so the two halves of this panel stay
    # visually distinct: what FIRED above, what it MADE below.
    for k, v in summarise(trades).items():
        out[k] = str(v)
    return out


def _broker_trades(db: Database, symbol: str) -> list[dict[str, Any]]:
    """Your real fills, imported from a broker report (`data/mt5.py`).

    Drawn at the price you actually got. The broker's contract is not the
    exchange's — spot gold sits about 0.08% under the perpetual — so a marker can
    sit slightly off the candles; adjusting the price to hide that would be
    falsifying the one thing in this file that actually happened.
    """
    try:
        rows = mt5.load(db, symbol)
    except sqlite3.OperationalError as exc:
        # A database predating the import feature legitimately has no table.
        # Anything else — a lock, a corrupt row — must not be swallowed: it would
        # silently publish a page missing your trades with no error anywhere,
        # which is exactly how this went wrong once already.
        if "no such table" not in str(exc):
            raise
        log.debug("broker_trades table absent: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        profit = r["profit"] or 0.0
        out.append({
            "id": r["position_id"],
            "broker": r["broker_symbol"],
            "side": r["side"],
            "vol": r["volume"],
            "units": round(r["volume"] * mt5.CONTRACT_SIZE.get(r["broker_symbol"], 1.0), 4),
            "entry_time": int(r["open_time"]),
            "entry_price": round(float(r["open_price"]), 4),
            "exit_time": int(r["close_time"]) if r["close_time"] is not None else None,
            "exit_price": round(float(r["close_price"]), 4) if r["close_price"] is not None else None,
            "sl": round(float(r["sl"]), 4) if r["sl"] else None,
            "tp": round(float(r["tp"]), 4) if r["tp"] else None,
            "profit": round(profit, 2),
            "swap": round(r["swap"] or 0.0, 2),
            # What actually hit the account, so it is comparable with the
            # backtest's net_pnl rather than only with its gross.
            "net": round(profit + (r["swap"] or 0.0) + (r["commission"] or 0.0), 2),
        })
    return out


def _zone_block(db: Database, symbol: str, cfg: IndicatorConfig,
                compute: bool = True,
                since_ms: int | None = None) -> dict[str, Any] | None:
    """The KEY LEVELS card for every H1 bar, encoded compactly.

    Zones are market-wide (M30+H1+H4 swings plus H1 session levels) and the app
    splits them using the H1 CONFIRMED close, so one series on the H1 grid serves
    every chart timeframe — a 5m bar inside an unfinished hour reads the last
    CLOSED hour's snapshot, the same as-of-closed rule as everything else.

    Source labels are interned: the same eleven strings repeat across ~24,000
    zones, and spelling them out would be most of the block's size.
    """
    try:
        if compute:
            snaps = zones.snapshot_series(db, symbol, cfg)
        else:
            # Cache only. `snapshot_series` fills the cache as a side effect, and
            # on `MT5:GOLD` that is 81,915 H1 bars at ~3 ms each -- minutes inside
            # a loop that is supposed to keep a live page current. A cold cache
            # means an empty KEY LEVELS card, which the page states, rather than
            # a page that hangs on first load with no explanation.
            cached = zones._load_cached(db, symbol, zones.config_key(cfg))
            snaps = [cached[t] for t in sorted(cached)]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        log.warning("zone_snapshots table absent — run any command once to "
                    "migrate: %s", exc)
        return None
    if since_ms is not None:
        # Only the hours the page can actually point at. The cache is keyed per
        # H1 bar and accumulates for the whole panel, so on a 25-year series it
        # is 22 MB of zones behind a chart showing 1,500 bars. One snapshot
        # BEFORE the window is kept, because a 5m bar in the first hour reads the
        # last CLOSED hour's card and would otherwise have none.
        keep = [x for x in snaps if x.ts >= since_ms]
        earlier = [x for x in snaps if x.ts < since_ms]
        snaps = ([earlier[-1]] if earlier else []) + keep
    if not snaps:
        log.warning("no zone snapshots for %s — the KEY LEVELS card will be "
                    "empty. Is 30m/1h/4h synced?", symbol)
        return None

    names: list[str] = []
    index: dict[str, int] = {}

    def enc(z: tuple) -> list[Any]:
        lo, hi, strength, sources = z
        ids = []
        for src in sources:
            if src not in index:
                index[src] = len(names)
                names.append(src)
            ids.append(index[src])
        return [round(lo, 2), round(hi, 2), round(strength, 2), ids]

    step = INTERVAL_MS[zones.ANCHOR_TF]
    ts = [s.ts for s in snaps]
    uniform = len(ts) < 2 or all(b - a == step for a, b in zip(ts, ts[1:]))

    block: dict[str, Any] = {
        "tf": zones.ANCHOR_TF,
        "step": step,
        "t0": ts[0],
        "n": len(ts),
        "price": [round(s.price, 2) for s in snaps],
        "atr": [None if s.reference_atr != s.reference_atr else round(s.reference_atr, 3)
                for s in snaps],
        "r": [[enc(z) for z in s.resistance] for s in snaps],
        "s": [[enc(z) for z in s.support] for s in snaps],
    }
    if not uniform:
        block["t"] = ts
    block["sources"] = names
    return block


# ---------------------------------------------------------------------------
# Books — one page holding several symbols
# ---------------------------------------------------------------------------


def build_book(
    db: Database,
    runs: dict[str, str],
    active: str | None = None,
    timeframes: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h"),
    indicator_config: IndicatorConfig | None = None,
    comparison: dict[str, Any] | None = None,
    external: bool = False,
) -> dict[str, Any]:
    """``{symbol: run_id}`` -> one payload the report can switch between.

    ``external=True`` keeps the non-active payloads OUT of the book, so the
    server can write them as sibling ``payload_<SYMBOL>.json`` files and the page
    fetches them on demand. That is right for watch mode (already over http, and
    three symbols embedded is ~25 MB) and wrong for a file you want to hand to
    someone, where self-contained is the whole point.
    """
    symbols = list(runs)
    if not symbols:
        raise SystemExit("build_book needs at least one {symbol: run_id} pair")
    active = active or symbols[0]
    if active not in runs:
        raise SystemExit(f"active symbol {active!r} is not one of {symbols}")

    built: dict[str, Any] = {}
    for sym, run_id in runs.items():
        if external and sym != active:
            continue
        built[sym] = build_payload(db, run_id, timeframes, indicator_config)

    return {
        "book": True,
        "symbols": symbols,
        "labels": {s: SYMBOL_LABEL.get(s, s) for s in symbols},
        "active": active,
        "runs": built,
        "external": bool(external),
        "comparison": comparison,
    }


def _as_book(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("book"):
        return payload
    sym = payload["symbol"]
    return {
        "book": True,
        "symbols": [sym],
        "labels": {sym: SYMBOL_LABEL.get(sym, sym)},
        "active": sym,
        "runs": {sym: payload},
        "external": False,
        "comparison": None,
    }


def atomic_write(path: Path, text: str, attempts: int = 12) -> None:
    """Write via a temp file and rename, retrying the rename on Windows.

    These files are megabytes and are rewritten while a browser may be fetching
    them. A plain write truncates first, so a request landing inside that window
    gets a partial file — which for an embedded JSON payload means a syntax error
    and a blank page.

    ``os.replace`` is atomic on both platforms, but on Windows it raises
    ``PermissionError`` while another process holds the destination open — and
    the report server has ``index.html`` open for as long as it takes to stream
    8 MB to the browser. So the rename is retried for a few seconds. Only if it
    still cannot land does this fall back to writing in place, which is the
    lesser evil: a stale page is worse than a briefly torn one.

    The temp name carries the pid so two processes writing the same directory
    cannot destroy each other's half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25 * (i + 1))
    log.warning("could not rename onto %s after %d attempts (is another process "
                "serving it?); writing in place", path.name, attempts)
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)


_atomic_write = atomic_write   # kept for callers inside this module


def render(payload: dict[str, Any], out_path: Path) -> Path:
    """Write the page. Accepts a single-run payload or a book."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    blob = json.dumps(_as_book(payload), separators=(",", ":"), allow_nan=False)
    _atomic_write(out_path, template.replace("/*__PAYLOAD__*/", blob))
    return out_path


def write_cockpit_page(out_path: Path) -> Path:
    """Copy the cockpit template into the served directory.

    Copied rather than served from `templates/` because the http server is
    rooted at the report directory and must not be given a second root -- and
    re-copied every cycle so editing the template shows up on refresh, the same
    behaviour `render` already gives the chart page.
    """
    _atomic_write(out_path, COCKPIT_TEMPLATE.read_text(encoding="utf-8"))
    return out_path


def write_payload_json(payload: dict[str, Any], path: Path) -> Path:
    """One symbol's payload as a sibling file, for the on-demand fetch path."""
    _atomic_write(path, json.dumps(payload, separators=(",", ":"), allow_nan=False))
    return path


def build_report(
    db: Database,
    run_id: str,
    out_path: Path,
    timeframes: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h"),
) -> Path:
    return render(build_payload(db, run_id, timeframes), out_path)
