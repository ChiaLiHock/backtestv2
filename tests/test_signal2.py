"""Signal 2: the mined pattern, and the separation that keeps it harmless.

Two different things are pinned here, and the second matters more than the first.

* **The thresholds are a regression lock.** They are not tunable parameters --
  they are the exact values the pattern was fitted at. Changing one silently
  makes it a different pattern with none of the reported statistics, so the
  numbers are asserted rather than left as defaults nobody checks.

* **It must not be able to touch the measured rule.** Signal 2 was chosen from
  555,000+ tested conditions on ~332 independent trades and scored 47.8% on an
  out-of-sample period against a 48.9% baseline. It is on the page because the
  owner asked for it knowing that. The one thing that must stay true is that it
  cannot change what the validated system decides, cannot write to its log, and
  cannot be mistaken for it by anything reading the output.
"""

from __future__ import annotations

import json

import pytest

from backtest.data.db import CandleRepository, Database
from backtest.engine.signal2 import Signal2, Signal2Config, read_log


@pytest.fixture()
def sig(tmp_path):
    return Signal2(log_path=tmp_path / "signals2.jsonl")


class TestConfigIsFrozenEvidence:
    """The thresholds ARE the pattern. They are not knobs."""

    def test_exact_fitted_thresholds(self):
        c = Signal2Config()
        assert c.minus_di_min == 34.7074
        assert c.ut_distance_atr_max == 1.1336
        assert c.adx_min == 31.5998

    def test_is_short_only(self):
        assert Signal2Config().side == "short"

    def test_declares_itself_unmeasured(self):
        """The whole point of the flag: a reader must be able to tell this
        apart from the rule that was actually validated."""
        assert Signal2Config().measured is False

    def test_bracket_is_symmetric_25_and_the_right_way_round(self):
        c = Signal2Config()
        sl, tp = c.bracket(4600.0)
        assert sl == 4625.0, "a short's stop is ABOVE entry"
        assert tp == 4575.0, "a short's target is BELOW entry"

    def test_config_cannot_be_mutated_in_place(self):
        with pytest.raises(Exception):
            Signal2Config().minus_di_min = 10.0


class TestEvaluation:
    def _db(self):
        with Database() as db:
            n = len(CandleRepository(db).load_tail("XAUUSDT", "15m", 100))
        if n < 100:
            pytest.skip("needs XAUUSDT 15m synced")

    def test_returns_a_complete_row_without_raising(self, sig):
        self._db()
        with Database() as db:
            out = sig.evaluate(db)
        for k in ("name", "symbol", "side", "pattern", "bracket", "measured",
                  "fired", "legs", "blocking", "reason", "bar_open_ms"):
            assert k in out, k
        assert out["name"] == "signal2"
        assert out["measured"] is False

    def test_three_legs_and_fired_is_exactly_all_of_them(self, sig):
        self._db()
        with Database() as db:
            out = sig.evaluate(db)
        assert len(out["legs"]) == 3
        assert out["fired"] == all(out["legs"].values())
        assert out["blocking"] == [k for k, v in out["legs"].items() if not v]

    def test_a_non_firing_bar_carries_no_tradeable_levels(self, sig):
        """A blocked evaluation must not hand anything an entry price."""
        self._db()
        with Database() as db:
            out = sig.evaluate(db)
        if not out["fired"]:
            assert out["entry_ref"] is None
            assert out["sl"] is None and out["tp"] is None

    def test_a_bad_database_is_reported_not_raised(self, sig, tmp_path):
        """A cycle must survive this module failing completely."""
        with Database(tmp_path / "empty.db") as db:
            out = sig.evaluate(db)
        assert out["fired"] is False
        assert "reason" in out


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
            json.dumps({"symbol": "XAUUSDT", "bar_open_ms": bar, "fired": True}) + "\n",
            encoding="utf-8")
        fresh = Signal2(log_path=sig.log_path)
        fresh.seed_from_log()
        fresh._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": bar, "broker_symbol": "GOLD",
            "side": "short", "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0, "tp": 0.5,
            "lots": 0.01, "time_stop_ms": 1, "bracket": "b", "measured": False,
            "pattern": "p", "ts_utc": "t",
        })
        assert fresh.events() == [], "re-alerted on a bar already in the log"

    def test_event_ids_cannot_collide_with_the_measured_rule(self, sig):
        """Both event lists are concatenated for the page, which dedupes by id.
        A shared id would make one signal suppress the other."""
        sig._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": 123, "broker_symbol": "GOLD",
            "side": "short", "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0, "tp": 0.5,
            "lots": 0.01, "time_stop_ms": 1, "bracket": "b", "measured": False,
            "pattern": "p", "ts_utc": "t",
        })
        ev = sig.events()[0]
        assert ev["id"].startswith("s2:"), "must be namespaced away from the rule"
        assert ev["id"] != "XAUUSDT@123", "this is the measured rule's id format"
        assert ev["name"] == "signal2"

    def test_one_event_per_bar(self, sig):
        row = {
            "symbol": "XAUUSDT", "bar_open_ms": 555, "broker_symbol": "GOLD",
            "side": "short", "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0, "tp": 0.5,
            "lots": 0.01, "time_stop_ms": 1, "bracket": "b", "measured": False,
            "pattern": "p", "ts_utc": "t",
        }
        sig._record_event(row)
        sig._record_event(row)
        assert len(sig.events()) == 1


class TestSeparationFromTheMeasuredRule:
    def test_signal2_does_not_import_the_rule(self):
        """Sharing a module would eventually mean sharing state.

        Parsed from the AST rather than grepped: the docstring discusses
        `live_engine` by name on purpose, and a substring check would fail on
        the explanation instead of on an actual dependency.
        """
        import ast
        import pathlib

        import backtest.engine.signal2 as s2

        tree = ast.parse(pathlib.Path(s2.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any("live_engine" in m or "live_signal" in m for m in imported), \
            f"signal2 must not depend on the measured rule; imports {imported}"

    def test_the_two_write_different_files(self, tmp_path):
        from backtest.engine.live_engine import LiveEngine

        eng = LiveEngine(log_path=tmp_path / "signals.jsonl")
        s2 = Signal2(log_path=tmp_path / "signals2.jsonl")
        assert eng.signal.log_path != s2.log_path

    def test_watcher_keeps_them_in_separate_attributes(self, tmp_path):
        """`rule` and `rule2` must stay distinct keys all the way to the page."""
        from backtest.watch import Watcher

        cfg = __import__("pathlib").Path(
            r"C:\inetpub\Claude\ITSupport\backtest\configs\ut_1h_long_v1.yaml")
        if not cfg.exists():
            pytest.skip("config not present")
        w = Watcher(cfg, symbols=["XAUUSDT"], live_mode=True,
                    report_dir=tmp_path, port=8791)
        assert w.engine is not None and w.signal2 is not None
        assert w.engine.signal.log_path != w.signal2.log_path
        assert w._rule == {} and w._rule2 == {}
