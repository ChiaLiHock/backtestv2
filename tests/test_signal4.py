"""Signal 4: a config-driven pattern that is refitted every week.

What is different from signals 2 and 3, and therefore what needs pinning:

* **The thresholds are data.** They arrive from `configs/signal4.json`, so the
  things worth asserting are not the numbers but the CONTRACT: a missing or
  broken config must go quiet rather than fire, an unknown feature name must
  fire nothing rather than half a rule, and a rewritten file must be picked up
  without a restart -- otherwise a weekly refit silently would not apply.
* **The bracket is asymmetric.** TP 15 against SL 25 puts break-even at 63.5%.
  Getting the legs the wrong way round would look plausible on a chart and be
  the exact opposite trade.
* **Weekends are excluded by ENTRY, not by signal bar.** A Friday 23:45 signal
  opens on Saturday and must be dropped; a Sunday 23:45 signal opens Monday and
  must be kept. Classifying by the signal bar reverses both.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine.signal4 import (BREAKEVEN, SL_USD, TP_USD, Signal4,
                                     Signal4Config, bracket, build_features,
                                     entry_is_weekday, fire_mask, read_log)

STEP = INTERVAL_MS["15m"]


@pytest.fixture()
def sig(tmp_path):
    return Signal4(log_path=tmp_path / "signals4.jsonl",
                   config_path=tmp_path / "signal4.json")


def write_cfg(path, conditions, side="long"):
    path.write_text(json.dumps({
        "symbol": "XAUUSDT", "broker_symbol": "GOLD", "anchor": "15m",
        "side": side, "conditions": conditions, "lots": 0.01,
        "fitted": {"win_rate": 0.9, "trades": 17, "conditions_tested": 5612},
    }), encoding="utf-8")


class TestAsymmetricBracket:
    def test_breakeven_is_63_5_not_51(self):
        """The number that makes this win rate incomparable to the others'.

        A win nets 14.605 and a loss nets -25.395, so break-even is 25.395/40.
        """
        assert TP_USD == 15.0 and SL_USD == 25.0
        assert round(BREAKEVEN, 4) == 0.6349

    def test_long_legs_are_the_right_way_round(self):
        sl, tp = bracket(4600.0, "long")
        assert tp == 4615.0, "a long target is 15 ABOVE"
        assert sl == 4575.0, "a long stop is 25 BELOW"

    def test_short_legs_are_the_right_way_round(self):
        sl, tp = bracket(4600.0, "short")
        assert tp == 4585.0, "a short target is 15 BELOW"
        assert sl == 4625.0, "a short stop is 25 ABOVE"

    def test_stop_is_wider_than_target_on_both_sides(self):
        for side in ("long", "short"):
            sl, tp = bracket(4600.0, side)
            assert abs(sl - 4600.0) > abs(tp - 4600.0)


class TestWeekendExclusion:
    def _bar(self, iso: str) -> np.ndarray:
        ms = int(pd.Timestamp(iso, tz="Etc/GMT-8").timestamp() * 1000)
        return np.array([ms], dtype="int64")

    def test_friday_2345_dropped_because_it_enters_saturday(self):
        assert not entry_is_weekday(self._bar("2026-08-21 23:45"), STEP)[0]

    def test_sunday_2345_kept_because_it_enters_monday(self):
        assert entry_is_weekday(self._bar("2026-08-23 23:45"), STEP)[0]

    def test_plain_saturday_dropped(self):
        assert not entry_is_weekday(self._bar("2026-08-22 12:00"), STEP)[0]

    def test_plain_wednesday_kept(self):
        assert entry_is_weekday(self._bar("2026-08-26 12:00"), STEP)[0]


class TestFireMaskFailsClosed:
    def _frame(self):
        return pd.DataFrame({"a": [1.0, 5.0, 9.0], "b": [0.0, 1.0, 2.0]})

    def test_and_of_conditions(self):
        m = fire_mask(self._frame(), [{"feature": "a", "op": ">", "value": 2},
                                      {"feature": "b", "op": "<", "value": 2}])
        assert list(m) == [False, True, False]

    def test_unknown_feature_fires_nothing(self):
        """Half a rule is worse than no rule."""
        m = fire_mask(self._frame(), [{"feature": "a", "op": ">", "value": 2},
                                      {"feature": "nope", "op": ">", "value": 0}])
        assert not m.any()

    def test_unknown_operator_fires_nothing(self):
        assert not fire_mask(self._frame(),
                             [{"feature": "a", "op": "~=", "value": 2}]).any()

    def test_no_conditions_fires_nothing(self):
        assert not fire_mask(self._frame(), []).any()

    def test_nan_never_fires(self):
        f = pd.DataFrame({"a": [np.nan, 5.0]})
        got = fire_mask(f, [{"feature": "a", "op": ">", "value": 0}])
        assert list(got) == [False, True]


class TestConfigContract:
    def test_missing_config_is_quiet_and_says_how_to_fix_it(self, sig):
        with Database() as db:
            out = sig.evaluate(db)
        assert out["fired"] is False
        assert "mine_signal4" in out["reason"]

    def test_unreadable_config_does_not_raise(self, tmp_path):
        p = tmp_path / "signal4.json"
        p.write_text("{ not json", encoding="utf-8")
        s = Signal4(log_path=tmp_path / "l.jsonl", config_path=p)
        assert s.cfg is None
        with Database() as db:
            assert s.evaluate(db)["fired"] is False

    def test_reload_picks_up_a_refit_without_restart(self, tmp_path):
        """The weekly refit is the point; needing a restart means it silently
        would not apply."""
        import os
        import time

        p = tmp_path / "signal4.json"
        write_cfg(p, [{"feature": "rsi_14", "op": "<", "value": 30}])
        s = Signal4(log_path=tmp_path / "l.jsonl", config_path=p)
        assert s.cfg.conditions[0]["value"] == 30

        write_cfg(p, [{"feature": "rsi_14", "op": "<", "value": 40}], side="short")
        future = time.time() + 5
        os.utime(p, (future, future))
        assert s.reload_if_changed() is True
        assert s.cfg.conditions[0]["value"] == 40
        assert s.cfg.side == "short"

    def test_label_renders_every_condition(self, tmp_path):
        p = tmp_path / "signal4.json"
        write_cfg(p, [{"feature": "a", "op": ">", "value": 1.5},
                      {"feature": "b", "op": "<", "value": 2}])
        assert Signal4Config.load(p).label == "a > 1.5 AND b < 2"

    def test_provenance_survives_the_round_trip(self, tmp_path):
        """The page prints sample size and search size beside the win rate; if
        `fitted` were dropped the caption would outlive the numbers."""
        p = tmp_path / "signal4.json"
        write_cfg(p, [{"feature": "a", "op": ">", "value": 1}])
        c = Signal4Config.load(p)
        assert c.fitted["conditions_tested"] == 5612
        assert c.fitted["win_rate"] == 0.9


class TestFeaturesAreShared:
    def test_miner_imports_the_evaluator_rather_than_copying_it(self):
        """One definition of every feature name, or a refit can mean something
        different at runtime than it did during the search."""
        import ast
        import pathlib

        import backtest.tools.mine_signal4 as m

        tree = ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    names.add(f"{node.module}.{a.name}")
        for want in ("build_features", "bracket", "entry_is_weekday"):
            assert any(n.endswith(f"signal4.{want}") for n in names), want

    def test_build_features_carries_price_and_named_features(self):
        with Database() as db:
            if len(CandleRepository(db).load_tail("XAUUSDT", "15m", 300)) < 300:
                pytest.skip("needs XAUUSDT 15m synced")
            f = build_features(db, "XAUUSDT", "15m", tail=400)
        for c in ("open", "high", "low", "close", "volume", "rsi_14", "atr_14",
                  "body_atr", "dist_ema14_atr", "hour_myt", "dow_myt"):
            assert c in f.columns, c


class TestSeparation:
    def test_event_ids_cannot_collide_with_the_other_channels(self, sig):
        sig._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": 7, "broker_symbol": "GOLD",
            "side": "long", "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0, "tp": 3.0,
            "lots": 0.01, "time_stop_ms": 1, "bracket": "b", "pattern": "p",
            "ts_utc": "t",
        })
        ev = sig.events()[0]
        assert ev["id"].startswith("s4:")
        assert not ev["id"].startswith(("s2:", "s3:"))
        assert ev["id"] != "XAUUSDT@7"

    def test_signal4_does_not_import_the_measured_rule(self):
        import ast
        import pathlib

        import backtest.engine.signal4 as s4

        tree = ast.parse(pathlib.Path(s4.__file__).read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
        assert not any("live_engine" in m or "live_signal" in m for m in mods)

    def test_watcher_keeps_four_separate_logs(self, tmp_path):
        from pathlib import Path

        from backtest.watch import Watcher

        cfg = Path(__file__).resolve().parents[1] / "configs" / "ut_1h_long_v1.yaml"
        if not cfg.exists():
            pytest.skip("config not present")
        w = Watcher(cfg, symbols=["XAUUSDT"], live_mode=True,
                    report_dir=tmp_path, port=8792)
        paths = {w.engine.signal.log_path, w.signal2.log_path,
                 w.signal3.log_path, w.signal4.log_path}
        assert len(paths) == 4, "every channel needs its own audit trail"


class TestLog:
    def test_dedupes_by_bar(self, sig):
        row = {"symbol": "XAUUSDT", "bar_open_ms": 11, "fired": True}
        assert sig.write(row) is True
        assert sig.write(row) is False
        assert len(read_log(sig.log_path)) == 1

    def test_seed_stops_a_restart_re_alerting(self, sig, tmp_path):
        sig.log_path.write_text(
            json.dumps({"symbol": "XAUUSDT", "bar_open_ms": 99, "fired": True}) + "\n",
            encoding="utf-8")
        fresh = Signal4(log_path=sig.log_path, config_path=tmp_path / "none.json")
        fresh.seed_from_log()
        fresh._record_event({
            "symbol": "XAUUSDT", "bar_open_ms": 99, "broker_symbol": "GOLD",
            "side": "long", "bar_myt": "x", "entry_ref": 1.0, "sl": 2.0, "tp": 3.0,
            "lots": 0.01, "time_stop_ms": 1, "bracket": "b", "pattern": "p",
            "ts_utc": "t",
        })
        assert fresh.events() == []
