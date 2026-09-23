"""Swing structure, BOS/CHoCH and support/resistance zones.

Ports `analysis/structure/SwingDetector.kt`, `MarketStructureEngine.kt` and
`SupportResistanceEngine.kt`.

**This module is where look-ahead safety is won or lost.** Two facts drive the design:

1. A fractal pivot at index ``i`` needs ``lookback`` bars to its right, so it is
   *confirmed late* — first visible at bar ``i + lookback``. `SwingDetector.kt:54`
   bounds the loop at ``size - lookback``, so pivot-ness never depends on a bar
   beyond ``i + lookback``: computing pivots once over the full series and then
   gating on ``i + lookback <= t`` is exactly equivalent to recomputing, and is
   causally safe.

2. The zigzag filter is **not** append-only. `SwingDetector.kt:93` replaces the
   last accepted pivot in place when a later, more extreme same-type pivot
   arrives, so ``lastSwingHigh`` can change with no new bar closing at that index.
   The accepted chain must therefore be folded left-to-right and **snapshotted at
   every bar** — which is what `swing_state_series` does incrementally in O(n).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .mathseries import NaN

SWING_HIGH = 1
SWING_LOW = -1

STRUCTURE_RANGE = 0
STRUCTURE_BULLISH = 1
STRUCTURE_BEARISH = -1

BIAS_NEUTRAL, BIAS_BULLISH, BIAS_BEARISH = 0, 1, -1


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    price: float
    time_utc: int
    type: int  # SWING_HIGH | SWING_LOW


def swing_lookback_for(interval: str) -> int:
    """Higher timeframes need a wider pivot window. Port of `SwingDetector.kt:24-27`."""
    return 3 if interval in ("1h", "4h", "1d") else 2


def raw_pivots(
    high: np.ndarray, low: np.ndarray, lookback: int
) -> tuple[np.ndarray, np.ndarray]:
    """Boolean masks of raw fractal highs and lows. Port of `SwingDetector.kt:50-69`.

    Strict on both sides: ``candle[i].high > candle[i±k].high`` for every
    ``k in 1..lookback``. Ties are rejected, matching the ``<=`` / ``>=`` guards.

    A bar can be both a raw high and a raw low (an inside-out bar); the Kotlin
    emits both, high first.
    """
    high = np.asarray(high, dtype="float64")
    low = np.asarray(low, dtype="float64")
    n = high.size

    is_high = np.zeros(n, dtype=bool)
    is_low = np.zeros(n, dtype=bool)
    if n < lookback * 2 + 1:
        return is_high, is_low

    core = slice(lookback, n - lookback)
    cand_high = high[core]
    cand_low = low[core]
    h_ok = np.ones(cand_high.size, dtype=bool)
    l_ok = np.ones(cand_low.size, dtype=bool)

    for k in range(1, lookback + 1):
        left_h = high[lookback - k : n - lookback - k]
        right_h = high[lookback + k : n - lookback + k]
        h_ok &= (cand_high > left_h) & (cand_high > right_h)

        left_l = low[lookback - k : n - lookback - k]
        right_l = low[lookback + k : n - lookback + k]
        l_ok &= (cand_low < left_l) & (cand_low < right_l)

    is_high[core] = h_ok
    is_low[core] = l_ok
    return is_high, is_low


@dataclass(slots=True)
class SwingState:
    """Snapshot of the accepted swing chain as of one bar."""

    last_high: SwingPoint | None
    prev_high: SwingPoint | None
    last_low: SwingPoint | None
    prev_low: SwingPoint | None
    accepted_count: int


class _ZigzagFold:
    """Incremental left-to-right fold of `SwingDetector.filterAlternating`.

    The fold only ever appends to the accepted chain or replaces its tail, and a
    replacement is always same-type — so maintaining separate ``highs``/``lows``
    tails alongside gives O(1) access to the four points structure needs.
    """

    __slots__ = ("_accepted", "_highs", "_lows", "_min_sep")

    def __init__(self, min_separation_atr: float) -> None:
        self._accepted: list[SwingPoint] = []
        self._highs: list[SwingPoint] = []
        self._lows: list[SwingPoint] = []
        self._min_sep = min_separation_atr

    def offer(self, point: SwingPoint, reference_atr: float) -> None:
        """Port of `SwingDetector.kt:80-100` for one incoming raw pivot."""
        if not self._accepted:
            self._push(point)
            return

        last = self._accepted[-1]
        if last.type == point.type:
            # Two highs (or two lows) in a row: keep whichever is more extreme.
            more_extreme = (
                point.price > last.price
                if point.type == SWING_HIGH
                else point.price < last.price
            )
            if more_extreme:
                self._replace_tail(point)
            return

        threshold = (0.0 if np.isnan(reference_atr) else reference_atr) * self._min_sep
        if abs(point.price - last.price) >= threshold:
            self._push(point)

    def _push(self, point: SwingPoint) -> None:
        self._accepted.append(point)
        (self._highs if point.type == SWING_HIGH else self._lows).append(point)

    def _replace_tail(self, point: SwingPoint) -> None:
        self._accepted[-1] = point
        bucket = self._highs if point.type == SWING_HIGH else self._lows
        bucket[-1] = point

    def state(self) -> SwingState:
        return SwingState(
            last_high=self._highs[-1] if self._highs else None,
            prev_high=self._highs[-2] if len(self._highs) > 1 else None,
            last_low=self._lows[-1] if self._lows else None,
            prev_low=self._lows[-2] if len(self._lows) > 1 else None,
            accepted_count=len(self._accepted),
        )

    def accepted(self) -> list[SwingPoint]:
        return self._accepted


def swing_state_series(
    high: np.ndarray,
    low: np.ndarray,
    open_time: np.ndarray,
    atr_values: np.ndarray,
    lookback: int,
    min_separation_atr: float = 0.5,
    keep_snapshots: bool = False,
) -> tuple[list[SwingState], list[list[SwingPoint]]]:
    """Causal per-bar swing state.

    Returns ``(states, accepted_snapshots)`` where element ``t`` is the accepted
    chain as it stood at the close of bar ``t``, using only bars ``<= t``.

    ``keep_snapshots`` is off by default and for good reason: copying the whole
    accepted chain on every bar is O(n x pivots), which on a 240k-bar 1m series
    is billions of operations. Only the multi-timeframe zone builder needs the
    full chain, and it needs it at a handful of bars, not all of them. The
    ``SwingState`` (last/prev high and low) is what everything else reads and is
    maintained in O(1).

    On the ATR reference: the Kotlin falls back to the mean of the **whole
    supplied ATR series** when ``atr[pivot.index]`` is null (`SwingDetector.kt:77`,
    `:97`), which peeks at bars after the pivot. We use a causal expanding mean
    instead — a deliberate divergence recorded in docs/OPEN_QUESTIONS.md Q3.

    The branch is only *entered* during ATR warm-up (the first ``atr_period - 1``
    bars), but its effect on the accepted chain outlives that window: the Kotlin
    re-folds from scratch on every call, so an early pivot accepted at threshold
    0.0 can be retroactively dropped once ATR becomes available, whereas this
    incremental fold freezes it. A differential test against a brute-force
    re-fold-every-prefix reference found **zero** mismatches on realistic gold
    series, and on adversarial input (13 flat bars then a 60-point candle) the
    last diverging bar was t=44 — well inside ``IndicatorConfig.warmup_bars=500``,
    which the engine discards.
    """
    high = np.asarray(high, dtype="float64")
    low = np.asarray(low, dtype="float64")
    open_time = np.asarray(open_time, dtype="int64")
    atr_values = np.asarray(atr_values, dtype="float64")
    n = high.size

    is_high, is_low = raw_pivots(high, low, lookback)

    # Causal expanding mean of non-NaN ATR, for the unreachable fallback branch.
    finite = np.where(np.isnan(atr_values), 0.0, atr_values)
    counts = np.cumsum(~np.isnan(atr_values))
    sums = np.cumsum(finite)
    with np.errstate(invalid="ignore", divide="ignore"):
        expanding_mean = np.where(counts > 0, sums / np.maximum(counts, 1), NaN)

    fold = _ZigzagFold(min_separation_atr)
    states: list[SwingState] = []
    snapshots: list[list[SwingPoint]] = []

    min_bars = lookback * 2 + 1
    for t in range(n):
        if t + 1 >= min_bars:
            i = t - lookback
            if i >= lookback:
                reference = atr_values[i]
                if np.isnan(reference):
                    # `SwingDetector.kt:77` guards with `isFinite() && it > 0.0`;
                    # both halves matter — an infinite reference would make the
                    # separation threshold infinite and reject every pivot.
                    reference = expanding_mean[t]
                    if not np.isfinite(reference) or reference <= 0.0:
                        reference = NaN
                # Kotlin emits HIGH before LOW for the same index.
                if is_high[i]:
                    fold.offer(
                        SwingPoint(i, float(high[i]), int(open_time[i]), SWING_HIGH),
                        reference,
                    )
                if is_low[i]:
                    fold.offer(
                        SwingPoint(i, float(low[i]), int(open_time[i]), SWING_LOW),
                        reference,
                    )
        states.append(fold.state())
        if keep_snapshots:
            snapshots.append(list(fold.accepted()))

    return states, snapshots


def final_accepted_chain(
    high: np.ndarray,
    low: np.ndarray,
    open_time: np.ndarray,
    atr_values: np.ndarray,
    lookback: int,
    min_separation_atr: float = 0.5,
) -> list[SwingPoint]:
    """The accepted swing chain as of the LAST bar, without per-bar snapshots.

    What `SupportResistanceEngine` consumes (`MarketAnalysisEngine.kt:142-144`
    passes ``it.structure.swings``, i.e. the chain at the current bar). Folding
    once to the end is O(n); snapshotting every bar is not.
    """
    is_high, is_low = raw_pivots(high, low, lookback)
    atr_values = np.asarray(atr_values, dtype="float64")
    finite = np.where(np.isnan(atr_values), 0.0, atr_values)
    counts = np.cumsum(~np.isnan(atr_values))
    sums = np.cumsum(finite)
    with np.errstate(invalid="ignore", divide="ignore"):
        expanding_mean = np.where(counts > 0, sums / np.maximum(counts, 1), NaN)

    fold = _ZigzagFold(min_separation_atr)
    n = high.size
    min_bars = lookback * 2 + 1
    for t in range(n):
        if t + 1 < min_bars:
            continue
        i = t - lookback
        if i < lookback:
            continue
        reference = atr_values[i]
        if np.isnan(reference):
            reference = expanding_mean[t]
            if not np.isfinite(reference) or reference <= 0.0:
                reference = NaN
        if is_high[i]:
            fold.offer(
                SwingPoint(i, float(high[i]), int(open_time[i]), SWING_HIGH), reference
            )
        if is_low[i]:
            fold.offer(
                SwingPoint(i, float(low[i]), int(open_time[i]), SWING_LOW), reference
            )
    return fold.accepted()


@dataclass(slots=True)
class StructureSeries:
    state: np.ndarray  # int8: STRUCTURE_*
    bos: np.ndarray  # int8: BIAS_*
    choch: np.ndarray  # int8: BIAS_*
    last_swing_high: np.ndarray  # float64, NaN when unknown
    last_swing_low: np.ndarray
    prev_swing_high: np.ndarray
    prev_swing_low: np.ndarray


def market_structure(
    close: np.ndarray, states: list[SwingState]
) -> StructureSeries:
    """Port of `MarketStructureEngine.kt:45-105`, evaluated at every bar.

    The Kotlin evaluates BOS/CHoCH against ``candles.last().close`` only
    (`:71`); here that is bar ``t`` for each ``t``.

    BOS/CHoCH are **latched states, not edges** — ``close > lastSwingHigh`` stays
    true until a new swing forms. Edge detection is the caller's job
    (docs/OPEN_QUESTIONS.md Q10).
    """
    close = np.asarray(close, dtype="float64")
    n = close.size

    state = np.full(n, STRUCTURE_RANGE, dtype="int8")
    bos = np.full(n, BIAS_NEUTRAL, dtype="int8")
    choch = np.full(n, BIAS_NEUTRAL, dtype="int8")
    last_h = np.full(n, NaN, dtype="float64")
    last_l = np.full(n, NaN, dtype="float64")
    prev_h = np.full(n, NaN, dtype="float64")
    prev_l = np.full(n, NaN, dtype="float64")

    for t in range(n):
        s = states[t]
        lh, ph, ll, pl = s.last_high, s.prev_high, s.last_low, s.prev_low
        if lh is not None:
            last_h[t] = lh.price
        if ph is not None:
            prev_h[t] = ph.price
        if ll is not None:
            last_l[t] = ll.price
        if pl is not None:
            prev_l[t] = pl.price

        higher_high = lh is not None and ph is not None and lh.price > ph.price
        higher_low = ll is not None and pl is not None and ll.price > pl.price
        lower_high = lh is not None and ph is not None and lh.price < ph.price
        lower_low = ll is not None and pl is not None and ll.price < pl.price

        if higher_high and higher_low:
            st = STRUCTURE_BULLISH
        elif lower_high and lower_low:
            st = STRUCTURE_BEARISH
        else:
            st = STRUCTURE_RANGE
        state[t] = st

        c = close[t]
        broke_above = lh is not None and c > lh.price
        broke_below = ll is not None and c < ll.price

        b = ch = BIAS_NEUTRAL
        if st == STRUCTURE_BULLISH:
            if broke_above:
                b = BIAS_BULLISH
            if broke_below:
                ch = BIAS_BEARISH
        elif st == STRUCTURE_BEARISH:
            if broke_below:
                b = BIAS_BEARISH
            if broke_above:
                ch = BIAS_BULLISH
        else:
            # Leaving a range is a breakout, not a change of character.
            # NOTE: when one bar breaks BOTH extremes the second assignment wins
            # and BOS reads BEARISH. Replicated verbatim (OPEN_QUESTIONS Q11).
            if broke_above:
                b = BIAS_BULLISH
            if broke_below:
                b = BIAS_BEARISH
        bos[t] = b
        choch[t] = ch

    return StructureSeries(state, bos, choch, last_h, last_l, prev_h, prev_l)


# ---------------------------------------------------------------------------
# Support / resistance zones  (SupportResistanceEngine.kt)
# ---------------------------------------------------------------------------

ZONE_TOLERANCE_ATR = 0.35  # SupportResistanceEngine.kt:54
MAX_ZONES_PER_SIDE = 3  # :55
TIMEFRAME_WEIGHT = {"4h": 1.0, "1h": 0.8, "30m": 0.5}  # :57-61
_DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class PriceZone:
    low: float
    high: float
    strength: float
    sources: tuple[str, ...]

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def distance_from(self, price: float) -> float:
        """Absolute distance to the nearest edge; 0 when inside. Port of `:20-24`."""
        if price < self.low:
            return self.low - price
        if price > self.high:
            return price - self.high
        return 0.0


@dataclass(frozen=True, slots=True)
class KeyLevels:
    resistance: tuple[PriceZone, ...]
    support: tuple[PriceZone, ...]

    @property
    def nearest_resistance(self) -> PriceZone | None:
        return self.resistance[0] if self.resistance else None

    @property
    def nearest_support(self) -> PriceZone | None:
        return self.support[0] if self.support else None


EMPTY_LEVELS = KeyLevels((), ())


def _session_levels(
    open_time: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> list[tuple[float, float, str]]:
    """Previous-day high/low/close and the current session's extremes so far.

    Port of `SupportResistanceEngine.kt:112-133`. Day bucket is **UTC midnight**
    (`:114-115`) — a boundary no gold session observes, and one that disagrees
    with both `MarketHours` (21:00/22:00 UTC) and the seasonality MYT bucket.
    Replicated verbatim; see docs/OPEN_QUESTIONS.md Q12 / INDICATORS.md §12.
    """
    n = open_time.size
    if n == 0:
        return []

    days = np.floor_divide(open_time, _DAY_MS)
    unique_days = np.unique(days)  # np.unique returns sorted
    out: list[tuple[float, float, str]] = []

    if unique_days.size >= 2:
        prev_day = unique_days[-2]
        mask = days == prev_day
        if mask.any():
            out.append((float(high[mask].max()), 0.9, "Previous day high"))
            out.append((float(low[mask].min()), 0.9, "Previous day low"))
            # `bars.last().close` — last bar of that day in time order.
            out.append((float(close[mask][-1]), 0.6, "Previous day close"))

    today = unique_days[-1]
    mask = days == today
    if mask.any():
        out.append((float(high[mask].max()), 0.7, "Session high"))
        out.append((float(low[mask].min()), 0.7, "Session low"))
    return out


def _cluster(
    levels: list[tuple[float, float, str]], tolerance: float
) -> list[PriceZone]:
    """Greedy chained merge. Port of `SupportResistanceEngine.kt:135-160`.

    Each level is compared against the **previous level in the bucket**, not the
    bucket's origin, so a zone can end up arbitrarily wider than ``tolerance``.
    Replicated verbatim (OPEN_QUESTIONS Q12).
    """
    if not levels:
        return []
    ordered = sorted(levels, key=lambda x: x[0])

    zones: list[PriceZone] = []
    bucket = [ordered[0]]

    def flush() -> None:
        prices = [b[0] for b in bucket]
        seen: list[str] = []
        for _, _, src in bucket:
            if src not in seen:
                seen.append(src)
        zones.append(
            PriceZone(
                low=min(prices),
                high=max(prices),
                strength=sum(b[1] for b in bucket),
                sources=tuple(seen),
            )
        )

    for level in ordered[1:]:
        if abs(level[0] - bucket[-1][0]) <= tolerance:
            bucket.append(level)
        else:
            flush()
            bucket = [level]
    flush()
    return zones


def build_key_levels(
    current_price: float,
    swings_by_timeframe: dict[str, list[SwingPoint]],
    daily_open_time: np.ndarray,
    daily_high: np.ndarray,
    daily_low: np.ndarray,
    daily_close: np.ndarray,
    reference_atr: float,
) -> KeyLevels:
    """Port of `SupportResistanceEngine.kt:68-109`.

    Zones are built from swing structure and session extremes — **never** from the
    UT Dynamic Level, which is a trailing stop and not a level price has ever
    respected (`:46-47`).

    ``reference_atr`` is the H1 ATR-14 in the app (`MarketAnalysisEngine.kt:150`),
    used to size the merge tolerance for *all* timeframes including H4.
    """
    if reference_atr is None or np.isnan(reference_atr):
        return EMPTY_LEVELS
    tolerance = reference_atr * ZONE_TOLERANCE_ATR
    if tolerance <= 0.0:
        return EMPTY_LEVELS

    raw: list[tuple[float, float, str]] = []
    for interval, swings in swings_by_timeframe.items():
        base = TIMEFRAME_WEIGHT.get(interval)
        if base is None:
            continue
        total = len(swings)
        for position, swing in enumerate(swings):
            # Older swings matter less; decay is smooth so there is no cliff.
            age = float(total - 1 - position)
            recency = max(float(np.exp(-age / 12.0)), 0.25)
            kind = "high" if swing.type == SWING_HIGH else "low"
            label = _interval_label(interval)
            raw.append((swing.price, base * recency, f"{label} swing {kind}"))

    raw.extend(
        _session_levels(daily_open_time, daily_high, daily_low, daily_close)
    )
    if not raw:
        return EMPTY_LEVELS

    zones = _cluster(raw, tolerance)

    resistance = [z for z in zones if z.mid > current_price]
    resistance.sort(key=lambda z: -z.strength)
    resistance = resistance[:MAX_ZONES_PER_SIDE]
    resistance.sort(key=lambda z: z.distance_from(current_price))

    support = [z for z in zones if z.mid <= current_price]
    support.sort(key=lambda z: -z.strength)
    support = support[:MAX_ZONES_PER_SIDE]
    support.sort(key=lambda z: z.distance_from(current_price))

    return KeyLevels(tuple(resistance), tuple(support))


def _interval_label(interval: str) -> str:
    """Canonical interval -> the app's `Timeframe.label` (`core/Timeframe.kt:7-11`)."""
    return {
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "4h": "4H",
    }.get(interval, interval)
