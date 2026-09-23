"""Bybit V5 public market data client.

No account, no API key. Reached over `api.bytick.com` — the same V5 service as
`api.bybit.com`, but the mirror stays reachable from networks that block the
primary domain. That is the same reason the Android app uses it
(`data/remote/BybitApi.kt:12-14`).

Two Bybit quirks this module exists to absorb:

1. Errors arrive as a non-zero ``retCode`` inside an HTTP 200. A successful HTTP
   call is not yet a successful request (`BybitApi.kt:16-18`).
2. Kline rows are positional arrays of **strings**, newest-first. They are cast
   and re-sorted ascending here so nothing downstream ever sees wire order.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import httpx

log = logging.getLogger(__name__)

PRIMARY_HOST = "https://api.bytick.com"
FALLBACK_HOST = "https://api.bybit.com"

CATEGORY_LINEAR = "linear"
MAX_LIMIT = 1000

# Canonical interval -> Bybit wire code.
INTERVAL_TO_BYBIT: dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

# Positional indices into a kline row (`BybitApi.kt:63-69`).
_START_TIME, _OPEN, _HIGH, _LOW, _CLOSE, _VOLUME, _TURNOVER = 0, 1, 2, 3, 4, 5, 6

RET_OK = 0
_RATE_LIMIT_RET_CODES = {10006, 10018}


# Symbols we have a documented expectation for. Resolution asserts the baseCoin
# matches, so a typo or a re-listing cannot silently swap the instrument under a
# saved config. Symbols absent from this table resolve without the extra check.
EXPECTED_BASE_COIN: dict[str, str] = {
    "XAUUSDT": "XAU",   # gold commodity perpetual — NOT XAUT (Tether Gold)
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}


class BybitError(RuntimeError):
    """A request failed in a way retrying will not fix."""


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    base_coin: str
    quote_coin: str
    contract_type: str
    tick_size: float | None
    qty_step: float | None
    min_qty: float | None
    raw: dict[str, Any]


class _RateLimiter:
    """Token-bucket-ish spacing. Cheap, thread-safe, good enough for ~8 req/s."""

    def __init__(self, max_per_second: float = 8.0) -> None:
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


class BybitClient:
    """Public market-data reads only. Never signs, never trades."""

    def __init__(
        self,
        primary_host: str = PRIMARY_HOST,
        fallback_host: str = FALLBACK_HOST,
        timeout: float = 20.0,
        max_per_second: float = 8.0,
        max_retries: int = 3,
        rng_seed: int = 0,
    ) -> None:
        self.hosts = [primary_host, fallback_host]
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "xau-backtest/0.1 (+local)"},
            follow_redirects=True,
        )
        self._limiter = _RateLimiter(max_per_second)
        self._max_retries = max_retries
        # Seeded so backoff jitter cannot make a run non-reproducible.
        self._rng = random.Random(rng_seed)
        self._active_host = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BybitClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None

        for host_offset in range(len(self.hosts)):
            host = self.hosts[(self._active_host + host_offset) % len(self.hosts)]
            url = f"{host}/{path.lstrip('/')}"

            for attempt in range(self._max_retries + 1):
                self._limiter.acquire()
                try:
                    resp = self._client.get(url, params=params)
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt == self._max_retries:
                        break
                    self._sleep_backoff(attempt)
                    continue

                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    last_error = BybitError(
                        f"{host} returned HTTP {resp.status_code} for {path}"
                    )
                    if attempt == self._max_retries:
                        break
                    self._sleep_backoff(attempt)
                    continue

                if resp.status_code != 200:
                    # 400-class other than 403/429: the request itself is wrong.
                    raise BybitError(
                        f"{host} rejected {path} with HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )

                body = resp.json()
                ret_code = body.get("retCode", -1)
                if ret_code == RET_OK:
                    # Remember the host that worked so later calls start there.
                    self._active_host = (self._active_host + host_offset) % len(self.hosts)
                    return body

                if ret_code in _RATE_LIMIT_RET_CODES:
                    last_error = BybitError(f"rate limited (retCode {ret_code})")
                    if attempt == self._max_retries:
                        break
                    self._sleep_backoff(attempt)
                    continue

                raise BybitError(
                    f"Bybit error {ret_code} on {path}: "
                    f"{body.get('retMsg') or 'no reason given'}"
                )

            log.warning("host %s exhausted retries for %s, trying next host", host, path)

        raise BybitError(f"all hosts failed for {path}") from last_error

    def _sleep_backoff(self, attempt: int) -> None:
        delay = (2.0**attempt) + self._rng.uniform(0.0, 0.4)
        log.debug("backing off %.2fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    # -- instruments -------------------------------------------------------

    def instruments(self, category: str = CATEGORY_LINEAR) -> list[Instrument]:
        """Every linear instrument, paged through Bybit's cursor."""
        out: list[Instrument] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = self._get("/v5/market/instruments-info", params)
            result = body.get("result") or {}
            for item in result.get("list", []):
                out.append(_parse_instrument(item))
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return out

    def resolve_symbol(
        self, symbol: str, category: str = CATEGORY_LINEAR
    ) -> Instrument:
        """Resolve a contract at runtime rather than trusting a literal string.

        `symbol` must exist in instruments-info or this raises. When the symbol is
        one we have a documented expectation for, the `baseCoin` is additionally
        asserted — the README's stated test, which the Kotlin app documents but
        does not actually verify.

        This is deliberately strict: silently falling back to a different but
        similarly-named instrument is how you end up backtesting Tether Gold and
        trading a commodity perpetual.
        """
        found = [i for i in self.instruments(category) if i.symbol == symbol]
        if not found:
            expected = EXPECTED_BASE_COIN.get(symbol)
            near = sorted(
                i.symbol
                for i in self.instruments(category)
                if expected and i.base_coin.startswith(expected[:3])
            )
            raise BybitError(
                f"{symbol} not found in category={category}."
                + (f" Similar symbols available: {near}" if near else "")
            )
        inst = found[0]
        expected = EXPECTED_BASE_COIN.get(symbol)
        if expected is not None and inst.base_coin != expected:
            raise BybitError(
                f"{symbol} resolved but baseCoin is {inst.base_coin!r}, expected "
                f"{expected!r}. Refusing to proceed — see docs/OPEN_QUESTIONS.md Q1."
            )
        return inst

    def resolve_gold_symbol(
        self, preferred: str = "XAUUSDT", category: str = CATEGORY_LINEAR
    ) -> Instrument:
        """Back-compatible alias. Gold's baseCoin guard lives in EXPECTED_BASE_COIN."""
        return self.resolve_symbol(preferred, category)

    # -- klines ------------------------------------------------------------

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        category: str = CATEGORY_LINEAR,
        max_pages: int | None = None,
    ) -> list[tuple[int, float, float, float, float, float, float]]:
        """All bars with ``start_ms <= open_time <= end_ms``, ascending, deduped.

        Bybit answers newest-first from ``end`` backwards, so paging means walking
        the ``end`` cursor down past the oldest row received.

        ``max_pages`` caps the walk. The live loop passes 1: its window is well
        under the 1000-bar page size, so a second request could only ever return
        the same rows again, and it runs several times a minute.
        """
        rows: dict[int, tuple[int, float, float, float, float, float, float]] = {}
        cursor_end = int(end_ms)
        bybit_interval = INTERVAL_TO_BYBIT[interval]
        pages = 0

        while cursor_end >= start_ms:
            if max_pages is not None and pages >= max_pages:
                break
            pages += 1
            body = self._get(
                "/v5/market/kline",
                {
                    "category": category,
                    "symbol": symbol,
                    "interval": bybit_interval,
                    "start": int(start_ms),
                    "end": cursor_end,
                    "limit": MAX_LIMIT,
                },
            )
            page = (body.get("result") or {}).get("list") or []
            if not page:
                break

            parsed = [p for p in (_parse_kline_row(r) for r in page) if p is not None]
            if not parsed:
                break

            for row in parsed:
                rows[row[0]] = row

            oldest = min(r[0] for r in parsed)
            if oldest <= start_ms:
                break
            # Step strictly below the oldest bar we just took. Bybit's `end` is
            # treated as inclusive of a bar whose open_time equals it (see
            # docs/OPEN_QUESTIONS.md Q31); stepping by 1ms is correct either way
            # because we dedupe on open_time.
            next_end = oldest - 1
            if next_end >= cursor_end:  # no progress: bail rather than spin
                break
            cursor_end = next_end

        return [rows[k] for k in sorted(rows)]

    # -- context features --------------------------------------------------

    def funding_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        category: str = CATEGORY_LINEAR,
    ) -> list[tuple[int, float]]:
        out: dict[int, float] = {}
        cursor_end = int(end_ms)
        while cursor_end >= start_ms:
            body = self._get(
                "/v5/market/funding/history",
                {
                    "category": category,
                    "symbol": symbol,
                    "startTime": int(start_ms),
                    "endTime": cursor_end,
                    "limit": 200,
                },
            )
            page = (body.get("result") or {}).get("list") or []
            if not page:
                break
            parsed: list[tuple[int, float]] = []
            for item in page:
                try:
                    parsed.append(
                        (int(item["fundingRateTimestamp"]), float(item["fundingRate"]))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if not parsed:
                break
            out.update(dict(parsed))
            oldest = min(t for t, _ in parsed)
            if oldest <= start_ms or oldest - 1 >= cursor_end:
                break
            cursor_end = oldest - 1
        return sorted(out.items())

    def open_interest(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        category: str = CATEGORY_LINEAR,
    ) -> list[tuple[int, float]]:
        """Bybit exposes OI only on 5min/15min/30min/1h/4h/1d grids."""
        oi_interval = {
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1h",
            "4h": "4h",
            "1d": "1d",
        }.get(interval)
        if oi_interval is None:
            raise ValueError(f"open interest is not published for interval {interval!r}")

        out: dict[int, float] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "category": category,
                "symbol": symbol,
                "intervalTime": oi_interval,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            body = self._get("/v5/market/open-interest", params)
            result = body.get("result") or {}
            page = result.get("list") or []
            if not page:
                break
            for item in page:
                try:
                    out[int(item["timestamp"])] = float(item["openInterest"])
                except (KeyError, TypeError, ValueError):
                    continue
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return sorted(out.items())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_kline_row(
    row: Sequence[Any],
) -> tuple[int, float, float, float, float, float, float] | None:
    """One positional kline row -> typed tuple. A malformed row is dropped, not raised.

    Mirrors `BybitMarketDataSource.toBybitCandle` (`:111-136`): one bad bar must
    not kill an otherwise good fetch.
    """
    try:
        open_time = int(row[_START_TIME])
    except (IndexError, TypeError, ValueError):
        return None

    values = [_to_float(row[i]) if i < len(row) else None
              for i in (_OPEN, _HIGH, _LOW, _CLOSE)]
    if any(v is None for v in values):
        return None
    o, h, low, c = values  # type: ignore[misc]

    volume = _to_float(row[_VOLUME]) if len(row) > _VOLUME else 0.0
    turnover = _to_float(row[_TURNOVER]) if len(row) > _TURNOVER else 0.0
    return (open_time, o, h, low, c, volume or 0.0, turnover or 0.0)


def _parse_instrument(item: dict[str, Any]) -> Instrument:
    price_filter = item.get("priceFilter") or {}
    lot_filter = item.get("lotSizeFilter") or {}
    return Instrument(
        symbol=item.get("symbol", ""),
        base_coin=item.get("baseCoin", ""),
        quote_coin=item.get("quoteCoin", ""),
        contract_type=item.get("contractType", ""),
        tick_size=_to_float(price_filter.get("tickSize")),
        qty_step=_to_float(lot_filter.get("qtyStep")),
        min_qty=_to_float(lot_filter.get("minOrderQty")),
        raw=item,
    )
