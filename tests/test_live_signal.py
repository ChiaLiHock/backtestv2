"""The decision function and its log.

The properties worth pinning are not "does it compute the rule" — `validate_rule`
already owns that — but the ones that make the log usable as an audit trail and
the config safe to ship:

* the three `OPERATING_PLAN.md` §8.4 omissions are refused, not merely absent;
* `would_enter` matches the harness bar for bar (this is §D3 in miniature);
* the log is append-only and a restart does not duplicate it.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine.live_signal import LiveSignal, RuleConfig, read_log
from backtest.tools.validate_rule import measure

PANEL = "MT5:GOLD"


def measured_cfg(**over):
    """The config `OPERATING_PLAN.md` §8 measured, built explicitly.

    `RuleConfig()`'s DEFAULT is no longer this. The live default trades Bybit
    gold on a fixed +25/-25 bracket, because that is what the owner asked for and
    what the page shows next to their own fills. The measured system is a
    different thing and these tests are about that one, so they name it rather
    than inherit whatever the default happens to be this month — the whole point
    of the assertions below is that the measured config cannot drift silently.
    """
    from dataclasses import replace

    return replace(RuleConfig(), symbol=PANEL, bracket_mode="atr",
                   tp_atr=5.0, sl_atr=2.5, **over)


def _have(minimum: int = 5000):
    with Database() as db:
        d = CandleRepository(db).load(PANEL, "15m")
    if len(d) < minimum:
        pytest.skip(f"needs {PANEL} 15m synced")
    return d


@pytest.fixture(scope="module")
def evals():
    """One backfill over the last 60 days, shared by the tests below."""
    import time

    _have()
    sig = LiveSignal(measured_cfg())
    since = int((time.time() - 60 * 86400) * 1000)
    with Database() as db:
        return sig, sig.backfill(db, since_ms=since)


# ---------------------------------------------------------------------------
# §8.4 — the three things that silently change the strategy
# ---------------------------------------------------------------------------


def test_measured_config_is_the_shipping_config():
    """§8's bracket must stay reachable and unchanged, whatever the default is."""
    c = measured_cfg()
    assert c.side == "long"
    assert (c.tp_atr, c.sl_atr) == (5.0, 2.5)
    assert c.is_measured_bracket()
    assert c.time_stop_bars == 96
    assert c.use_5m_leg is False
    assert c.hour_window is False          # windows measure worse than all day
    assert c.session == "gold_session"
    assert c.lots == 0.01
    assert c.partial_tp is False and c.trail is False
    c.assert_matches_measured_system()


@pytest.mark.parametrize("bad, why", [
    (dict(time_stop_bars=0), "time_stop_bars"),
    (dict(time_stop_bars=48), "time_stop_bars"),
    (dict(partial_tp=True), "partial"),
    (dict(trail=True), "partial"),
    (dict(use_5m_leg=True), "use_5m_leg"),
])
def test_the_three_omissions_are_refused(bad, why):
    """Configuring them wrong must stop the process, not be silently accepted.

    11% of measured trades exit on the time stop; a partial at 2.5 ATR removes
    the winner that makes TP 5.0 work; leg 5 caps the panel at 5m depth.
    """
    with pytest.raises(SystemExit) as e:
        LiveSignal(replace(RuleConfig(), **bad))
    assert why in str(e.value)


def test_config_hash_moves_when_the_config_does():
    a = LiveSignal(RuleConfig()).config_hash()
    b = LiveSignal(replace(RuleConfig(), tp_atr=3.0)).config_hash()
    assert a != b and len(a) == 12


# ---------------------------------------------------------------------------
# §D1 — every evaluation is logged, firing or not
# ---------------------------------------------------------------------------


def test_non_firing_bars_are_logged_with_a_reason(evals):
    _sig, rows = evals
    assert len(rows) > 500
    quiet = [r for r in rows if not r.fired]
    assert len(quiet) > len(rows) * 0.8, "most bars should not fire"
    assert all(r.reason_if_skipped for r in quiet), "a non-firing bar needs a reason"
    assert all(r.legs for r in rows), "leg values must be recorded on every bar"


def test_every_leg_of_the_rule_is_recorded(evals):
    _sig, rows = evals
    legs = set(rows[0].legs)
    # Eight labels cover nine legs: 8 and 9 are both inside the reclaim signal.
    assert len(legs) == 7, sorted(legs)          # 8 minus the disabled 5m leg
    assert not any("5m" in k for k in legs), "leg 5 is off and must not appear"
    for want in ("4h UT bullish", "1h UT bullish", "30m UT bullish",
                 "own UT bullish", "4h EMA stack", "dip below EMA14", "reclaim"):
        assert want in legs, (want, sorted(legs))


def test_fired_and_would_enter_are_different_things(evals):
    """Conflating them overstates the live rate by ~5.7x."""
    _sig, rows = evals
    fired = [r for r in rows if r.fired]
    entered = [r for r in rows if r.would_enter]
    assert fired and entered
    assert len(entered) < len(fired), "some firings must be blocked by an open trade"
    assert all(r.fired for r in entered), "would_enter implies fired"
    blocked = [r for r in fired if not r.would_enter]
    assert all(r.position_since_ms is not None for r in blocked)
    assert all("already in a position" in (r.reason_if_skipped or "") for r in blocked)


def test_entry_levels_only_exist_on_bars_that_would_enter(evals):
    _sig, rows = evals
    for r in rows:
        if r.would_enter:
            assert r.entry_ref and r.sl and r.tp and r.time_stop_ms
            assert r.sl < r.entry_ref < r.tp, "long bracket must straddle the entry"
            # The bracket must be whatever the config says, computed one way.
            # Compared through the SAME rounding the log applies, not with a
            # tolerance: a half-cent tolerance straddles the rounding boundary
            # and fails on values that are in fact exactly right (4164.785 ->
            # 4164.78). Equality after rounding is both stricter and correct.
            want_sl, want_tp = _sig.cfg.bracket(r.entry_ref, r.atr_entry)
            assert r.sl == round(want_sl, 2)
            assert r.tp == round(want_tp, 2)
            assert r.time_stop_ms - r.bar_close_ms == 96 * INTERVAL_MS["15m"]
        else:
            assert r.entry_ref is None and r.sl is None and r.tp is None


# ---------------------------------------------------------------------------
# §D3 in miniature — the live path must agree with the harness
# ---------------------------------------------------------------------------


def _harness_signal_bars(lo: int, hi: int) -> list[int]:
    """The harness's entries, expressed as SIGNAL bars inside [lo, hi].

    The harness records the FILL bar; the log records the SIGNAL bar, which is
    one anchor bar earlier.
    """
    step = INTERVAL_MS["15m"]
    with Database() as db:
        p = measure(db, PANEL, "15m", tp_mult=5.0, sl_mult=2.5, cost_bps=0.85,
                    use_5m=False, path_tf="15m")
    return sorted(t.entry_time - step for t in p["long"].trades
                  if lo <= t.entry_time - step <= hi)


def test_would_enter_matches_the_harness_bar_for_bar():
    """The only check that proves the live path is not subtly different.

    **Compared at one slot, deliberately.** `measure()` walks non-overlapping
    sequential trades — it has exactly one position at a time and no way to
    express more. The shipped `RuleConfig` runs `max_concurrent = 2`
    (`OPERATING_PLAN.md` §10.5), so comparing the shipped config against the
    harness compares two different systems and fails by exactly the second slot's
    entries. That is a difference in the CONFIG, not a defect in the live path,
    and hiding it inside this assertion cost a real debugging session.

    So the reconciliation runs at the concurrency the harness models. The next
    test pins what the extra slot is allowed to do.
    """
    import time

    from dataclasses import replace

    _have()
    since = int((time.time() - 60 * 86400) * 1000)
    with Database() as db:
        rows = LiveSignal(measured_cfg(max_concurrent=1)).backfill(
            db, since_ms=since)
    # The two sides read the DB moments apart, and a bar closing in between would
    # give one of them an entry the other cannot have. Dropping the last day from
    # the comparison window removes that race without weakening the check — it is
    # about agreement over 59 days either way.
    lo = rows[0].bar_open_ms
    hi = rows[-1].bar_open_ms - 86_400_000
    mine = sorted(r.bar_open_ms for r in rows if r.would_enter and r.bar_open_ms <= hi)
    theirs = _harness_signal_bars(lo, hi)
    assert mine == theirs, {
        "only_live": [x for x in mine if x not in theirs],
        "only_harness": [x for x in theirs if x not in mine],
    }


def test_the_second_slot_only_ever_adds_entries(evals):
    """Two slots is a strict superset of one, never a different set.

    An extra slot can only let a signal through that the queue was discarding —
    it can never suppress one, and it can never move one to a different bar. If
    it ever does, occupancy is being simulated wrongly rather than merely
    differently, and `OPERATING_PLAN.md` §10.2's "the signals the queue was
    discarding are exactly as good as the ones it kept" would be measuring an
    artefact.
    """
    _sig, rows = evals                       # the shipped config: two slots
    lo = rows[0].bar_open_ms
    hi = rows[-1].bar_open_ms - 86_400_000   # same race guard as above
    two = set(r.bar_open_ms for r in rows if r.would_enter and r.bar_open_ms <= hi)
    one = set(_harness_signal_bars(lo, hi))
    assert one <= two, {"dropped by the second slot": sorted(one - two)}


def test_bounded_simulation_runup_gives_the_same_answer(evals):
    """The backfill simulates occupancy from before the window, not from it.

    A shorter run-up would mislabel bars covered by a position opened earlier.
    """
    import time

    sig, rows = evals
    since = int((time.time() - 60 * 86400) * 1000)
    long_runup = LiveSignal(measured_cfg())
    long_runup.SIM_RUNUP_BARS = 400          # effectively the whole panel
    with Database() as db:
        full = long_runup.backfill(db, since_ms=since)
    assert [r.would_enter for r in full] == [r.would_enter for r in rows]


# ---------------------------------------------------------------------------
# The log as an audit trail
# ---------------------------------------------------------------------------


def test_log_is_append_only_and_restart_safe(tmp_path, evals):
    sig, rows = evals
    path = tmp_path / "signals.jsonl"
    s = LiveSignal(RuleConfig(), log_path=path)
    assert s.write(rows[:100]) == 100
    assert s.write(rows[:100]) == 0, "re-writing the same bars must be a no-op"
    assert s.write(rows[:150]) == 50, "only the new bars are appended"
    back = read_log(path)
    assert len(back) == 150
    assert [r["bar_open_ms"] for r in back] == [r.bar_open_ms for r in rows[:150]]


def test_every_line_is_valid_json_with_the_schema(tmp_path, evals):
    sig, rows = evals
    path = tmp_path / "signals.jsonl"
    LiveSignal(RuleConfig(), log_path=path).write(rows[:50])
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            for k in ("schema", "ts_utc", "bar_open_ms", "fired", "would_enter",
                      "legs", "blocking", "config_hash", "lots"):
                assert k in r, k
            assert r["lots"] == 0.01


def test_ticket_link_is_a_separate_appended_record(tmp_path, evals):
    """§D2: a fill must trace back to its signal, and the log is never rewritten.

    Editing the original line would destroy the property that makes this an
    audit trail at all.
    """
    sig, rows = evals
    path = tmp_path / "signals.jsonl"
    s = LiveSignal(RuleConfig(), log_path=path)
    s.write(rows[:20])
    before = path.read_text(encoding="utf-8")
    s.attach_ticket(rows[0].bar_open_ms, 123456)
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before), "existing lines must not be modified"
    link = json.loads(after.splitlines()[-1])
    assert link["kind"] == "link" and link["order_ticket"] == 123456
    assert link["bar_open_ms"] == rows[0].bar_open_ms


def test_the_forming_bar_is_never_evaluated(evals):
    """`evaluate_latest` must stop at the newest CLOSED bar of ITS OWN symbol."""
    sig, _rows = evals
    assert sig.cfg.symbol == PANEL, "this test reads PANEL's newest bar"
    with Database() as db:
        d = CandleRepository(db).load(PANEL, "15m")
        e = sig.evaluate_latest(db)
    assert e.bar_open_ms == int(d["open_time"].iloc[-1])
    assert e.bar_close_ms == e.bar_open_ms + INTERVAL_MS["15m"]


# ---------------------------------------------------------------------------
# The LIVE default — Bybit gold, fixed +25 / -25
# ---------------------------------------------------------------------------


def test_live_default_is_bybit_gold_with_a_fixed_bracket():
    c = RuleConfig()
    assert c.symbol == "XAUUSDT"
    assert c.broker_symbol == "GOLD"        # where the order would actually go
    assert c.anchor == "15m" and c.side == "long"
    assert c.bracket_mode == "dollars"
    assert (c.tp_usd, c.sl_usd) == (25.0, 25.0)


def test_the_fixed_bracket_is_symmetric_and_atr_independent():
    """25 points either way, whatever ATR is doing.

    That is the point of it and also its cost: the same trade is ~1.8 ATR in a
    violent week and ~3.7 ATR in a quiet one, so the bet changes while the
    numbers stay still. `is_measured_bracket()` is False so the page can say so.
    """
    c = RuleConfig()
    for atr in (5.0, 13.5, 40.0):
        sl, tp = c.bracket(4650.0, atr)
        assert (tp - 4650.0, 4650.0 - sl) == (25.0, 25.0)
    assert not c.is_measured_bracket()


def test_a_short_bracket_mirrors():
    from dataclasses import replace

    c = replace(RuleConfig(), side="short")
    sl, tp = c.bracket(4650.0, 13.5)
    assert tp == 4625.0 and sl == 4675.0


def test_the_log_deduplicates_per_symbol_not_per_bar(tmp_path):
    """Gold on two venues shares the 15-minute grid.

    De-duplicating on `bar_open_ms` alone silently dropped a `XAUUSDT` signal
    because an `MT5:GOLD` row already carried that timestamp — the signal was
    never written and the audit trail showed nothing at all.
    """
    from dataclasses import asdict, replace

    log = tmp_path / "signals.jsonl"
    a = LiveSignal(RuleConfig(), log_path=log)
    b = LiveSignal(measured_cfg(), log_path=log)

    def stub(sig, bar):
        from backtest.engine.live_signal import Evaluation, SCHEMA_VERSION
        return Evaluation(
            schema=SCHEMA_VERSION, ts_utc="2026-01-01T00:00:00+00:00",
            bar_open_ms=bar, bar_close_ms=bar + 900_000,
            symbol=sig.cfg.symbol, broker_symbol=sig.cfg.broker_symbol,
            anchor="15m", side="long", fired=False, would_enter=False,
            position_since_ms=None, legs={}, blocking=[], close=1.0,
            atr_entry=1.0, entry_ref=None, sl=None, tp=None, time_stop_ms=None,
            lots=0.01, in_session=True, spread_at_eval=None,
            reason_if_skipped=None)

    BAR = 1_787_575_500_000
    assert a.write([stub(a, BAR)]) == 1
    assert b.write([stub(b, BAR)]) == 1, "same bar, other symbol, must still write"
    assert a.write([stub(a, BAR)]) == 0, "same bar, same symbol, must not repeat"
    assert len(read_log(log)) == 2
