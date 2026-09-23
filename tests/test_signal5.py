"""Signal 5: FAST-C, and the concurrency cap that makes it the measured signal.

Signal 5 shipped without a test file, and that is exactly where the bug lived.
`cap_concurrency` was applied in `analysis/report.py`, so every number ever
reported about this pattern -- 16.6 trades/day at 55.5% -- described a stream
capped at 3 concurrent positions. The live path applied no cap at all.

Measured on the live log for 2026-08-27: **33 fires that were only 7 distinct
episodes**, in consecutive-bar runs of 4, 7, 6, 6, 6, 3 and 1. The condition
stays true for 15-35 minutes and fires on every 5m bar in the run. Uncapped
that is ~53 events a day for the same handful of trades, against the 16.6 the
signal was validated at -- a different population wearing the same name, and
seven phone alerts for one position.

So the cap is not a notification-volume preference. It is part of the signal's
definition (`max_concurrent: 3`, and the bracket label says so out loud), and
these tests pin it in the live path where it was missing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine.signal5 import Signal5, Signal5Config, cap_concurrency

STEP = 5 * 60 * 1000
T0 = 1_787_800_000_000


@pytest.fixture()
def sig(tmp_path):
    return Signal5(log_path=tmp_path / "signals5.jsonl")


def _bars(n: int, high: float, low: float, start: int = T0) -> pd.DataFrame:
    """A flat 5m frame that touches neither barrier unless told to."""
    return pd.DataFrame({
        "open_time": [start + i * STEP for i in range(n)],
        "high": [high] * n,
        "low": [low] * n,
    })


def _fire(sig: Signal5, bar_ms: int, entry: float = 4600.0) -> bool:
    sl, tp = sig.cfg.bracket(entry)
    return sig._record_event({
        "symbol": "XAUUSDT", "broker_symbol": "GOLD", "side": "short",
        "bar_open_ms": bar_ms, "bar_myt": "x", "ts_utc": "2026-08-27T00:00:00+00:00",
        "entry_ref": entry, "sl": sl, "tp": tp, "lots": 0.01,
        "time_stop_ms": bar_ms + sig.cfg.time_stop_bars * STEP,
        "bracket": sig.cfg.bracket_label, "pattern": sig.cfg.label,
    })


class TestConfigIsFrozenEvidence:
    """The thresholds ARE the pattern. They are not knobs."""

    def test_exact_fitted_thresholds(self):
        c = Signal5Config()
        assert c.atr_min == 4.4262
        assert c.macd_hist_30m_min == -0.124451
        assert c.adx_4h_min == 22.185962
        assert c.tp_usd == 10.0 and c.sl_usd == 10.0

    def test_is_short_only(self):
        assert Signal5Config().side == "short"

    def test_declares_itself_unmeasured(self):
        assert Signal5Config().measured is False

    def test_bracket_is_symmetric_10_and_the_right_way_round(self):
        sl, tp = Signal5Config().bracket(4600.0)
        assert sl == 4610.0, "a short's stop is ABOVE entry"
        assert tp == 4590.0, "a short's target is BELOW entry"

    def test_three_slots_is_part_of_the_definition(self):
        """At k=1 the frequency collapses and the signal loses its only point."""
        assert Signal5Config().max_concurrent == 3

    def test_config_cannot_be_mutated_in_place(self):
        with pytest.raises(Exception):
            Signal5Config().atr_min = 0.0


class TestTheLiveCapMatchesTheMeasurement:
    """The regression that the missing cap would have caught."""

    def test_a_seven_bar_run_yields_three_events_not_seven(self, sig):
        """The exact shape observed live on 2026-08-27 at 09:05 MYT."""
        taken = [_fire(sig, T0 + i * STEP) for i in range(7)]
        assert sum(taken) == 3, "the run must be capped at max_concurrent"
        assert taken[:3] == [True, True, True], "the first three are the trades"
        assert not any(taken[3:]), "the rest are the same position, not new ones"
        assert len(sig.events()) == 3

    def test_every_observed_run_length_is_capped(self, sig):
        """Runs of 4, 7, 6, 6, 6, 3, 1 -> 3, 3, 3, 3, 3, 3, 1, never the raw 33."""
        total = 0
        for run in (4, 7, 6, 6, 6, 3, 1):
            s = Signal5(log_path=sig.log_path)
            total += sum(_fire(s, T0 + i * STEP) for i in range(run))
        assert total == 19, "uncapped this is 33 -- the number that was never measured"

    def test_a_full_book_blocks_a_genuinely_new_bar(self, sig):
        for i in range(3):
            assert _fire(sig, T0 + i * STEP)
        assert not _fire(sig, T0 + 50 * STEP), \
            "a later, unrelated signal is still refused while the book is full"

    def test_the_gate_is_the_config_not_a_literal(self, tmp_path):
        one = Signal5(log_path=tmp_path / "s.jsonl",
                      cfg=Signal5Config(max_concurrent=1))
        assert sum(_fire(one, T0 + i * STEP) for i in range(5)) == 1

    def test_already_emitted_bar_does_not_consume_a_slot(self, sig):
        """Dedup must be checked before the cap, or a replayed bar burns a slot."""
        assert _fire(sig, T0)
        assert not _fire(sig, T0), "same bar twice is not two trades"
        assert len(sig._open) == 1
        assert _fire(sig, T0 + STEP) and _fire(sig, T0 + 2 * STEP)


class TestSlotsAreReleased:
    def test_target_touched_frees_the_slot(self, sig):
        _fire(sig, T0, entry=4600.0)                    # tp 4590, sl 4610
        sig._release_slots(_bars(4, high=4605.0, low=4589.0), T0 + 4 * STEP)
        assert sig._open == []

    def test_stop_touched_frees_the_slot(self, sig):
        _fire(sig, T0, entry=4600.0)
        sig._release_slots(_bars(4, high=4611.0, low=4595.0), T0 + 4 * STEP)
        assert sig._open == []

    def test_price_between_the_barriers_keeps_it_open(self, sig):
        _fire(sig, T0, entry=4600.0)
        sig._release_slots(_bars(4, high=4609.9, low=4590.1), T0 + 4 * STEP)
        assert len(sig._open) == 1

    def test_the_signal_bar_itself_cannot_close_the_trade(self, sig):
        """Entry is the NEXT bar's open, so the signal bar's own range is not a fill."""
        _fire(sig, T0, entry=4600.0)
        one = _bars(1, high=4620.0, low=4580.0)         # spans both barriers
        sig._release_slots(one, T0 + STEP)
        assert len(sig._open) == 1, "only bars AFTER the signal bar can resolve it"

    def test_time_stop_frees_the_slot(self, sig):
        _fire(sig, T0, entry=4600.0)
        late = T0 + (Signal5Config().time_stop_bars + 1) * STEP
        sig._release_slots(_bars(4, high=4609.0, low=4591.0), late)
        assert sig._open == []

    def test_a_freed_slot_is_reusable(self, sig):
        for i in range(3):
            _fire(sig, T0 + i * STEP)
        assert not _fire(sig, T0 + 10 * STEP), "full"
        sig._release_slots(_bars(12, high=4605.0, low=4589.0), T0 + 12 * STEP)
        assert sig._open == []
        assert _fire(sig, T0 + 20 * STEP), "capacity returns once positions close"

    def test_release_is_safe_with_no_open_positions(self, sig):
        sig._release_slots(_bars(4, high=4605.0, low=4589.0), T0)
        assert sig._open == []

    def test_only_the_resolved_slot_is_freed(self, sig):
        """A move that resolves one short need not resolve another.

        Both are shorts, so a fall hits the HIGHER entry's target first -- the
        surviving slot has to be the one whose barriers straddle the range, not
        merely a different one.
        """
        _fire(sig, T0, entry=4600.0)                    # tp 4590, sl 4610
        _fire(sig, T0 + STEP, entry=4597.0)             # tp 4587, sl 4607
        sig._release_slots(_bars(6, high=4605.0, low=4589.0), T0 + 6 * STEP)
        assert len(sig._open) == 1
        assert sig._open[0]["tp"] == 4587.0, "the straddling position must survive"


class TestLiveAndMeasuredAgree:
    def test_the_live_gate_reproduces_cap_concurrency(self):
        """Same k, same chronology, same answer -- one definition, two call sites."""
        starts = np.array([T0 + i * STEP for i in range(7)], dtype="int64")
        exits = np.full(7, float(T0 + 100 * STEP))      # nothing closes in the run
        assert int(cap_concurrency(starts, exits, 3).sum()) == 3

    def test_the_report_still_caps_at_the_config_value(self):
        """The report must read k from the config, never hardcode a 3.

        If it ever hardcodes, changing `max_concurrent` moves the live gate
        without moving the measurement, and the two silently disagree again --
        which is the whole failure this file exists to prevent.
        """
        import pathlib

        import backtest.analysis.report as rep

        src = pathlib.Path(rep.__file__).read_text(encoding="utf-8")
        assert "cap_concurrency(starts, exits, s5.max_concurrent)" in src, \
            "report.py must cap using the config's k, not a literal"


class TestSeparationFromTheMeasuredRule:
    def test_signal5_does_not_import_the_rule(self):
        import ast
        import pathlib

        import backtest.engine.signal5 as s5

        tree = ast.parse(pathlib.Path(s5.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any("live_engine" in m or "live_signal" in m for m in imported), \
            f"signal5 must not depend on the measured rule; imports {imported}"

    def test_the_two_write_different_files(self, tmp_path):
        from backtest.engine.live_engine import LiveEngine

        eng = LiveEngine(log_path=tmp_path / "signals.jsonl")
        s5 = Signal5(log_path=tmp_path / "signals5.jsonl")
        assert eng.signal.log_path != s5.log_path

    def test_events_carry_a_distinct_id_prefix(self, sig):
        _fire(sig, T0)
        assert sig.events()[0]["id"].startswith("s5:")
        assert sig.events()[0]["name"] == "signal5"

    def test_a_restart_does_not_replay_old_events(self, tmp_path):
        """`_events` starts empty so yesterday's signals never reach a phone."""
        s5 = Signal5(log_path=tmp_path / "signals5.jsonl")
        assert s5.events() == []
        assert s5._open == [], "and it opens with an empty book, not a stale one"
