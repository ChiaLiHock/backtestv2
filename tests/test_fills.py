"""Fill-rule tests.

These guard the assumptions that decide whether a backtest is honest: which side
slippage falls on, how an ambiguous bar is resolved, and that the pessimistic
fallback actually fires rather than silently doing nothing.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engine import fills
from backtest.engine.config import ExitLeg


# ---------------------------------------------------------------------------
# Slippage always works against the trade
# ---------------------------------------------------------------------------


def test_slippage_direction():
    tick = 0.01
    # Entering long pays up; exiting long is filled down.
    assert fills.apply_slippage(100.0, "long", True, 1, tick) == pytest.approx(100.01)
    assert fills.apply_slippage(100.0, "long", False, 1, tick) == pytest.approx(99.99)
    # Entering short is filled down; exiting short pays up.
    assert fills.apply_slippage(100.0, "short", True, 1, tick) == pytest.approx(99.99)
    assert fills.apply_slippage(100.0, "short", False, 1, tick) == pytest.approx(100.01)


def test_slippage_never_helps():
    """Whatever the side or direction, slippage must not improve the fill."""
    for side in ("long", "short"):
        for entering in (True, False):
            filled = fills.apply_slippage(100.0, side, entering, 3, 0.01)
            favourable = (side == "long") == entering
            if favourable:
                assert filled > 100.0
            else:
                assert filled < 100.0


def test_fee_is_on_notional_both_legs():
    # 5.5 bps on 4600 notional
    assert fills.fee_for(4600.0, 5.5) == pytest.approx(2.53)


# ---------------------------------------------------------------------------
# Exit level resolution
# ---------------------------------------------------------------------------


def test_usd_pnl_levels_scale_with_qty():
    tp, sl = fills.exit_levels(
        "long", 4600.0, 2.0, 10.0,
        ExitLeg(mode="usd_pnl", value=25), ExitLeg(mode="usd_pnl", value=25),
    )
    # $25 at qty 2 is only 12.5 price points.
    assert tp == pytest.approx(4612.5)
    assert sl == pytest.approx(4587.5)


def test_atr_mult_uses_entry_bar_atr_frozen():
    tp, sl = fills.exit_levels(
        "long", 4600.0, 1.0, 14.0,
        ExitLeg(mode="atr_mult", value=1.75), ExitLeg(mode="atr_mult", value=1.75),
    )
    assert tp == pytest.approx(4600 + 1.75 * 14)
    assert sl == pytest.approx(4600 - 1.75 * 14)


def test_short_levels_are_mirrored():
    tp, sl = fills.exit_levels(
        "short", 4600.0, 1.0, 10.0,
        ExitLeg(mode="usd_pnl", value=25), ExitLeg(mode="usd_pnl", value=25),
    )
    assert tp == pytest.approx(4575.0)   # profit is DOWN for a short
    assert sl == pytest.approx(4625.0)


def test_rr_take_profit_measures_against_the_stop():
    tp, sl = fills.exit_levels(
        "long", 100.0, 1.0, 5.0,
        ExitLeg(mode="rr", value=2.0), ExitLeg(mode="price_delta", value=10.0),
    )
    assert sl == pytest.approx(90.0)
    assert tp == pytest.approx(120.0)   # 2x the 10-point stop


def test_rr_without_a_stop_is_rejected():
    with pytest.raises(ValueError, match="needs a stop_loss"):
        fills.exit_levels("long", 100.0, 1.0, 5.0, ExitLeg(mode="rr", value=2.0), None)


# ---------------------------------------------------------------------------
# Bar resolution
# ---------------------------------------------------------------------------


def test_untouched_bar_returns_none():
    assert fills.resolve_bar("long", 100, 101, 99, 110, 90) is None


def test_clean_target_and_clean_stop():
    tp = fills.resolve_bar("long", 100, 111, 99, 110, 90)
    assert tp is not None and tp.reason == "tp" and not tp.ambiguous

    sl = fills.resolve_bar("long", 100, 101, 89, 110, 90)
    assert sl is not None and sl.reason == "sl" and not sl.ambiguous


def test_ambiguous_bar_without_minute_data_assumes_stop_first():
    """The pessimistic default. Assuming the target would flatter the strategy."""
    r = fills.resolve_bar("long", 100, 111, 89, 110, 90)
    assert r is not None
    assert r.reason == "sl"
    assert r.ambiguous is True


def test_ambiguous_bar_resolved_by_minute_replay_target_first():
    """1m replay must be able to award the TARGET, not just confirm the stop —
    otherwise the resolver is indistinguishable from the pessimistic fallback."""
    # Minute path: up through 110 first, only later down through 90.
    mh = np.array([105.0, 111.0, 100.0, 95.0])
    ml = np.array([ 99.0, 104.0,  95.0, 89.0])
    r = fills.resolve_bar("long", 100, 111, 89, 110, 90, mh, ml)
    assert r is not None
    assert r.reason == "tp"
    assert r.ambiguous is True


def test_ambiguous_bar_resolved_by_minute_replay_stop_first():
    mh = np.array([101.0, 100.0, 108.0, 111.0])
    ml = np.array([ 99.0,  89.0,  95.0, 104.0])
    r = fills.resolve_bar("long", 100, 111, 89, 110, 90, mh, ml)
    assert r is not None and r.reason == "sl" and r.ambiguous is True


def test_single_minute_containing_both_falls_back_to_policy():
    """If even one minute spans both levels we cannot order them. Say so by
    falling back, rather than picking the flattering side."""
    mh = np.array([111.0])
    ml = np.array([89.0])
    r = fills.resolve_bar("long", 100, 111, 89, 110, 90, mh, ml)
    assert r is not None and r.reason == "sl" and r.ambiguous is True

    r2 = fills.resolve_bar(
        "long", 100, 111, 89, 110, 90, mh, ml, ambiguous_policy="target_first"
    )
    assert r2 is not None and r2.reason == "tp" and r2.ambiguous is True


def test_short_side_ambiguity():
    # Short: target BELOW, stop ABOVE.
    mh = np.array([101.0, 111.0])
    ml = np.array([ 89.0, 100.0])
    r = fills.resolve_bar("short", 100, 111, 89, 90, 110, mh, ml)
    assert r is not None and r.reason == "tp"   # 89 <= 90 reached in minute 0


# ---------------------------------------------------------------------------
# Excursions
# ---------------------------------------------------------------------------


def test_excursions_long():
    mae, mfe = fills.excursions(
        "long", 100.0, 2.0, np.array([104.0, 106.0]), np.array([98.0, 95.0])
    )
    assert mfe == pytest.approx(12.0)   # (106 - 100) * 2
    assert mae == pytest.approx(-10.0)  # (95 - 100) * 2


def test_excursions_short_are_inverted():
    mae, mfe = fills.excursions(
        "short", 100.0, 1.0, np.array([104.0]), np.array([95.0])
    )
    assert mfe == pytest.approx(5.0)
    assert mae == pytest.approx(-4.0)


def test_mae_is_never_positive_and_mfe_never_negative():
    mae, mfe = fills.excursions(
        "long", 100.0, 1.0, np.array([101.0]), np.array([100.5])
    )
    assert mae <= 0.0 and mfe >= 0.0
