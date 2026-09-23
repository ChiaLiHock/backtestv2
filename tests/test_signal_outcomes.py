"""Resolving an emitted signal into a real outcome.

A signal log without outcomes is a list of opinions. These pin the four rules
that decide whether the resulting win rate means anything:

* the fill is the **next bar's open**, never the signal bar's close;
* a bar that spans both barriers is a **loss**, not a win;
* a position whose 24 hours have not elapsed is **open**, not a timeout — the
  difference is the entire recent record;
* the win rate is computed over **resolved trades only**, so it cannot drift
  toward 50% purely because you looked sooner.
"""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine import signal_outcomes as SO
from backtest.engine.live_signal import RuleConfig

SYMBOL = "XAUUSDT"
STEP = INTERVAL_MS["15m"]


def _skip_without():
    with Database() as db:
        d = CandleRepository(db).load_tail(SYMBOL, "15m", 500)
    if len(d) < 500:
        pytest.skip(f"needs {SYMBOL} 15m synced")


def _signal(bar_ms, symbol=SYMBOL, would_enter=True, **extra):
    return {"schema": 2, "symbol": symbol, "side": "long",
            "bar_open_ms": int(bar_ms), "fired": True,
            "would_enter": would_enter, "entry_ref": 4600.0,
            "sl": 4575.0, "tp": 4625.0, **extra}


# ---------------------------------------------------------------------------
# summarise() — pure, so it can be pinned exactly
# ---------------------------------------------------------------------------


def _t(net, resolved=True, vetoed=False, reason="tp"):
    return {"net": net, "gross": net, "fees": 0.0, "resolved": resolved,
            "vetoed": vetoed, "reason": reason}


class TestSummarise:
    def test_open_positions_are_not_losses(self):
        """Three wins and two still running is 100%, not 60%.

        Counting a running position as a loss makes the win rate a function of
        when you looked, which is the one thing a win rate must not be.
        """
        s = SO.summarise([_t(24.6), _t(24.6), _t(24.6),
                          _t(0.0, resolved=False), _t(0.0, resolved=False)])
        assert s["win rate"].startswith("100.0% (3/3)")
        assert s["still open"] == 2

    def test_nothing_resolved_reports_no_win_rate_at_all(self):
        s = SO.summarise([_t(0.0, resolved=False), _t(0.0, resolved=False)])
        assert "win rate" not in s
        assert s["resolved"] == 0 and s["still open"] == 2

    def test_vetoed_signals_stay_out_of_the_headline(self):
        """A vetoed signal never happened; its counterfactual is not P/L."""
        s = SO.summarise([_t(24.6), _t(-25.4),
                          _t(999.0, vetoed=True), _t(999.0, vetoed=True)])
        assert s["win rate"].startswith("50.0% (1/2)")
        assert "+999" not in s["net P/L"]
        assert s["vetoed by gate"] == 2

    def test_break_even_is_stated_and_is_not_fifty(self):
        """A symmetric bracket does NOT break even at 50%; cost is the margin."""
        s = SO.summarise([_t(24.6), _t(-25.4)])
        assert "50.8%" in s["break-even"]

    def test_exit_reasons_are_counted(self):
        s = SO.summarise([_t(24.6, reason="tp"), _t(-25.4, reason="sl"),
                          _t(-3.0, reason="timeout")])
        assert "tp=1" in s["exits"] and "sl=1" in s["exits"]
        assert "timeout=1" in s["exits"]

    def test_confidence_interval_is_reported_with_the_rate(self):
        s = SO.summarise([_t(1.0)] * 6 + [_t(-1.0)] * 4)
        assert s["win rate"].startswith("60.0%")
        lo, hi = (float(x) for x in s["95% CI"].replace("%", "").split("–"))
        assert lo < 60.0 < hi, "the interval must bracket the point estimate"
        assert hi - lo > 20, "at n=10 the interval has to be wide, and look it"


class TestWilson:
    def test_matches_the_harness(self):
        """Same interval the measurement reports, so the two are comparable."""
        from backtest.tools.validate_rule import _wilson as harness
        for wins, n in ((22, 33), (0, 5), (5, 5), (250, 588)):
            assert SO._wilson(wins, n) == pytest.approx(harness(wins, n), abs=1e-9)

    def test_zero_sample_does_not_divide_by_zero(self):
        assert SO._wilson(0, 0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# resolve() — against real stored price
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def resolved():
    _skip_without()
    with Database() as db:
        d = CandleRepository(db).load_tail(SYMBOL, "15m", 400)
        bars = [int(t) for t in d["open_time"].to_numpy()]
        # Every 7th bar, leaving the last few deliberately unresolved.
        sigs = [_signal(b) for b in bars[::7]]
        return SO.resolve(db, sigs, SYMBOL, RuleConfig()), bars


class TestResolve:
    def test_the_fill_is_the_next_bars_open(self, resolved):
        """Not the signal bar's close, which nobody could have traded."""
        rows, bars = resolved
        with Database() as db:
            d = CandleRepository(db).load_tail(SYMBOL, "15m", 400)
        t = d["open_time"].to_numpy()
        o = d["open"].to_numpy()
        for r in rows:
            # A signal on the newest bar has no fill bar yet; `_pending` falls
            # back to the reference price so the chart can still place a marker,
            # and there is nothing to compare it against.
            if r["reason"].startswith("waiting"):
                continue
            k = int(np.searchsorted(t, r["signal_bar"], "left"))
            assert r["entry_time"] == int(t[k + 1]), "fill bar is signal bar + 1"
            assert r["entry_price"] == pytest.approx(float(o[k + 1]), abs=0.01)

    def test_the_bracket_is_rederived_from_the_fill(self, resolved):
        rows, _ = resolved
        cfg = RuleConfig()
        for r in rows:
            if not r.get("resolved"):
                continue
            sl, tp = cfg.bracket(r["entry_price"], 0.0)
            assert r["sl"] == pytest.approx(round(sl, 2), abs=0.01)
            assert r["tp"] == pytest.approx(round(tp, 2), abs=0.01)

    def test_a_resolved_trade_hit_its_own_barrier(self, resolved):
        rows, _ = resolved
        for r in rows:
            if not r.get("resolved"):
                continue
            if r["reason"] == "tp":
                assert r["exit_price"] == pytest.approx(r["tp"], abs=0.01)
                assert r["net"] > 0
            elif r["reason"] == "sl":
                assert r["exit_price"] == pytest.approx(r["sl"], abs=0.01)
                assert r["net"] < 0

    def test_cost_is_charged_once_and_makes_net_worse(self, resolved):
        rows, _ = resolved
        for r in rows:
            if not r.get("resolved"):
                continue
            assert r["fees"] == pytest.approx(
                r["entry_price"] * SO.COST_BPS / 1e4, abs=0.01)
            assert r["net"] == pytest.approx(r["gross"] - r["fees"], abs=0.01)
            assert r["net"] < r["gross"], "cost can only reduce the result"

    def test_a_recent_trade_can_hit_a_barrier_but_cannot_time_out(self, resolved):
        """The bug this guards: `walk_forward` books a "timeout" when it simply
        runs out of bars, which for a trade opened an hour ago is an invented
        exit at whatever price happened to be last.

        A recent trade CAN still resolve — price may have reached +25 or -25
        within the hour. What it cannot do is time out, because the 24 hours
        have not passed.
        """
        rows, _ = resolved
        now = int(time.time() * 1000)
        horizon = RuleConfig().time_stop_bars * STEP
        checked = 0
        for r in rows:
            if not r["entry_time"] or now >= r["entry_time"] + horizon:
                continue
            checked += 1
            assert r["reason"] != "timeout", (
                f"signal at {r['entry_time']} timed out before its 24h elapsed")
            if not r.get("resolved"):
                assert r["exit_price"] is None and r["net"] == 0.0
            else:
                assert r["reason"] in ("tp", "sl"), (
                    "a trade inside its window may only resolve by touching a "
                    f"barrier, got {r['reason']!r}")
        assert checked, "no recent signals in the fixture to check"

    def test_older_signals_do_resolve(self, resolved):
        rows, _ = resolved
        assert any(r.get("resolved") for r in rows), "nothing resolved at all"

    def test_excursions_bracket_the_outcome(self, resolved):
        """MAE/MFE must contain the actual exit, or the path was mis-sliced."""
        rows, _ = resolved
        for r in rows:
            if not r.get("resolved") or r["reason"] == "timeout":
                continue
            assert r["mae"] >= 0 and r["mfe"] >= 0
            if r["reason"] == "tp":
                assert r["mfe"] >= abs(r["gross"]) - 0.01
            else:
                assert r["mae"] >= abs(r["gross"]) - 0.01

    def test_other_symbols_are_ignored(self, resolved):
        _skip_without()
        with Database() as db:
            d = CandleRepository(db).load_tail(SYMBOL, "15m", 100)
            b = int(d["open_time"].iloc[0])
            rows = SO.resolve(db, [_signal(b, symbol="BTCUSDT")], SYMBOL,
                              RuleConfig())
        assert rows == []

    def test_non_entries_are_ignored(self, resolved):
        _skip_without()
        with Database() as db:
            d = CandleRepository(db).load_tail(SYMBOL, "15m", 100)
            b = int(d["open_time"].iloc[0])
            rows = SO.resolve(db, [_signal(b, would_enter=False)], SYMBOL,
                              RuleConfig())
        assert rows == []

    def test_an_empty_log_is_not_an_error(self):
        _skip_without()
        with Database() as db:
            assert SO.resolve(db, [], SYMBOL, RuleConfig()) == []


class TestTieBreak:
    def test_a_bar_spanning_both_barriers_is_a_loss(self):
        """Straight from `walk_forward`, and the reason it is worth a test.

        Reading a straddling bar as a win is the bug that makes a record look
        profitable and an account not.
        """
        from backtest.tools.validate_rule import walk_forward

        t = np.array([0, 60_000, 120_000], dtype="int64")
        hi = np.array([200.0, 200.0, 200.0])
        lo = np.array([0.0, 0.0, 0.0])
        c = np.array([100.0, 100.0, 100.0])
        reason, px, _ms, amb = walk_forward(
            t, hi, lo, c, 0, 100.0, 125.0, 75.0, True, 10 * 60_000)
        assert reason == "sl" and px == 75.0 and amb is True


class TestFixedBracket:
    def test_twenty_five_either_way_whatever_atr_does(self):
        cfg = RuleConfig()
        for atr in (3.0, 13.5, 50.0):
            sl, tp = cfg.bracket(4670.0, atr)
            assert (tp - 4670.0, 4670.0 - sl) == (25.0, 25.0)

    def test_a_win_nets_less_than_twenty_five(self):
        """+25 gross is +24.60 net at gold near 4,670. The gap is the margin."""
        entry = 4670.0
        cost = entry * SO.COST_BPS / 1e4
        assert 0.35 < cost < 0.45
        assert 24.5 < 25.0 - cost < 24.7
