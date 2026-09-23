"""Resumable, gap-aware market-data sync.

Design rules:

* **Never store an unclosed bar.** A bar is closed when its whole span is behind
  the sync horizon. The Kotlin app derives this from the device clock
  (`BybitMarketDataSource.kt:134`); here the horizon is an explicit parameter so
  a sync is reproducible.
* **Idempotent.** Re-running over the same range rewrites identical rows.
* **Resumable.** `sync_state` records the newest stored bar per (symbol, interval).
* **Gaps are refilled, not ignored.** After a pass, holes are detected and
  re-requested once; whatever remains is reported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .bybit_client import BybitClient, BybitError
from .db import INTERVAL_MS, CandleRepository, Database, Gap, MetaRepository

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncReport:
    symbol: str
    interval: str
    requested_start_ms: int
    requested_end_ms: int
    bars_written: int = 0
    first_open_time: int | None = None
    last_open_time: int | None = None
    gaps: list[Gap] = field(default_factory=list)
    refilled_bars: int = 0
    # Set when the requested window starts before any data exists for this
    # contract. A silent empty table is exactly the quiet failure this project
    # is supposed to refuse, so it is reported explicitly.
    truncated_to_ms: int | None = None

    @property
    def ok(self) -> bool:
        return not self.gaps

    def describe(self) -> str:
        span = "—"
        if self.first_open_time is not None and self.last_open_time is not None:
            span = f"{_iso(self.first_open_time)} → {_iso(self.last_open_time)}"
        gap_text = "no gaps" if self.ok else f"{len(self.gaps)} gap(s)"
        return (
            f"{self.symbol} {self.interval}: {self.bars_written} bars, {span}, {gap_text}"
        )


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) + "Z"


def closed_bar_horizon(interval: str, now_ms: int) -> int:
    """Newest ``open_time`` whose bar is fully closed at ``now_ms``.

    A bar opening at ``t`` closes at ``t + step``. So the newest closed bar opens
    at ``floor(now/step)*step - step``.
    """
    step = INTERVAL_MS[interval]
    return (now_ms // step) * step - step


class MarketDataSync:
    def __init__(
        self,
        client: BybitClient,
        db: Database,
        *,
        allow_unclosed: bool = False,
    ) -> None:
        self.client = client
        self.db = db
        self.candles = CandleRepository(db)
        self.meta = MetaRepository(db)
        self.allow_unclosed = allow_unclosed

    # -- instruments -------------------------------------------------------

    def sync_instrument(self, symbol: str) -> str:
        """Resolve and cache instrument metadata. Returns the confirmed symbol."""
        import json

        inst = self.client.resolve_symbol(symbol)
        self.meta.upsert_instrument(
            symbol=inst.symbol,
            tick_size=inst.tick_size,
            qty_step=inst.qty_step,
            min_qty=inst.min_qty,
            raw_json=json.dumps(inst.raw, separators=(",", ":")),
            updated_at=int(time.time() * 1000),
        )
        log.info(
            "resolved %s: baseCoin=%s quote=%s tick=%s qtyStep=%s minQty=%s",
            inst.symbol,
            inst.base_coin,
            inst.quote_coin,
            inst.tick_size,
            inst.qty_step,
            inst.min_qty,
        )
        return inst.symbol

    # -- candles -----------------------------------------------------------

    def sync_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        now_ms: int | None = None,
        resume: bool = True,
    ) -> SyncReport:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unknown interval {interval!r}")

        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        horizon = end_ms if self.allow_unclosed else min(
            end_ms, closed_bar_horizon(interval, now_ms)
        )

        report = SyncReport(
            symbol=symbol,
            interval=interval,
            requested_start_ms=start_ms,
            requested_end_ms=horizon,
        )
        if horizon < start_ms:
            log.info("%s %s: nothing closed in range", symbol, interval)
            return report

        fetch_from = start_ms
        if resume:
            last = self.meta.get_sync_state(symbol, interval)
            first_stored, last_stored, count = self.candles.coverage(symbol, interval)
            # Only skip ahead when the stored series already reaches back far
            # enough; otherwise a resume would leave a permanent hole at the front.
            if last is not None and first_stored is not None and first_stored <= start_ms:
                fetch_from = max(start_ms, last - INTERVAL_MS[interval])
                if count:
                    log.info(
                        "%s %s: resuming from %s (%d bars stored)",
                        symbol,
                        interval,
                        _iso(fetch_from),
                        count,
                    )

        rows = self.client.klines(symbol, interval, fetch_from, horizon)
        rows = [r for r in rows if r[0] <= horizon]
        report.bars_written = self.candles.upsert(symbol, interval, rows)

        # If the exchange has nothing as far back as we asked, say so loudly and
        # narrow the gap scan to the range that can actually exist — otherwise
        # every bar before listing is reported as a "gap" forever.
        earliest = self._earliest_available(symbol, interval)
        effective_start = start_ms
        if earliest is not None and earliest > start_ms:
            report.truncated_to_ms = earliest
            effective_start = earliest
            log.warning(
                "%s %s: no data before %s (contract listed later) — "
                "requested start %s is out of range",
                symbol,
                interval,
                _iso(earliest),
                _iso(start_ms),
            )

        # Gap detection over the range that can actually exist.
        report.gaps = self.candles.find_gaps(symbol, interval, effective_start, horizon)
        if report.gaps:
            report.refilled_bars = self._refill(symbol, interval, report.gaps, horizon)
            report.gaps = self.candles.find_gaps(
                symbol, interval, effective_start, horizon
            )

        first, last, _ = self.candles.coverage(symbol, interval)
        report.first_open_time, report.last_open_time = first, last
        if last is not None:
            self.meta.set_sync_state(symbol, interval, last, now_ms)
        return report

    def _earliest_available(self, symbol: str, interval: str) -> int | None:
        """Listing time of the contract, from cached instrument metadata.

        Bybit's ``launchTime`` is authoritative and cheaper than probing klines.
        Returns None when the instrument has not been resolved yet.
        """
        import json

        row = self.meta.get_instrument(symbol)
        if row is None:
            return None
        try:
            raw = json.loads(row["raw_json"])
            launch = int(raw.get("launchTime"))
        except (TypeError, ValueError, KeyError):
            return None
        # Round up to the first fully-contained bar boundary at this interval.
        step = INTERVAL_MS[interval]
        return -(-launch // step) * step

    def _refill(
        self, symbol: str, interval: str, gaps: list[Gap], horizon: int
    ) -> int:
        """One re-request pass per detected hole."""
        written = 0
        step = INTERVAL_MS[interval]
        for gap in gaps:
            log.warning(
                "%s %s: refilling %d missing bars %s → %s",
                symbol,
                interval,
                gap.missing_bars,
                _iso(gap.start_ms),
                _iso(gap.end_ms),
            )
            try:
                rows = self.client.klines(
                    symbol,
                    interval,
                    max(0, gap.start_ms - step),
                    min(horizon, gap.end_ms + step),
                )
            except BybitError as exc:
                log.error("refill failed for %s %s: %s", symbol, interval, exc)
                continue
            rows = [r for r in rows if r[0] <= horizon]
            written += self.candles.upsert(symbol, interval, rows)
        return written

    # -- context features --------------------------------------------------

    def sync_funding(self, symbol: str, start_ms: int, end_ms: int) -> int:
        rows = self.client.funding_history(symbol, start_ms, end_ms)
        return self.meta.upsert_funding(symbol, rows)

    def sync_open_interest(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> int:
        rows = self.client.open_interest(symbol, interval, start_ms, end_ms)
        return self.meta.upsert_open_interest(symbol, interval, rows)
