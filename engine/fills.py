"""Fill rules and intrabar ambiguity resolution.

This is the module that decides whether a backtest is honest. Three rules:

1. **Signal on close, fill on the next open.** A condition confirmed at the close
   of bar ``t`` is not tradable at that price — the bar is already over. Fill at
   the open of ``t+1`` plus slippage.

2. **TP/SL are checked with the bar's own high/low**, never its close.

3. **Ambiguous bars get replayed at 1-minute resolution.** When a bar's range
   spans both the target and the stop, the daily/hourly bar cannot say which came
   first, and guessing "target" is how a losing strategy backtests profitably.
   Measured on this dataset: within 24 h of a random 1H close, both a +$25 and a
   -$25 excursion occur 22% of the time — so this is the common case, not an edge
   case. If 1m data is missing for that window we assume the **stop** filled
   first and flag the trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Side = Literal["long", "short"]
ExitReason = Literal["tp", "sl", "trail", "time_stop", "end_of_data"]


@dataclass(frozen=True, slots=True)
class FillResult:
    price: float
    reason: ExitReason
    ambiguous: bool


def apply_slippage(price: float, side: Side, entering: bool, ticks: int, tick_size: float) -> float:
    """Slippage always works against the trade.

    Entering long / exiting short pays UP; entering short / exiting long pays DOWN.
    """
    adverse_up = (side == "long") == entering
    delta = ticks * tick_size
    return price + delta if adverse_up else price - delta


def fee_for(notional: float, bps: float) -> float:
    return abs(notional) * bps / 10_000.0


def touched(
    side: Side, high: float, low: float, target: float | None, stop: float | None
) -> tuple[bool, bool]:
    """Did this bar's range reach the target / the stop?"""
    if side == "long":
        hit_tp = target is not None and high >= target
        hit_sl = stop is not None and low <= stop
    else:
        hit_tp = target is not None and low <= target
        hit_sl = stop is not None and high >= stop
    return hit_tp, hit_sl


def resolve_bar(
    side: Side,
    bar_open: float,
    high: float,
    low: float,
    target: float | None,
    stop: float | None,
    minute_high: np.ndarray | None = None,
    minute_low: np.ndarray | None = None,
    ambiguous_policy: str = "stop_first",
) -> FillResult | None:
    """Decide what, if anything, this bar did to an open position.

    ``minute_high`` / ``minute_low`` are the 1m bars *inside* this bar, ascending.
    Pass ``None`` when 1m data is unavailable for the window.
    """
    hit_tp, hit_sl = touched(side, high, low, target, stop)

    if not hit_tp and not hit_sl:
        return None
    if hit_tp and not hit_sl:
        return FillResult(float(target), "tp", False)   # type: ignore[arg-type]
    if hit_sl and not hit_tp:
        return FillResult(float(stop), "sl", False)     # type: ignore[arg-type]

    # Both touched inside one bar. Replay at 1m.
    if minute_high is not None and minute_low is not None and minute_high.size:
        for i in range(minute_high.size):
            m_tp, m_sl = touched(side, float(minute_high[i]), float(minute_low[i]), target, stop)
            if m_tp and m_sl:
                # Still ambiguous even at 1m — one minute contained both. Fall
                # through to the pessimistic policy rather than pretending.
                break
            if m_tp:
                return FillResult(float(target), "tp", True)   # type: ignore[arg-type]
            if m_sl:
                return FillResult(float(stop), "sl", True)     # type: ignore[arg-type]

    if ambiguous_policy == "target_first":
        return FillResult(float(target), "tp", True)  # type: ignore[arg-type]
    return FillResult(float(stop), "sl", True)        # type: ignore[arg-type]


def resolve_bar_with_ltf(
    side: Side,
    target: float | None,
    stop: float | None,
    ltf_high: np.ndarray,
    ltf_low: np.ndarray,
    ltf_close: np.ndarray,
    ltf_ema: np.ndarray,
    breaks_needed: int,
    breaks_so_far: int,
    armed: bool = True,
    minute_slices: list[tuple[np.ndarray, np.ndarray]] | None = None,
    ambiguous_policy: str = "stop_first",
) -> tuple[FillResult | None, int, bool]:
    """Walk the lower-timeframe sub-bars inside one strategy bar, in order.

    Returns ``(fill_or_None, updated_break_count, armed)``.

    **Arming.** A pullback entry is, by definition, taken while price is on the
    wrong side of a fast moving average — that is what a pullback is. So an
    "exit when the LTF closes through the EMA" rule is already true at the moment
    of entry and fires on the first sub-bar, holding every trade for minutes.
    (Measured before this was added: 169 of 171 exits were the trail, average
    hold 1.7 bars.) When ``armed`` starts False the trail stays dormant until the
    LTF first closes back on the favourable side — the pullback resolving — and
    only the hard stop is live until then.

    Ordering within each sub-bar is deliberate:

    1. **Stop / target first, from the sub-bar's high and low.** These are
       intrabar events — a stop would have filled while the sub-bar was still
       forming, before anyone could know where it would close.
    2. **Then the EMA break, from that sub-bar's close.** A close-based signal
       cannot be acted on until the close exists.

    Getting this backwards would let a trade escape a stop because the bar it was
    stopped on later closed above the EMA, which is the flattering direction and
    exactly the sort of thing that makes a backtest lie.
    """
    n = ltf_close.size
    for i in range(n):
        hit_tp, hit_sl = touched(side, float(ltf_high[i]), float(ltf_low[i]), target, stop)

        if hit_tp and hit_sl:
            mh = ml = None
            if minute_slices is not None and i < len(minute_slices):
                mh, ml = minute_slices[i]
            filled = resolve_bar(
                side, float(ltf_close[i]), float(ltf_high[i]), float(ltf_low[i]),
                target, stop, mh, ml, ambiguous_policy,
            )
            if filled is not None:
                return filled, breaks_so_far, armed
        elif hit_sl:
            return FillResult(float(stop), "sl", False), breaks_so_far, armed  # type: ignore[arg-type]
        elif hit_tp:
            return FillResult(float(target), "tp", False), breaks_so_far, armed  # type: ignore[arg-type]

        ema = ltf_ema[i]
        if ema != ema:  # NaN — the EMA has not warmed up on this sub-bar
            continue
        broke = ltf_close[i] < ema if side == "long" else ltf_close[i] > ema

        if not armed:
            # Waiting for the pullback to resolve: the first close back on the
            # favourable side arms the trail. Nothing can exit on the trail here.
            if not broke:
                armed = True
            continue

        if broke:
            breaks_so_far += 1
            if breaks_so_far >= breaks_needed:
                return FillResult(float(ltf_close[i]), "trail", False), breaks_so_far, armed
        else:
            breaks_so_far = 0   # the run must be consecutive

    return None, breaks_so_far, armed


def exit_levels(
    side: Side,
    entry_price: float,
    qty: float,
    entry_atr: float,
    take_profit,
    stop_loss,
) -> tuple[float | None, float | None]:
    """Resolve the configured TP/SL legs into absolute prices.

    ``atr_mult`` uses the ATR as of the **entry bar**, frozen for the life of the
    trade. A trailing ATR would be a different feature and is configured
    separately.
    """
    def distance(leg, other_distance: float | None) -> float | None:
        if leg is None:
            return None
        if leg.mode == "usd_pnl":
            if qty <= 0:
                raise ValueError("qty must be positive for usd_pnl exits")
            return leg.value / qty
        if leg.mode == "price_delta":
            return leg.value
        if leg.mode == "atr_mult":
            if entry_atr != entry_atr:  # NaN
                return None
            return leg.value * entry_atr
        if leg.mode == "rr":
            if other_distance is None:
                raise ValueError("rr take_profit needs a stop_loss to measure against")
            return leg.value * other_distance
        raise ValueError(f"unknown exit mode {leg.mode!r}")

    stop_distance = distance(stop_loss, None)
    tp_distance = distance(take_profit, stop_distance)

    sign = 1.0 if side == "long" else -1.0
    target = entry_price + sign * tp_distance if tp_distance is not None else None
    stop = entry_price - sign * stop_distance if stop_distance is not None else None
    return target, stop


def excursions(
    side: Side, entry_price: float, qty: float, highs: np.ndarray, lows: np.ndarray
) -> tuple[float, float]:
    """(MAE, MFE) in quote currency, over the bars the position was open.

    MAE is reported as a negative number — the worst the trade ever looked.
    """
    if highs.size == 0:
        return 0.0, 0.0
    if side == "long":
        mfe = (float(np.max(highs)) - entry_price) * qty
        mae = (float(np.min(lows)) - entry_price) * qty
    else:
        mfe = (entry_price - float(np.min(lows))) * qty
        mae = (entry_price - float(np.max(highs))) * qty
    return min(mae, 0.0), max(mfe, 0.0)
