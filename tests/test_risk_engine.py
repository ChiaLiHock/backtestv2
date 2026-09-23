"""Tests for the `RiskExecutiveEngine.kt` port and its gate.

Three things here are worth more than the rest:

* **the gate can only subtract** — if it could ever turn a non-firing bar into a
  trade, the unmeasured weights would have become the trigger by accident;
* **a missing input must not read as safety** — no order book and no open
  interest is the normal state on this feed, and the failure that matters is a
  Danger of 0 that means "we could not measure it";
* **ties in the double-barrier sim resolve as SL first** — the same conservative
  tie-break `tools/validate_rule.py` uses, so the two agree.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engine import risk_engine as R


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _series(n=400, start=100.0, drift=0.0, seed=7):
    """A deterministic OHLC walk. No randomness leaks between tests."""
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(drift, 0.4, n))
    high = close + np.abs(rng.normal(0, 0.25, n))
    low = close - np.abs(rng.normal(0, 0.25, n))
    open_ = np.concatenate([[start], close[:-1]])
    return open_, high, low, close


def _inputs(**kw):
    o, h, l, c = _series()
    base = dict(
        spot=float(c[-1]), open_=o, high=h, low=l, close=c,
        ema_7=float(c[-1]), ema_14=float(c[-1]), ema_28=float(c[-1]),
        ema_28_series=np.full(c.size, float(c.mean())),
        rsi_series=np.full(c.size, 50.0),
        atr=1.0,
        tf_signals=(R.TfCandleSignal("1h"), R.TfCandleSignal("4h")),
    )
    base.update(kw)
    return R.RiskInputs(**base)


# ---------------------------------------------------------------------------
# wick bias
# ---------------------------------------------------------------------------


class TestWickBias:
    def test_long_upper_wick_is_bearish_rejection(self):
        # open 100, close 101, high 110, low 99.8 -> price ran up and was sold
        assert R.wick_bias(100, 110, 99.8, 101) == R.BEARISH_REJECTION

    def test_long_lower_wick_is_bullish_rejection(self):
        assert R.wick_bias(100, 100.2, 90, 99) == R.BULLISH_REJECTION

    def test_a_plain_trend_candle_is_neutral(self):
        # big body, tiny wicks
        assert R.wick_bias(100, 110.1, 99.9, 110) == R.WICK_NEUTRAL

    def test_a_doji_with_no_range_is_neutral_not_a_crash(self):
        assert R.wick_bias(100, 100, 100, 100) == R.WICK_NEUTRAL

    def test_both_tests_must_pass(self):
        """A long candle with a small tail is not a rejection.

        The upper wick here beats the body ratio against a small body but is a
        thin slice of the range; the range test is what rejects it. Without both
        tests every long candle with any tail would vote.
        """
        # body 100->104, high 105, low 99: upper 1.0, rng 6 -> 0.17 of range
        assert R.wick_bias(100, 105, 99, 104) == R.WICK_NEUTRAL

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_non_finite_is_neutral(self, bad):
        assert R.wick_bias(bad, 110, 99, 101) == R.WICK_NEUTRAL


# ---------------------------------------------------------------------------
# the danger matrix
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_scores_are_percentages(self):
        s = R.evaluate(_inputs())
        for score in (s.long, s.short):
            assert 0 <= score.danger <= 100
            assert 0 <= score.confidence <= 100

    def test_weights_are_the_kotlin_matrix(self):
        assert (R.W_TREND, R.W_ORDERBOOK, R.W_MOMENTUM) == (0.40, 0.30, 0.30)
        assert R.W_TREND + R.W_ORDERBOOK + R.W_MOMENTUM == pytest.approx(1.0)

    def test_below_every_ema_reads_bearish(self):
        s = R.evaluate(_inputs(spot=50.0, ema_7=100.0, ema_14=101.0,
                               ema_28=102.0, ema_50=103.0, ema_200=104.0))
        assert s.bias == R.BEARISH

    def test_above_every_ema_reads_bullish(self):
        s = R.evaluate(_inputs(spot=200.0, ema_7=100.0, ema_14=99.0,
                               ema_28=98.0, ema_50=97.0, ema_200=96.0))
        assert s.bias == R.BULLISH

    def test_no_emas_is_neutral_not_bullish(self):
        """`RiskExecutiveEngine.kt:160-163`, and the reason it is written down.

        An EMPTY ema list makes `below_all` 0.0, which the bias arithmetic would
        otherwise read as "price is above all EMAs" — a false bullish produced by
        having no data at all. That is the single worst direction for this bug.
        """
        s = R.evaluate(_inputs(ema_7=None, ema_14=None, ema_28=None,
                               ema_50=None, ema_200=None))
        assert s.bias == R.NEUTRAL

    def test_long_side_is_dangerous_when_price_sits_under_the_stack(self):
        under = R.evaluate(_inputs(spot=50.0, ema_7=100.0, ema_14=101.0, ema_28=102.0))
        over = R.evaluate(_inputs(spot=200.0, ema_7=100.0, ema_14=99.0, ema_28=98.0))
        assert under.long.danger > over.long.danger

    def test_reasons_are_never_empty(self):
        s = R.evaluate(_inputs())
        assert s.long.reasons and s.short.reasons

    def test_factor_points_sum_close_to_danger_before_the_sim_blend(self):
        """The three factor point totals are the pre-blend danger, by construction."""
        s = R.evaluate(_inputs(ema_28_series=None))   # no sim -> no blend
        assert s.historical_sim is None
        total = s.long.trend_pts + s.long.orderbook_pts + s.long.momentum_pts
        assert abs(total - s.long.danger) <= 2      # rounding of three terms


class TestMissingInputsAreNotSafety:
    def test_absent_oi_is_reported_not_silently_zero(self):
        s = R.evaluate(_inputs(oi_deviation_pct=None))
        assert "open interest" in s.missing_inputs

    def test_absent_oi_reweights_rather_than_dropping_the_term(self):
        """Weights must still sum to 1.0 with the OI quarter removed.

        If the missing term simply scored 0, momentum danger would be capped at
        75% of what it can measure — a feed limitation reading as evidence of a
        safer market.
        """
        with_oi = R._momentum_danger(chase=1.0, knife=1.0, oi=1.0,
                                     vol_amp=0.0, vol_danger=0.0)
        without = R._momentum_danger(chase=1.0, knife=1.0, oi=None,
                                     vol_amp=0.0, vol_danger=0.0)
        assert without == pytest.approx(with_oi)

    def test_zero_oi_and_missing_oi_are_different(self):
        zero = R._momentum_danger(chase=1.0, knife=0.0, oi=0.0,
                                  vol_amp=0.0, vol_danger=0.0)
        missing = R._momentum_danger(chase=1.0, knife=0.0, oi=None,
                                     vol_amp=0.0, vol_danger=0.0)
        assert missing > zero

    def test_completeness_falls_when_inputs_are_absent(self):
        full = _inputs(oi_deviation_pct=10.0, long_ratio=0.5, short_ratio=0.5,
                       resistance=(R.Zone(110, 111, 2.0),),
                       support=(R.Zone(90, 91, 2.0),),
                       tf_signals=tuple(R.TfCandleSignal(t)
                                        for t in ("5m", "15m", "1h")))
        bare = _inputs()
        assert R._data_completeness(full)[0] > R._data_completeness(bare)[0]


class TestZoneProxy:
    def test_a_resistance_inside_the_target_raises_long_danger(self):
        c = _inputs().close
        spot = float(c[-1])
        near = R.evaluate(_inputs(
            resistance=(R.Zone(spot * 1.001, spot * 1.002, 5.0),),
            support=(R.Zone(spot * 0.90, spot * 0.905, 5.0),), atr=spot * 0.02))
        far = R.evaluate(_inputs(
            resistance=(R.Zone(spot * 1.30, spot * 1.31, 5.0),),
            support=(R.Zone(spot * 0.90, spot * 0.905, 5.0),), atr=spot * 0.02))
        assert near.long.danger > far.long.danger

    def test_a_zone_containing_spot_is_zero_distance(self):
        z = R.Zone(99.0, 101.0, 1.0)
        assert z.distance_from(100.0) == 0.0
        assert z.distance_from(102.0) == pytest.approx(1.0)
        assert z.distance_from(98.0) == pytest.approx(1.0)

    def test_snapshot_always_declares_the_proxy(self):
        """The page must never be able to show this as a real order book."""
        assert R.evaluate(_inputs()).orderbook_is_proxy is True


# ---------------------------------------------------------------------------
# the double-barrier simulation
# ---------------------------------------------------------------------------


class TestSimulate:
    def _flat(self, n=500):
        c = np.full(n, 100.0)
        return R.RiskInputs(
            spot=100.0, open_=c.copy(), high=c.copy(), low=c.copy(), close=c.copy(),
            ema_28_series=np.full(n, 100.0), rsi_series=np.full(n, 50.0), atr=1.0)

    def test_too_little_history_returns_none(self):
        small = self._flat(20)
        assert R.simulate(small, 1.0, 1.0, R.RiskSettings()) is None

    def test_a_flat_market_touches_no_barrier(self):
        sim = R.simulate(self._flat(), 1.0, 1.0, R.RiskSettings())
        assert sim is not None and sim.matches > 0
        assert sim.long_tp_first == sim.long_sl_first == 0

    def test_a_bar_touching_both_barriers_counts_as_sl_first(self):
        """The conservative tie-break, matching `walk_forward`'s.

        Every bar after the warm-up spans both barriers. Reading a straddling bar
        as TP would make every ambiguous trade a winner, which is exactly the bug
        that makes a backtest look profitable and an account not.
        """
        n = 400
        c = np.full(n, 100.0)
        inp = R.RiskInputs(
            spot=100.0, open_=c.copy(),
            high=np.full(n, 130.0), low=np.full(n, 70.0), close=c.copy(),
            ema_28_series=np.full(n, 100.0), rsi_series=np.full(n, 50.0), atr=1.0)
        sim = R.simulate(inp, 5.0, 5.0, R.RiskSettings(sim_horizon=10))
        assert sim.long_tp_first == 0 and sim.long_sl_first > 0
        assert sim.short_tp_first == 0 and sim.short_sl_first > 0

    def test_a_thin_match_set_is_not_blended(self):
        sim = R.HistoricalSim(matches=3, long_tp_first=3, long_sl_first=0,
                              short_tp_first=0, short_sl_first=3,
                              lookback_label="x", horizon_label="y", summary="")
        assert sim.long_sl_first_prob() is None      # below SIM_MIN_MATCHES

    def test_the_blend_is_twenty_percent(self):
        assert R.SIM_BLEND == 0.20
        # base 0.0 danger, sim says SL-first always -> 20 points of danger
        assert R._blend_danger(0.0, 1.0) == 20
        assert R._blend_danger(0.0, None) == 0

    def test_rsi_zone_vector_matches_the_scalar(self):
        vals = np.array([np.nan, 5, 29.9, 30, 44.9, 45, 54.9, 55, 69.9, 70, 99])
        got = R._rsi_zone_arr(vals)
        want = [R._rsi_zone(float(v)) for v in vals]
        assert list(got) == want


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def _snap(danger=10, confidence=90, bias=R.BULLISH, missing=()):
    def s(d):
        return R.RiskDirectionScore(d, danger, confidence, 0, 0, 0, ())
    return R.RiskSnapshot(long=s(R.LONG), short=s(R.SHORT), bias=bias,
                          historical_sim=None, data_confidence_note="",
                          tp_pct=1.0, sl_pct=0.5, missing_inputs=missing)


class TestGate:
    def test_a_calm_read_passes(self):
        assert R.gate(_snap(), "long").passed

    def test_high_danger_vetoes(self):
        g = R.gate(_snap(danger=95), "long")
        assert not g.passed and "danger" in g.vetoes[0]

    def test_low_confidence_vetoes(self):
        g = R.gate(_snap(confidence=1), "long")
        assert not g.passed and "confidence" in g.vetoes[0]

    def test_bias_is_not_checked_unless_asked(self):
        assert R.gate(_snap(bias=R.BEARISH), "long").passed
        strict = R.GateSettings(require_bias=True)
        assert not R.gate(_snap(bias=R.BEARISH), "long", strict).passed

    def test_disabling_the_gate_passes_everything(self):
        off = R.GateSettings(enabled=False)
        assert R.gate(_snap(danger=100, confidence=0), "long", off).passed

    def test_the_gate_returns_a_verdict_not_a_signal(self):
        """It has no way to say "enter" — only `passed` on something already fired.

        This is the structural guarantee that the unmeasured weights cannot
        become the trigger. A `GateResult` carries no price, no side and no size.
        """
        g = R.gate(_snap(), "long")
        assert not hasattr(g, "entry") and not hasattr(g, "fired")
        assert set(g.as_dict()) == {"passed", "danger", "confidence", "bias", "vetoes"}

    def test_default_thresholds_stay_loose(self):
        """Tightening these spends a measured edge to buy an unmeasured one."""
        d = R.GateSettings()
        assert d.max_danger >= 70 and d.min_confidence <= 25
        assert d.require_bias is False
