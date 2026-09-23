"""Key-level snapshots over time — the app's KEY LEVELS card, for every bar.

The dashboard shows support and resistance zones built from swing structure and
session extremes (`SupportResistanceEngine.kt:68-109`, never from the UT level).
Reproducing that for a *single* moment is what `tools/plot_chart.build_zones`
already does. Reproducing it for **every** bar so a chart tooltip can answer
"what did the card say here?" is a different problem, for two reasons.

**1. It is window-dependent, not warm-up sensitive.** Swing chains *accumulate* —
over the full 166 days a 30m series yields 1,633 accepted swings versus 104 over
the app's 500-bar buffer, and `_cluster` chains transitively, so with the extra
swings every zone merges into one 300-point blob. The window has to slide with
the evaluation point, which means genuinely recomputing the chain per snapshot
rather than indexing into one pass. See `docs/OPEN_QUESTIONS.md` Q4.

**2. It is market-wide, not per-timeframe.** The zones come from M30 + H1 + H4
swings plus H1 session levels regardless of which chart you are looking at
(`MarketAnalysisEngine.kt:141-151`), and the app splits them into support and
resistance using the **H1 CONFIRMED close** as "current price" (`:149`). So one
snapshot per H1 bar describes every timeframe's view of that moment, and a 5m bar
inside an unfinished H1 bar correctly sees the last *closed* H1 snapshot — the
same as-of-closed rule the engine uses everywhere else.

That makes the grid H1, which is ~4,000 snapshots over this dataset at ~3 ms
each. Closed bars never change, so they are cached in `zone_snapshots` keyed by
the indicator config; the first build costs ~13 s per symbol and every later one
costs a few milliseconds for the new hour.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

import numpy as np

from ..data.db import INTERVAL_MS, CandleRepository, Database
from . import structure as st
from .base import IndicatorConfig
from .trend import atr as atr_fn

# Zones are built market-wide from these three, not per timeframe
# (`MarketAnalysisEngine.kt:141-144`).
STRUCTURAL_TFS: tuple[str, ...] = ("30m", "1h", "4h")
# `dailyCandles` and `referenceAtr` are both H1 CONFIRMED (`:149-150`).
ANCHOR_TF = "1h"


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    ts: int                     # H1 bar open_time this state is as of
    price: float                # the app's "Current" — H1 CONFIRMED close
    reference_atr: float
    resistance: tuple[tuple[float, float, float, tuple[str, ...]], ...]
    support: tuple[tuple[float, float, float, tuple[str, ...]], ...]


def config_key(cfg: IndicatorConfig) -> str:
    """Identifies the inputs a snapshot depends on, so a config change invalidates.

    Only the fields that actually feed zone construction are included — changing
    an unrelated knob should not throw away 13 seconds of work.
    """
    payload = json.dumps({
        "window": cfg.app_window_bars,
        "atr_period": cfg.atr_period,
        "swing_sep": cfg.swing_min_separation_atr,
        "tol": st.ZONE_TOLERANCE_ATR,
        "max_side": st.MAX_ZONES_PER_SIDE,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


class _Frames:
    """Numpy views of the structural timeframes, sliced positionally.

    Filtering a DataFrame per snapshot is O(n) over the whole series and was 40%
    of the runtime; searchsorted plus a slice is O(window).
    """

    def __init__(self, repo: CandleRepository, symbol: str) -> None:
        self.f: dict[str, dict[str, np.ndarray]] = {}
        for tf in {*STRUCTURAL_TFS, ANCHOR_TF}:
            d = repo.load(symbol, tf)
            if d.empty:
                continue
            self.f[tf] = {
                "t": d["open_time"].to_numpy(dtype="int64"),
                "h": d["high"].to_numpy(),
                "l": d["low"].to_numpy(),
                "c": d["close"].to_numpy(),
            }

    def window(self, tf: str, end_ms: int, win: int):
        a = self.f.get(tf)
        if a is None:
            return None
        j = int(np.searchsorted(a["t"], end_ms, "right"))
        i = max(0, j - win)
        if j <= i:
            return None
        return a["h"][i:j], a["l"][i:j], a["c"][i:j], a["t"][i:j]


def _zone_tuple(z: st.PriceZone) -> tuple[float, float, float, tuple[str, ...]]:
    return (round(z.low, 4), round(z.high, 4), round(z.strength, 4), z.sources)


def snapshot_at(
    frames: _Frames, end_ms: int, cfg: IndicatorConfig, _cache: dict | None = None
) -> ZoneSnapshot | None:
    """The KEY LEVELS card as of the H1 bar opening at ``end_ms``.

    ``_cache`` memoises swing chains by (timeframe, window bounds). The H4 window
    only advances every fourth H1 bar, so a third of the chain builds are
    redundant without it.
    """
    win = cfg.app_window_bars
    swings: dict[str, list[st.SwingPoint]] = {}
    anchor_atr: np.ndarray | None = None

    for tf in STRUCTURAL_TFS:
        w = frames.window(tf, end_ms, win)
        if w is None:
            continue
        H, L, C, T = w
        key = (tf, int(T[0]), int(T[-1]))
        if _cache is not None and key in _cache:
            swings[tf], a = _cache[key]
        else:
            a = atr_fn(H, L, C, cfg.atr_period)
            chain = st.final_accepted_chain(
                H, L, T, a, st.swing_lookback_for(tf), cfg.swing_min_separation_atr
            )
            swings[tf] = chain
            if _cache is not None:
                _cache[key] = (chain, a)
        if tf == ANCHOR_TF:
            anchor_atr = a

    anchor = frames.window(ANCHOR_TF, end_ms, win)
    if anchor is None:
        return None
    H, L, C, T = anchor
    if anchor_atr is None:
        anchor_atr = atr_fn(H, L, C, cfg.atr_period)
    reference_atr = float(anchor_atr[-1])
    price = float(C[-1])

    levels = st.build_key_levels(
        current_price=price,
        swings_by_timeframe=swings,
        daily_open_time=T,
        daily_high=H,
        daily_low=L,
        daily_close=C,
        reference_atr=reference_atr,
    )
    return ZoneSnapshot(
        ts=int(T[-1]),
        price=round(price, 4),
        reference_atr=round(reference_atr, 4) if reference_atr == reference_atr else float("nan"),
        resistance=tuple(_zone_tuple(z) for z in levels.resistance),
        support=tuple(_zone_tuple(z) for z in levels.support),
    )


# ---------------------------------------------------------------------------
# Cached series
# ---------------------------------------------------------------------------


def _load_cached(db: Database, symbol: str, key: str) -> dict[int, ZoneSnapshot]:
    rows = db.conn.execute(
        "SELECT ts, price, reference_atr, zones_json FROM zone_snapshots "
        "WHERE symbol = ? AND config_key = ? ORDER BY ts",
        (symbol, key),
    ).fetchall()
    out: dict[int, ZoneSnapshot] = {}
    for r in rows:
        blob = json.loads(r["zones_json"])
        out[int(r["ts"])] = ZoneSnapshot(
            ts=int(r["ts"]),
            price=float(r["price"]),
            # NaN round-trips through SQLite as NULL. Early bars legitimately have
            # no ATR yet (and therefore no zones), so this is expected, not an
            # error — but reading it back as float(None) is a crash.
            reference_atr=(float(r["reference_atr"])
                           if r["reference_atr"] is not None else float("nan")),
            resistance=tuple(tuple(z[:3]) + (tuple(z[3]),) for z in blob["r"]),
            support=tuple(tuple(z[:3]) + (tuple(z[3]),) for z in blob["s"]),
        )
    return out


def latest_cached(db: Database, symbol: str,
                  cfg: IndicatorConfig | None = None) -> ZoneSnapshot | None:
    """The newest cached snapshot, without loading the rest.

    `_load_cached` materialises every snapshot for a symbol and JSON-decodes each
    one. That is right when the caller wants the series; it is 81,922 rows on
    `MT5:GOLD` when the caller wants the current KEY LEVELS card, which is what
    the live risk read wants several times a minute.

    Returns None when nothing is cached — never computes. Building the cache is
    `cli zones`, deliberately, because a snapshot is a real recompute over a
    trailing 500-bar buffer and a cold cache would otherwise stall a live loop.
    """
    cfg = cfg or IndicatorConfig()
    try:
        row = db.conn.execute(
            "SELECT ts, price, reference_atr, zones_json FROM zone_snapshots "
            "WHERE symbol = ? AND config_key = ? ORDER BY ts DESC LIMIT 1",
            (symbol, config_key(cfg)),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    if row is None:
        return None
    blob = json.loads(row["zones_json"])
    return ZoneSnapshot(
        ts=int(row["ts"]),
        price=float(row["price"]),
        reference_atr=(float(row["reference_atr"])
                       if row["reference_atr"] is not None else float("nan")),
        resistance=tuple(tuple(z[:3]) + (tuple(z[3]),) for z in blob["r"]),
        support=tuple(tuple(z[:3]) + (tuple(z[3]),) for z in blob["s"]),
    )


def _store(db: Database, symbol: str, key: str, snaps: list[ZoneSnapshot]) -> None:
    now = int(time.time() * 1000)
    with db.tx() as cur:
        cur.executemany(
            "INSERT OR REPLACE INTO zone_snapshots "
            "(symbol, config_key, ts, price, reference_atr, zones_json, built_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (symbol, key, s.ts, s.price, s.reference_atr,
                 json.dumps({"r": [list(z[:3]) + [list(z[3])] for z in s.resistance],
                             "s": [list(z[:3]) + [list(z[3])] for z in s.support]},
                            separators=(",", ":")),
                 now)
                for s in snaps
            ],
        )


def snapshot_series(
    db: Database,
    symbol: str,
    cfg: IndicatorConfig | None = None,
    end_ms: int | None = None,
    progress=None,
) -> list[ZoneSnapshot]:
    """One snapshot per H1 bar, cached. Only new bars are computed.

    A closed bar's snapshot is a function of closed bars only, so it never
    changes and caching it is not an approximation.
    """
    cfg = cfg or IndicatorConfig()
    key = config_key(cfg)
    repo = CandleRepository(db)
    frames = _Frames(repo, symbol)
    anchor = frames.f.get(ANCHOR_TF)
    if anchor is None:
        return []

    times = anchor["t"]
    if end_ms is not None:
        times = times[times <= int(end_ms)]
    if times.size == 0:
        return []

    cached = _load_cached(db, symbol, key)
    missing = [int(t) for t in times if int(t) not in cached]
    if missing:
        if progress:
            progress(0, len(missing))
        swing_cache: dict = {}
        fresh: list[ZoneSnapshot] = []
        for n, t in enumerate(missing, 1):
            s = snapshot_at(frames, t, cfg, swing_cache)
            if s is not None:
                fresh.append(s)
                cached[s.ts] = s
            # The memo is keyed by window bounds and only the newest few are ever
            # reused; unbounded it would hold every window in the history.
            if len(swing_cache) > 16:
                swing_cache.clear()
            if progress and n % 250 == 0:
                progress(n, len(missing))
        if fresh:
            _store(db, symbol, key, fresh)
        if progress:
            progress(len(missing), len(missing))

    return [cached[int(t)] for t in times if int(t) in cached]
