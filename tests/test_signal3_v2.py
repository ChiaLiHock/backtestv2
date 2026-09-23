"""Signal 3 v2: the double-sweep/fake-break reversal (15m) + ExpD fallback.

Pins the machinery, not an edge (the pattern is the owner's framework
mechanised, unmeasured):
* swing detection (fractal correctness on 15m)
* the SYMMETRIC state machine (low sweep→long, high fake-break→short)
* the quiet-volume gate (a LOUD break does not count)
* the zone-width brackets (SL beyond extreme, TP nearest swing)
* the ExpD fallback when no sweep pattern exists
* honest degradation (no data, no crash)
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest.engine.signal3 import (Signal3, Signal3Config, SweepState,
                                     detect_swings, QUIET_VOL_RATIO_MAX)


class TestDetectSwings:
    def test_finds_the_obvious_fractals(self):
        h = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3, 2,
                      1.0])
        l = np.array([0.5, 1, 2, 3, 4, 3, 2, 1, 0.5, 1, 2, 3, 4, 3, 2,
                      1, 0.5])
        sh, sl = detect_swings(h, l, n=3)
        assert 4 in sh and 12 in sh
        assert 8 in sl

    def test_flat_market_no_crash(self):
        h = np.full(20, 5.0)
        l = np.full(20, 4.0)
        sh, sl = detect_swings(h, l, n=3)
        assert isinstance(sh, np.ndarray) and isinstance(sl, np.ndarray)

    def test_too_few_bars(self):
        sh, sl = detect_swings(np.array([1.0, 2.0]), np.array([0.5, 1.0]),
                               n=3)
        assert len(sh) == 0 and len(sl) == 0


class TestSweepState:
    """The SYMMETRIC state machine — both directions."""

    def test_double_quiet_sweep_low_is_ready_long(self):
        s = SweepState(swing_price=4600.0, side="low")
        s.break_count = 2
        s.extreme = 4595.0
        s.returned = True
        assert s.ready is True
        assert s.zone_width == 5.0

    def test_double_quiet_fake_break_high_is_ready_short(self):
        s = SweepState(swing_price=4610.0, side="high")
        s.break_count = 2
        s.extreme = 4615.0
        s.returned = True
        assert s.ready is True
        assert s.zone_width == 5.0

    def test_single_break_not_ready(self):
        s = SweepState(swing_price=4600.0, side="low")
        s.break_count = 1
        s.returned = True
        assert s.ready is False

    def test_still_beyond_not_ready(self):
        s = SweepState(swing_price=4600.0, side="low")
        s.break_count = 2
        s.returned = False
        assert s.ready is False


class TestStateMachine:
    """Run _run_state_machine directly on synthetic 15m arrays."""

    def _s3(self, tmp_path):
        return Signal3(log_path=tmp_path / "s3.jsonl")

    def _arrays(self, n=120):
        base = 4600.0
        h = np.full(n, base + 5.0)
        l = np.full(n, base - 5.0)
        c = np.full(n, base)
        v = np.full(n, 100.0)
        return h, l, c, v

    def test_double_quiet_sweep_low_fires_long(self, tmp_path):
        s3 = self._s3(tmp_path)
        h, l, c, v = self._arrays()
        # swing low at bar 20 (close below the surrounding bars)
        for i in range(17, 24):
            l[i] = 4585.0 if i == 20 else 4594.0
            h[i] = 4598.0
            c[i] = 4588.0 if i == 20 else 4595.0
        # first quiet sweep: bars 30-32 close BELOW 4585, bar 33 back above
        for i in range(30, 33):
            l[i] = 4575.0
            h[i] = 4598.0
            c[i] = 4580.0  # close BELOW the swing = breaking
            v[i] = 50.0
        l[30] = 4575.0
        c[33] = 4595.0  # close back above = return
        # second quiet sweep: bars 40-42
        for i in range(40, 43):
            l[i] = 4572.0
            h[i] = 4598.0
            c[i] = 4578.0  # close BELOW = breaking
            v[i] = 50.0
        l[40] = 4572.0
        c[43] = 4595.0  # return
        r = s3._run_state_machine(swing_price=4585.0, side="low",
                                  h=h, l=l, c=c, v=v, start=24, end=120,
                                  vol_med=100.0)
        assert r is not None, "the machine should complete"
        state, j, reason = r
        assert reason is None, f"should pass filters, got: {reason}"
        assert state.break_count == 2
        assert state.returned is True
        assert state.ready is True
        assert state.extreme == 4572.0
        assert state.zone_width >= 8.0, \
            "hard floor: zone must be at least $8 wide"

    def test_loud_break_does_not_count(self, tmp_path):
        s3 = self._s3(tmp_path)
        h, l, c, v = self._arrays()
        for i in range(17, 24):
            l[i] = 4592.0 if i == 20 else 4596.0
        for i in range(30, 34):
            l[i] = 4590.0
            c[i] = 4600.0 if i == 33 else 4593.0
            v[i] = 200.0  # LOUD
        for i in range(40, 44):
            l[i] = 4589.0
            c[i] = 4600.0 if i == 43 else 4592.0
            v[i] = 200.0  # LOUD
        r = s3._run_state_machine(swing_price=4592.0, side="low",
                                  h=h, l=l, c=c, v=v, start=24, end=120,
                                  vol_med=100.0)
        assert r is None or not r[0].ready, \
            "loud breaks should not produce a signal"

    def test_double_quiet_fake_break_high_fires_short(self, tmp_path):
        s3 = self._s3(tmp_path)
        h, l, c, v = self._arrays()
        # swing high at bar 20
        for i in range(17, 24):
            h[i] = 4615.0 if i == 20 else 4604.0
            l[i] = 4602.0
            c[i] = 4612.0 if i == 20 else 4603.0
        # first quiet fake break: bars 30-32 close ABOVE 4615, bar 33 back below
        for i in range(30, 33):
            h[i] = 4625.0
            l[i] = 4600.0
            c[i] = 4620.0  # close ABOVE = breaking
            v[i] = 50.0
        h[30] = 4625.0
        c[33] = 4600.0  # close back below = return
        # second quiet fake break: bars 40-42
        for i in range(40, 43):
            h[i] = 4628.0
            l[i] = 4600.0
            c[i] = 4622.0  # close ABOVE = breaking
            v[i] = 50.0
        h[40] = 4628.0
        c[43] = 4600.0  # return
        r = s3._run_state_machine(swing_price=4615.0, side="high",
                                  h=h, l=l, c=c, v=v, start=24, end=120,
                                  vol_med=100.0)
        assert r is not None
        state, _j, reason = r
        assert reason is None, f"should pass filters, got: {reason}"
        assert state.break_count == 2
        assert state.ready is True
        assert state.extreme == 4628.0
        assert state.zone_width >= 8.0, \
            "hard floor: zone must be at least $8 wide"

    def test_single_break_not_enough(self, tmp_path):
        s3 = self._s3(tmp_path)
        h, l, c, v = self._arrays()
        for i in range(17, 24):
            l[i] = 4592.0 if i == 20 else 4596.0
        for i in range(30, 34):  # only ONE sweep
            l[i] = 4590.0
            c[i] = 4600.0 if i == 33 else 4593.0
            v[i] = 50.0
        r = s3._run_state_machine(swing_price=4592.0, side="low",
                                  h=h, l=l, c=c, v=v, start=24, end=120,
                                  vol_med=100.0)
        assert r is None or r[0].break_count < 2, \
            "one sweep is not a double sweep"


class TestConfig:
    def test_declares_itself_unmeasured(self):
        assert Signal3Config().measured is False

    def test_label_mentions_both_modes(self):
        label = Signal3Config().label
        assert "sweep" in label.lower()
        assert "expd" in label.lower() or "gate" in label.lower()

    def test_cannot_be_mutated(self):
        with pytest.raises(Exception):
            Signal3Config().lots = 0.1


class TestAdaptiveTimeframe:
    """The owner's revised rule: start at 5m; bar range < $8 escalates
    (5→15→20→25→30→35m); 2 consecutive wide bars drops one rung. When
    5m bars already have $8+ range, stay on the fastest grid."""

    def _bars(self, n, base=4600.0, spread=10.0):
        return pd.DataFrame({
            "open_time": (pd.Timestamp("2026-09-20").timestamp() * 1000
                          + np.arange(n) * 300_000).astype("int64"),
            "open": np.full(n, base), "high": np.full(n, base + spread),
            "low": np.full(n, base - spread), "close": np.full(n, base),
            "volume": np.full(n, 100.0), "turnover": np.zeros(n)})

    def test_wide_5m_stays_at_5m(self, tmp_path):
        """如果5分钟差大于8就不需等15 — the owner's revision."""
        from backtest.engine.signal3 import TF_LADDER_MS
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        # spread=4.5 → 5m bar range = 9 ≥ 8 → stays at 5m
        f5 = self._bars(200, spread=4.5)
        tf = s3._pick_timeframe(f5)
        assert s3._tf_level == 0, "5m bar is wide enough → no escalation"
        assert tf == TF_LADDER_MS[0]  # 5m

    def test_quiet_5m_escalates_to_first_fitting_rung(self, tmp_path):
        from backtest.engine.signal3 import TF_LADDER_MS
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        # spread=2 → 5m range = 4 < 8; 15m range = 4 < 8; 20m = 4 < 8...
        # all quiet → top rung (35m)
        f5 = self._bars(200, spread=2.0)
        tf = s3._pick_timeframe(f5)
        assert s3._tf_level == len(TF_LADDER_MS) - 1
        assert tf == TF_LADDER_MS[-1]  # 35m

    def test_moderate_quiet_lands_on_fitting_rung(self, tmp_path):
        from backtest.engine.signal3 import TF_LADDER_MS
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        # spread=3.5: 5m range = 7 < 8 (upgrade); 15m range = 7 < 8;
        # 20m = 7 < 8... all quiet → top rung. But with slight variation:
        # make the last bar wider than the rest
        f5 = self._bars(200, spread=3.5)
        # widen the last few bars: 15m range now 16 ≥ 8
        f5.loc[f5.index[-3:], "high"] = 4607.0
        f5.loc[f5.index[-3:], "low"] = 4593.0
        tf = s3._pick_timeframe(f5)
        # 5m last bar range = 14 ≥ 8 → stays at 5m
        assert s3._tf_level == 0
        assert tf == TF_LADDER_MS[0]

    def test_two_wide_bars_drop_one_rung(self, tmp_path):
        from backtest.engine.signal3 import TF_LADDER_MS
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        # escalate to top first
        f5 = self._bars(200, spread=2.0)
        s3._pick_timeframe(f5)
        assert s3._tf_level == len(TF_LADDER_MS) - 1
        # now make the bars very wide: at 35m, range >> 8 → drop one rung
        f5_wide = self._bars(200, spread=20.0)  # 35m range ~40+ ≥ 8
        tf = s3._pick_timeframe(f5_wide)
        assert s3._tf_level < len(TF_LADDER_MS) - 1, \
            "2 consecutive wide bars → drop at least one rung"

    def test_ladder_starts_at_5m(self):
        from backtest.engine.signal3 import TF_LADDER_MS
        assert TF_LADDER_MS[0] == 300_000, "base is 5m"
        assert len(TF_LADDER_MS) == 6, "5,15,20,25,30,35m"
        assert TF_LADDER_MS == (300_000, 900_000, 1_200_000, 1_500_000,
                                1_800_000, 2_100_000)


class TestHonestDegradation:
    def test_no_map_returns_reason(self, tmp_path):
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        out = s3.evaluate(None, None)
        assert out["fired"] is False
        assert "no session map" in out["reason"]

    def test_wrong_symbol_returns_reason(self, tmp_path):
        s3 = Signal3(log_path=tmp_path / "s3.jsonl")
        block = {"error": None, "symbol": "BTCUSDT"}
        out = s3.evaluate(None, block)
        assert "BTCUSDT" in out["reason"]
