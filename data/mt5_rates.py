"""Bar ingestion from the MetaTrader 5 terminal.

`docs/AUTOMATION.md` §A. Pulls the broker's own candles for the instrument that
would actually be traded, rather than inferring it from a Bybit perpetual.

**Stored under a distinct symbol key.** Rows land as ``MT5:GOLD``, never merged
into ``XAUUSDT``. They are a different instrument on a different clock with a
different bar partition, and a panel that mixes them is measuring an artefact.

**The clock is converted once, at this boundary — as a timezone, not a number.**
MT5 hands back the broker server's wall clock typed as though it were UTC. The
server runs EET/EEST, so its offset is +2 in winter and +3 in summer; converting
with a single measured integer stamps every bar from late October to late March
one hour early. `broker_clock.py` documents the two independent proofs that this
was happening and why an hour matters here. Everything downstream sees true UTC
epoch milliseconds, like the rest of the database.

**The H4 partition is kept exactly as published.** The server's day starts at
UTC+3 in summer and UTC+2 in winter, so its H4 buckets open at
21/01/05/09/13/17 UTC under EEST and **22/02/06/10/14/18 under EET** — they do
not line up with Bybit's, and they are not even constant across the year. Re-bucketing them onto UTC boundaries
would be worse than useless: the 4H UT bias is a *path-dependent latching*
trailing stop, so a different partition produces a different flip history, not a
slightly different number. What the broker publishes is what the broker's own
chart shows, and that is what the rule has to be validated against.

**Gaps here are real and expected.** `GOLD` is a session instrument: it stops at
the weekend and has a daily rollover break. `coverage --show-gaps` will report
those and they are not errors — unlike the Bybit perpetual, where a gap means
missing data.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from .broker_clock import (BROKER_TZ, offset_hours_at,
                           server_naive_to_utc_ms, utc_ms_to_server_naive)
from .db import INTERVAL_MS, CandleRepository, Database

log = logging.getLogger(__name__)

SYMBOL_PREFIX = "MT5:"

# copy_rates_from_pos silently caps out around 60k bars, so ranges are walked in
# chunks instead. 20k is comfortably inside every observed limit.
CHUNK_BARS = 20_000

# Oldest date ever requested. `datetime.fromtimestamp` raises on Windows for
# pre-epoch values, and walking back past this serves no purpose.
EARLIEST_MS = int(datetime(1995, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def stored_symbol(broker_symbol: str) -> str:
    return f"{SYMBOL_PREFIX}{broker_symbol}"


def _timeframes(api) -> dict[str, int]:
    return {
        "1m": api.TIMEFRAME_M1,
        "5m": api.TIMEFRAME_M5,
        "15m": api.TIMEFRAME_M15,
        "30m": api.TIMEFRAME_M30,
        "1h": api.TIMEFRAME_H1,
        "4h": api.TIMEFRAME_H4,
    }


@dataclass(frozen=True, slots=True)
class RateReport:
    symbol: str
    interval: str
    bars: int
    first_ms: int | None
    last_ms: int | None
    spread_median: float | None

    def describe(self) -> str:
        def f(ms: int | None) -> str:
            return ("—" if ms is None else
                    datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d"))
        return (f"{self.symbol} {self.interval}: {self.bars} bars "
                f"{f(self.first_ms)} -> {f(self.last_ms)}")


def fetch_rates(
    api,
    offset_hours: int,          # reporting only — conversion is tz-aware now
    broker_symbol: str,
    interval: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> np.ndarray | None:
    """Raw MT5 rates for one timeframe, in ascending true-UTC order.

    Returns the structured array MT5 gives, with ``time`` **replaced by true UTC
    epoch seconds**. The conversion happens here and nowhere else.
    """
    tfs = _timeframes(api)
    if interval not in tfs:
        raise ValueError(f"unsupported interval {interval!r}")
    if not api.symbol_select(broker_symbol, True):
        return None

    step_s = INTERVAL_MS[interval] // 1000

    def server_dt(ms: int) -> datetime:
        """True-UTC ms -> the naive server-clock datetime the API expects."""
        return utc_ms_to_server_naive(max(int(ms), EARLIEST_MS))

    now_ms = int(time.time() * 1000)
    step_ms = INTERVAL_MS[interval]
    hi = min(end_ms or now_ms, now_ms) + step_ms
    # Default: walk back until the broker stops serving. Depth is per timeframe
    # and set by the broker, not by us — M5 and M15 run out years before H1 does.
    # Floored at 1995 rather than 0: `datetime.fromtimestamp` throws on Windows
    # for pre-epoch values, and no broker serves gold tick data from the 1970s.
    lo = start_ms if start_ms is not None else EARLIEST_MS

    chunks: list[np.ndarray] = []
    cursor_hi = hi
    while cursor_hi > lo:
        cursor_lo = max(lo, cursor_hi - CHUNK_BARS * step_ms)
        got = api.copy_rates_range(
            broker_symbol, tfs[interval], server_dt(cursor_lo), server_dt(cursor_hi))
        if got is None or len(got) == 0:
            # Past the broker's depth for this timeframe. Note that a range
            # starting entirely before available history returns EMPTY rather
            # than clamping to what exists, which is why this walks backwards in
            # windows instead of asking for everything at once.
            break
        chunks.append(got)
        if cursor_lo <= lo:
            break                       # reached the requested floor
        # Continue below this window. Using cursor_lo rather than the oldest bar
        # returned matters: a FULL chunk has its oldest bar at cursor_lo, and
        # stopping there would truncate the history at the first window.
        oldest_utc = int(server_naive_to_utc_ms([int(got["time"].min())])[0])
        nxt = min(oldest_utc, cursor_lo) - 1
        if nxt >= cursor_hi:
            break                       # no progress; bail rather than spin
        cursor_hi = nxt

    if not chunks:
        return None
    rates = np.concatenate(chunks)
    rates = rates[np.unique(rates["time"], return_index=True)[1]]
    rates = np.sort(rates, order="time")
    # The one and only place server time becomes UTC. Timezone-aware, so the
    # October/March transitions are handled instead of being averaged over.
    utc_ms = server_naive_to_utc_ms(rates["time"])
    rates["time"] = (utc_ms // 1000).astype(rates["time"].dtype)
    return rates


def sync_symbol(
    db: Database,
    api,
    offset_hours: int,
    broker_symbol: str,
    intervals: list[str],
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[RateReport]:
    """Fetch and upsert. Idempotent — the same call twice writes the same rows."""
    repo = CandleRepository(db)
    symbol = stored_symbol(broker_symbol)
    out: list[RateReport] = []

    for interval in intervals:
        rates = fetch_rates(api, offset_hours, broker_symbol, interval,
                            start_ms=start_ms, end_ms=end_ms)
        if rates is None or len(rates) == 0:
            log.warning("%s %s: no rates returned", symbol, interval)
            out.append(RateReport(symbol, interval, 0, None, None, None))
            continue

        # `real_volume` is 0 on this feed — only tick counts exist. Ratios such as
        # relative_volume stay meaningful because they are unitless; absolute
        # turnover gates do not transfer and must never be built on this column.
        # CandleRepository.upsert prepends symbol/interval itself.
        # turnover is stored as 0.0 because this feed has none — `real_volume` is
        # empty and inventing one from tick_volume x price would be a fabricated
        # column that an absolute turnover gate could silently be built on.
        rows = [
            (int(r["time"]) * 1000,
             float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
             float(r["tick_volume"]), 0.0)
            for r in rates
        ]
        repo.upsert(symbol, interval, rows)
        spread = rates["spread"].astype(float) * 0.01 if "spread" in rates.dtype.names else None
        out.append(RateReport(
            symbol, interval, len(rows),
            int(rates["time"][0]) * 1000, int(rates["time"][-1]) * 1000,
            float(np.median(spread)) if spread is not None and spread.size else None,
        ))
        log.info("%s", out[-1].describe())
    return out


def sync(
    db: Database,
    broker_symbol: str = "GOLD",
    intervals: list[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
):
    """Connect read-only, pull rates, disconnect. Returns (terminal, reports)."""
    from . import mt5_live

    api, terminal = mt5_live.connect()
    try:
        reports = sync_symbol(db, api, terminal.offset_hours, broker_symbol,
                              intervals or ["1m", "5m", "15m", "30m", "1h", "4h"],
                              start_ms=start_ms, end_ms=end_ms)
    finally:
        api.shutdown()
    return terminal, reports


def session_gaps(db: Database, symbol: str, interval: str) -> dict:
    """Describe the holes, and separate the expected ones from the suspicious.

    Weekend and rollover breaks are how a session instrument is *supposed* to
    look. A gap inside a trading session is not, and is the only kind worth
    reporting as a problem.
    """
    df = CandleRepository(db).load(symbol, interval)
    if df.empty:
        return {"bars": 0}
    t = df["open_time"].to_numpy(dtype="int64")
    step = INTERVAL_MS[interval]
    d = np.diff(t)
    holes = np.flatnonzero(d > step)
    weekend, intraday = 0, []
    for i in holes:
        start = datetime.fromtimestamp(t[i] / 1000, timezone.utc)
        missing = int(d[i] // step) - 1
        # A break that spans a Saturday is the market being shut.
        end = datetime.fromtimestamp(t[i + 1] / 1000, timezone.utc)
        spans_weekend = any((start + timedelta(days=k)).weekday() == 5
                            for k in range(0, (end - start).days + 1))
        if spans_weekend or d[i] <= step * 4:
            weekend += 1
        else:
            intraday.append({"after": int(t[i]), "missing": missing})
    return {
        "bars": int(t.size),
        "first": int(t[0]), "last": int(t[-1]),
        "expected_breaks": weekend,
        "unexplained": intraday[:20],
        "unexplained_count": len(intraday),
    }
