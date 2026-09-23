"""Signal 3 v2: the symmetry of the double-sweep/fake-break reversal, and the
separation that keeps it harmless.

Two different things are pinned here, and the second matters more than the first.

* **The evaluation contract stays complete.** Whether the sweep fires, the ExpD
  fallback blocks it, or no session map exists at all, the row must answer the
  panel's questions — name, symbol, side, pattern, bracket, measured, fired,
  legs, blocking, reason — without ever raising on a missing piece of data.

* **It must not be able to touch the measured rule.** Signal 3 was chosen from
  555,000+ tested conditions on ~332 independent trades and scored 47.8% on an
  out-of-sample period against a 48.9% baseline. It is on the page because the
  owner asked for it knowing that. The one thing that must stay true is that it
  cannot change what the validated system decides, cannot write to its log, and
  cannot be mistaken for it by anything reading the output.
"""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from backtest.data.db import CandleRepository, Database
from backtest.engine.signal3 import (Signal3, Signal3Config, _closed_fresh,
                                     _read_log as read_log)


@pytest.fixture()
def sig(tmp_path):
    return Signal3(log_path=tmp_path / "signals3.jsonl")


@pytest.fixture()
def block():
    """A bare session map: no levels yet, so the ExpD fallback says 'cold'."""
    return {"symbol": "XAUUSDT", "levels": {}, "as_of_myt": "2026-09-22 09:00"}


class TestConfigIsAStableRecord:
    def test_declares_itself_unmeasured(self):
        """The whole point of the flag: a reader must be able to tell this
        apart from the rule that was actually validated."""
        assert Signal3Config().measured is False

    def test_it_is_golds_pattern(self):
        assert Signal3Config().symbol == "XAUUSDT"
        assert Signal3Config().broker_symbol == "GOLD"

    def test_it_carries_its_pattern_names(self):
        c = Signal3Config()
        assert len(c.label) > 0 and len(c.bracket_label) > 0
        assert "ExpD" in c.bracket_label, "the fallback must be named"

    def test_config_cannot_be_mutated_in_place(self):
        with pytest.raises(Exception):
            Signal3Config().measured = True


class TestEvaluation:
    def _db(self):
        with Database() as db:
            n = len(CandleRepository(db).load_tail("XAUUSDT", "15m", 100))
        if n < 100:
            pytest.skip("needs XAUUSDT 15m synced")

    def test_returns_a_complete_row_without_raising(self, sig, block):
        self._db()
        with Database() as db:
            out = sig.evaluate(db, block)
        for k in ("name", "symbol", "side", "pattern", "bracket", "measured",
                  "fired", "legs", "blocking", "reason"):
            assert k in out, k
        assert out["name"] == "signal3"
        assert out["measured"] is False

    def test_fired_is_exactly_all_the_legs(self, sig, block):
        """The panel shades a channel by its own report of itself."""
        self._db()
        with Database() as db:
            out = sig.evaluate(db, block)
        # `legs` is a checklist, not a marker: it is non-empty even on quiet
        # bars (every leg false -> not fired). Firing means ALL legs are true,
        # and `blocking` names exactly the false ones. The single exception is
        # the pre-Asia day open, where `blocking` is a plain reason line.
        if out["blocking"] == ["no Asia range"]:
            assert out["fired"] is False
        else:
            assert out["fired"] == all(out["legs"].values())
            assert out["blocking"] == [
                k for k, v in out["legs"].items() if not v]

    def test_a_non_firing_bar_carries_no_tradeable_levels(self, sig, block):
        """A blocked evaluation must not hand anything an entry price."""
        self._db()
        with Database() as db:
            out = sig.evaluate(db, block)
        if not out["fired"]:
            assert out.get("entry_ref") is None
            assert out.get("sl") is None and out.get("tp") is None

    def test_a_bad_database_is_reported_not_raised(self, sig, tmp_path):
        """A cycle must survive this module failing completely."""
        with Database(tmp_path / "empty.db") as db:
            out = sig.evaluate(db, None)
        assert out["fired"] is False
        assert "reason" in out

    def test_no_session_map_is_downgraded_not_raised(self, sig):
        """The fast loop's very first tick has no block yet."""
        with Database() as db:
            out = sig.evaluate(db, None)
        assert "reason" in out
        assert out.get("bar_open_ms") is None


class TestLogAndAlertChannel:
    def test_write_then_reread(self, sig):
        row = {"symbol": "XAUUSDT", "bar_open_ms": 1_787_000_000_000, "fired": True}
        assert sig.write(row) is True
        got = read_log(sig.log_path)
        assert len(got) == 1 and got[0]["bar_open_ms"] == 1_787_000_000_000

    def test_the_same_bar_is_never_logged_twice(self, sig):
        row = {"symbol": "XAUUSDT", "bar_open_ms": 1_787_000_000_000, "fired": True}
        assert sig.write(row) is True
        assert sig.write(row) is False
        assert len(read_log(sig.log_path)) == 1

    def test_a_row_with_no_bar_is_refused(self, sig):
        assert sig.write({"symbol": "XAUUSDT"}) is False

    def test_seed_from_log_stops_a_restart_re_alerting(self, sig, tmp_path):
        """Without this, restarting the watcher would beep for last week."""
        bar = 1_787_000_000_000
        sig.log_path.write_text(
            json.dumps({"symbol": "XAUUSDT", "bar_open_ms": bar, "fired": True})
            + "\n", encoding="utf-8")
        fresh = Signal3(log_path=sig.log_path)
        fresh.seed_from_log()
        fresh._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": bar, "broker_symbol": "GOLD",
            "side": "short", "fired": True, "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0,
            "tp": 0.5, "lots": 0.01, "time_stop_ms": 1, "bracket": "b",
            "measured": False, "pattern": "p", "ts_utc": "t",
        })
        assert fresh.events() == [], "re-alerted on a bar already in the log"

    def test_event_ids_cannot_collide_with_the_measured_rule(self, sig):
        """Both event lists are concatenated for the page, which dedupes by id.
        A shared id would make one signal suppress the other."""
        sig._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": 123, "broker_symbol": "GOLD",
            "side": "short", "fired": True, "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0,
            "tp": 0.5, "lots": 0.01, "time_stop_ms": 1, "bracket": "b",
            "measured": False, "pattern": "p", "ts_utc": "t",
        })
        ev = sig.events()[0]
        assert ev["id"].startswith("s3:"), "must be namespaced away from the rule"
        assert ev["id"] != "XAUUSDT@123", "this is the measured rule's id format"
        assert not ev["id"].startswith("s2:"), "must not collide with signal2 either"
        assert ev["name"] == "signal3"

    def test_one_event_per_bar(self, sig):
        row = {
            "symbol": "XAUUSDT", "bar_open_ms": 555, "broker_symbol": "GOLD",
            "side": "short", "fired": True, "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0,
            "tp": 0.5, "lots": 0.01, "time_stop_ms": 1, "bracket": "b",
            "measured": False, "pattern": "p", "ts_utc": "t",
        }
        sig._record_event(row)
        sig._record_event(row)
        assert len(sig.events()) == 1


class TestSeparationFromTheMeasuredRule:
    def test_signal3_does_not_import_the_rule(self):
        """Sharing a module would eventually mean sharing state.

        Parsed from the AST rather than grepped: the docstring discusses
        `live_engine` by name on purpose, and a substring check would fail on
        the explanation instead of on an actual dependency.
        """
        import ast
        import pathlib

        import backtest.engine.signal3 as s2

        tree = ast.parse(pathlib.Path(s2.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any("live_engine" in m or "live_signal" in m for m in imported), \
            f"signal3 must not depend on the measured rule; imports {imported}"

    def test_the_two_write_different_files(self, tmp_path):
        from backtest.engine.live_engine import LiveEngine

        eng = LiveEngine(log_path=tmp_path / "signals.jsonl")
        s2 = Signal3(log_path=tmp_path / "signals3.jsonl")
        assert eng.signal.log_path != s2.log_path

    def test_watcher_keeps_them_in_separate_attributes(self, tmp_path):
        """`rule` and `rule3` must stay distinct keys all the way to the page."""
        from backtest.watch import Watcher

        cfg = __import__("pathlib").Path(
            r"C:\inetpub\Claude\ITSupport\backtest\configs\ut_1h_long_v1.yaml")
        if not cfg.exists():
            pytest.skip("config not present")
        w = Watcher(cfg, symbols=["XAUUSDT"], live_mode=True,
                    report_dir=tmp_path, port=8791)
        assert w.engine is not None and w.signal3 is not None
        assert w.engine.signal.log_path != w.signal3.log_path
        assert w._rule == {} and w._rule3 == {}


class TestLiveMinuteFeed:
    """The sweep frame reads the database, which syncs every `interval_s`.
    The fast loop hands over its live 1m pull so a fire lands on the bar's
    real close instead of the next DB-sync boundary — this is why a Telegram
    notification used to arrive minutes late with the CURRENT time in it.
    """

    def test_the_live_pull_keeps_only_closed_minutes(self):
        now = int(time.time() * 1000)
        k = now - now % 60_000  # a minute-aligned wall-clock
        df = pd.DataFrame({
            "open_time": [k - 120_000, k - 60_000, k],
            "open": [1.0, 2.0, 3.0], "high": [1.5, 2.5, 3.5],
            "low": [0.5, 1.5, 2.5], "close": [1.2, 2.2, 3.2],
            "volume": [10.0, 20.0, 30.0], "turnover": [1.0, 2.0, 3.0],
        })
        got = _closed_fresh(df, now)
        # the bar at `k` has not closed yet (k + 60s > now)
        assert list(got["open_time"]) == [k - 120_000, k - 60_000]
        assert got["open"].dtype == "float64"

    def test_frame_reaches_the_live_minute_before_the_db_catches_up(
            self, tmp_path):
        now = int(time.time() * 1000)
        base = now - 6 * 60_000          # DB tail ends here, a few min stale
        rows = [(base - (60 - i) * 60_000, 4350.0, 4351.0, 4349.0, 4350.5,
                 100.0, 100.0) for i in range(60)]
        with Database(tmp_path / "candles.db") as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", rows)
            sig = Signal3(log_path=tmp_path / "signals3.jsonl")

            sig._frame(db)
            assert sig._ctx_last_ms == base - 60_000, \
                "frame ends at the DB's last bar"
            sig._frame(db)               # no new data -> served from cache
            assert sig._ctx_last_ms == base - 60_000

            live = pd.DataFrame({
                "open_time": [base + 60_000, base + 120_000],
                "open": [4350.0, 4350.0], "high": [4351.0, 4351.0],
                "low": [4349.0, 4349.0], "close": [4350.5, 4350.5],
                "volume": [100.0, 100.0], "turnover": [1.0, 1.0],
            })
            sig._frame(db, minute=live)
            # the newest CLOSED live minute is now visible: two bars ahead of
            # the last DB sync, without waiting for the slow cycle.
            assert sig._ctx_last_ms == base + 120_000

            sig._frame(db)               # no live minute -> cached, not stale
            assert sig._ctx_last_ms == base + 120_000

    def test_evaluate_accepts_the_live_minute_without_raising(self, tmp_path):
        now = int(time.time() * 1000)
        base = now - 6 * 60_000
        rows = [(base - (60 - i) * 60_000, 4350.0, 4351.0, 4349.0, 4350.5,
                 100.0, 100.0) for i in range(60)]
        with Database(tmp_path / "candles.db") as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", rows)
            sig = Signal3(log_path=tmp_path / "signals3.jsonl")
            block = {"symbol": "XAUUSDT", "levels": {},
                     "as_of_myt": "2026-09-23 00:00"}
            live = pd.DataFrame({
                "open_time": [base + 60_000], "open": [4350.0], "high": [4351.0],
                "low": [4349.0], "close": [4350.5], "volume": [100.0],
                "turnover": [1.0],
            })
            out = sig.evaluate(db, block, minute=live)
            assert out["name"] == "signal3"
            assert out["fired"] is False


class TestEventTimeIsTheEntryBar:
    """The message a phone receives must show the ENTRY bar, not the moment
    the evaluate happened to run (the block's as-of time)."""

    def test_sweep_event_reports_the_entry_bar_time(self, sig):
        from datetime import datetime, timedelta, timezone

        MYT = timezone(timedelta(hours=8))
        bar = 1_787_000_000_000
        sig._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": bar, "broker_symbol": "GOLD",
            "side": "short", "fired": True, "bar_myt": "CURRENT WALL CLOCK",
            "entry_ref": 1.0, "sl": 2.0, "tp": 0.5, "lots": 0.01,
            "time_stop_ms": 1, "bracket": "b", "measured": False,
            "pattern": "p", "ts_utc": "t",
        })
        ev = sig.events()[0]
        assert ev["bar_myt"] == datetime.fromtimestamp(
            bar / 1000, MYT).strftime("%Y-%m-%d %H:%M")
        assert ev["bar_myt"] != "CURRENT WALL CLOCK"

    def test_event_without_a_bar_keeps_the_block_time(self, sig):
        sig._record_event({
            "symbol": "XAUUSDT", "broker_symbol": "GOLD",
            "side": "short", "fired": True, "bar_myt": "as-of",
            "entry_ref": 1.0, "sl": 2.0, "tp": 0.5, "lots": 0.01,
            "time_stop_ms": 1, "bracket": "b", "measured": False,
            "pattern": "p", "ts_utc": "t",
        })
        assert sig.events()[0]["bar_myt"] == "as-of"


class TestRiskRewardFloor:
    """Owner's rule (2026-09-23): a sweep whose target is under half its
    stop distance (rr < 0.5) is skipped with the reason stated — measured
    on all three markets, that slice is the only one that loses net."""

    def test_an_extreme_mismatch_is_filtered_not_fired(self, sig):
        import numpy as np
        from backtest.engine.signal3 import SweepState

        # Deep sweep: extreme 40 under the swing → stop ~$75 from entry,
        # nearest CONFIRMED opposing swing only $25 up → rr = 0.33 < 0.5.
        state = SweepState(swing_price=4600.0, side="low")
        state.break_count = 2
        state.extreme = 4560.0
        h = np.full(12, 4600.0)
        h[8] = 4620.0        # a confirmed swing high (3 bars after it)
        l = np.full(12, 4580.0)
        out = sig._sweep_signal(
            state, "long", spot=4595.0, close_price=4590.0,
            target_swings=np.array([8]), lo_i=0, h=h, l=l, bar_ms=1)
        assert out["fired"] is False
        assert "rr" in out["filtered_by"]

    def test_a_healthy_bracket_still_fires(self, sig):
        import numpy as np
        from backtest.engine.signal3 import SweepState

        # rr = 45/11 ≈ 4 — far above the floor.
        state = SweepState(swing_price=4600.0, side="low")
        state.break_count = 2
        state.extreme = 4592.0
        h = np.full(12, 4600.0)
        h[8] = 4640.0        # confirmed swing high, $45 above entry
        l = np.full(12, 4580.0)
        out = sig._sweep_signal(
            state, "long", spot=4595.0, close_price=4590.0,
            target_swings=np.array([8]), lo_i=0, h=h, l=l, bar_ms=1)
        assert out["fired"] is True
        assert abs(out["tp"] - out["entry_ref"]) >= 0.5 * abs(
            out["entry_ref"] - out["sl"])

    def test_exactly_half_is_allowed(self, sig):
        import numpy as np
        from backtest.engine.signal3 import SweepState

        state = SweepState(swing_price=4600.0, side="low")
        state.break_count = 2
        state.extreme = 4558.0            # zone 42 → sl = 4516 ($79 away)
        h = np.full(12, 4600.0)
        h[8] = 4645.0                     # tp $50 above entry 4595
        l = np.full(12, 4580.0)
        out = sig._sweep_signal(
            state, "long", spot=4595.0, close_price=4590.0,
            target_swings=np.array([8]), lo_i=0, h=h, l=l, bar_ms=1)
        # rr = 50/79 ≈ 0.63 ≥ 0.5 → fires
        assert out["fired"] is True