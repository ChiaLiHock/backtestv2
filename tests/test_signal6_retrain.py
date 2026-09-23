"""The retraining pipeline: replication, no-lookahead, labels, leakage.

Model 0 must reproduce the shipped Signal 6 numbers bit-for-bit — that is
the spec's (§24) proof the pipeline is honest before any model is trusted.
These tests pin the machinery on synthetic data; the full-history
replication (105/62.9% on Bybit, 201/47.8% on MT5) is verified by
`tools/retrain_signal6.py` and re-checked in m0_check.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engine.signal6_retrain import (FEATURE_KEYS, LogisticPwin,
                                             _fold, build_samples, matrix,
                                             model0_baseline)
from backtest.tests.test_session_map import (H, MIN, MON_MYT0, SUN, TUE_MYT0,
                                             minute_bars, poke, quiet)


def _break_dataset(prior_sweep=True, exit_poke=None):
    """A quiet Mon-Wed frame with a Tuesday A-grade sequence."""
    df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=5.0)
    df = quiet(df, MON_MYT0, "1500", span=540)
    if prior_sweep:
        df = poke(df, TUE_MYT0, "1530", high=4607.0, close=4605.0)
    df = poke(df, TUE_MYT0, "1600", span=5, high=4607.2, low=4605.6,
              close=4607.0, vol=1000.0)
    df = poke(df, TUE_MYT0, "1605", span=5, high=4607.4, low=4605.6,
              close=4607.5, vol=1000.0)
    df = poke(df, TUE_MYT0, "1610", span=5, open=4608.0, close=4608.0,
              high=4608.5, low=4607.5)
    if exit_poke is not None:
        df = exit_poke(df)
    else:
        df = quiet(df, TUE_MYT0, "1615", span=400)
    return df


def _seed(tmp_path, df):
    from backtest.data.db import CandleRepository, Database
    dbp = tmp_path / "retrain.db"
    with Database(dbp) as db:
        CandleRepository(db).upsert("XAUUSDT", "1m", [
            tuple(r) for r in df[["open_time", "open", "high", "low",
                                  "close", "volume", "turnover"]].to_numpy()])
    return dbp


class TestFold:
    def test_exact_ohlcv_to_5m_and_1h(self):
        df = minute_bars(SUN, 120, base=4600.0)
        for step, n in ((300_000, 24), (3_600_000, 2)):
            f = _fold(df, step)
            assert len(f) == n
            row = f.iloc[0]
            k = step // 60_000
            assert row["high"] == float(df["high"].iloc[:k].max())
            assert row["low"] == float(df["low"].iloc[:k].min())
            assert row["close"] == float(df["close"].iloc[k - 1])
            assert row["volume"] == float(df["volume"].iloc[:k].sum())


class TestSamples:
    def _build(self, tmp_path, df):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, df)
        with Database(dbp) as db:
            return build_samples(db, "XAUUSDT")

    def test_one_break_sample_with_the_baseline_filter(self, tmp_path):
        rows = self._build(tmp_path, _break_dataset())
        assert len(rows) == 1
        s = rows[0]
        assert s["event_type"] == "break_high" and s["side"] == 1
        assert s["prior_sweep"] == 1.0 and s["vol_ok"] == 1.0
        assert s["in_window"] == 1.0 and s["min_range_ok"] == 1.0
        assert model0_baseline(rows) == rows

    def test_sweep_features_carry_depth_and_spacing(self, tmp_path):
        s = self._build(tmp_path, _break_dataset())[0]
        # sweep at 15:30, confirming break bar at 16:05 -> 35 min = 7
        # buckets; the feature anchors on the CONFIRM bar (when the break
        # became a fact), same as the entry timing.
        assert s["bars_sweep_to_break"] == 7.0
        assert s["sweep_depth_R"] > 0 and s["sweep_depth_ATR"] > 0
        # a single-bar wick sweep reclaims within its own bar
        assert s["sweep_reclaim_speed"] == 0.0

    def test_no_prior_sweep_still_samples_but_model0_drops_it(self, tmp_path):
        rows = self._build(tmp_path, _break_dataset(prior_sweep=False))
        assert len(rows) == 1, "the population is ALL breaks (§24)"
        assert model0_baseline(rows) == []

    def test_labels_tp_win_and_research_fields(self, tmp_path):
        df = _break_dataset(exit_poke=lambda d: poke(
            d, TUE_MYT0, "1615", span=5, open=4608.0, close=4617.0,
            high=4618.0, low=4607.5))
        lab = self._build(tmp_path, df)[0]["label"]
        assert lab["resolved"] and lab["win"] and lab["reason"] == "tp"
        assert lab["mfe_R"] > 0 and lab["mae_R"] >= 0
        assert lab["minutes_to_outcome"] > 0

    def test_labels_sl_loss(self, tmp_path):
        df = _break_dataset(exit_poke=lambda d: poke(
            d, TUE_MYT0, "1615", span=5, open=4608.0, close=4600.0,
            high=4608.5, low=4599.0))
        lab = self._build(tmp_path, df)[0]["label"]
        assert lab["resolved"] and not lab["win"] and lab["reason"] == "sl"

    def test_features_do_not_change_when_the_future_extends(self, tmp_path):
        """The spec's §13 contract, pinned: append future bars, rebuild,
        and every as-of feature of the earlier sample is identical."""
        base = self._build(tmp_path, _break_dataset())
        df = _break_dataset()
        grown = poke(df, TUE_MYT0, "2000", span=120, open=4620.0,
                     close=4640.0, high=4645.0, low=4615.0, vol=500.0)
        grown = self._build(tmp_path / "g", grown)
        assert len(grown) >= len(base)
        k = [g for g in grown
             if g["entry_time"] == base[0]["entry_time"]][0]
        for f in FEATURE_KEYS:
            assert k[f] == pytest.approx(base[0][f], nan_ok=True), f


class TestLeakageGuard:
    def test_feature_keys_exclude_every_label_field(self):
        banned = {"win", "net", "realized_R", "mfe_R", "mae_R", "reason",
                  "resolved", "minutes_to_outcome", "label", "p_win"}
        assert not (set(FEATURE_KEYS) & banned)

    def test_matrix_shape_and_finiteness(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        with Database(dbp) as db:
            rows = build_samples(db, "XAUUSDT")
        X, y = matrix(rows)
        assert X.shape == (len(rows), len(FEATURE_KEYS))
        assert np.isfinite(X).all()
        assert set(y) <= {0.0, 1.0}


class TestLegFeatures:
    """The leg-state features: named, finite, and lookahead-free (the
    no-lookahead test above already sweeps them via FEATURE_KEYS)."""

    def test_leg_features_present_and_sane(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        with Database(dbp) as db:
            rows = build_samples(db, "XAUUSDT")
        s = rows[0]
        # invariants: the bracket orders correctly, and a BREAK sample sits
        # at/above the trailing 20-day high (that is what a breakout is —
        # dist_20d_high > 0 is expected here, price beyond the bracket of
        # the last CLOSED 1h bar).
        assert s["dist_20d_high"] <= s["dist_20d_low"]
        assert s["dist_20d_high"] >= 0
        assert np.isfinite(s["ret_20d"]) and np.isfinite(s["slope_200_1h"])


class TestModel2Features:
    """The regime-change and velocity features added for the Q3 spec."""

    def test_new_features_present_and_finite(self, tmp_path):
        from backtest.data.db import Database
        dbp = _seed(tmp_path, _break_dataset())
        with Database(dbp) as db:
            rows = build_samples(db, "XAUUSDT")
        s = rows[0]
        for k in ("adx_slope", "ema_slope_change", "atr_pctile_change",
                  "vol_change_ratio", "break_velocity"):
            assert np.isfinite(s[k]), k
        # a quiet synthetic day: regime roughly flat, velocity positive
        assert s["vol_change_ratio"] > 0
        assert s["break_velocity"] > 0, "price travelled to break, so > 0"

    def test_regime_thresholds_come_from_train_only(self):
        from backtest.tools.retrain_signal6 import pick_regime_thresholds, regime_of
        # 60 trend rows at p 0.55-0.70, 60 range rows at p 0.30-0.45:
        # the picker should RAISE the range bar and keep trend looser.
        rows = []
        probs = []
        for i in range(60):
            r = {"adx_1h": 30.0, "label": {"resolved": True,
                                           "win": i % 2 == 0,
                                           "net": 1.0 if i % 2 == 0 else -1.0}}
            rows.append(r)
            probs.append(0.60)
        for i in range(60):
            r = {"adx_1h": 15.0, "label": {"resolved": True,
                                           "win": False,
                                           "net": -1.0}}
            rows.append(r)
            probs.append(0.35)
        thr = pick_regime_thresholds(rows, probs)
        assert thr["trend"] <= 0.60
        assert thr["range"] == 0.50, \
            "no range threshold clears n>=25 at any level -> default 0.50"
        assert regime_of({"adx_1h": 25.0}) == "trend"

    def test_regime_thresholds_default_without_data(self):
        from backtest.tools.retrain_signal6 import pick_regime_thresholds
        assert pick_regime_thresholds([], []) == {"trend": 0.50,
                                                  "range": 0.50}


class TestLogistic:
    def test_separates_two_clouds_and_predicts_probabilities(self):
        rng = np.random.default_rng(3)
        n = 200
        X = np.vstack([rng.normal(0, 1, (n, 4)), rng.normal(2.5, 1, (n, 4))])
        y = np.concatenate([np.zeros(n), np.ones(n)])
        m = LogisticPwin(l2=1e-3).fit(X, y)
        p = m.predict(X)
        assert p.min() >= 0 and p.max() <= 1
        assert float(np.mean((p[:n] < 0.5))) > 0.9
        assert float(np.mean((p[n:] >= 0.5))) > 0.9
