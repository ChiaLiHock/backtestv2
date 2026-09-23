"""Broker bar ingestion and the rule-validation harness.

`AUTOMATION.md` §9 test 7 is here, plus the guard that matters more than any of
them: that `tools/validate_rule.py` still reproduces the numbers in
`ENTRY_RULES.md`. A measurement harness that has quietly drifted from the study
it was built to check is worse than not having one, because every later result it
produces looks authoritative.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backtest.data import mt5_rates
from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.indicators.base import IndicatorConfig
from backtest.tools import validate_rule as V

GOLD = "MT5:GOLD"


@pytest.fixture(scope="module")
def xau_panels():
    """The reference XAUUSDT measurement, computed once for the whole module.

    Each run walks ~48k 15m bars against a 240k-bar 1m path; doing it per test
    quadrupled the suite's runtime for no extra coverage.
    """
    _have("XAUUSDT", "15m", 5000)
    with Database() as db:
        return V.measure(db, "XAUUSDT", "15m", tp_mult=2.5, sl_mult=2.5,
                         cost_bps=11.0, use_5m=True, path_tf="1m")


def _have(symbol: str, tf: str, minimum: int = 100):
    with Database() as db:
        d = CandleRepository(db).load(symbol, tf)
    if len(d) < minimum:
        pytest.skip(f"needs {symbol} {tf} synced")
    return d


# ---------------------------------------------------------------------------
# §9 test 7 — server time became true UTC
# ---------------------------------------------------------------------------


def test_h4_hours_differ_between_summer_and_winter():
    """The DST test. EEST puts H4 opens on 21/01/05/09/13/17 UTC, EET on
    22/02/06/10/14/18.

    The original version of this test only checked summer and passed against a
    feed that used a fixed +3 all year — the identical hour set in both seasons
    was the bug's signature and nothing was looking for it.
    """
    d = _have(GOLD, "4h", 2000)
    dt = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    month, hour = dt.dt.month, dt.dt.hour
    summer = sorted(hour[month.isin([5, 6, 7, 8])].unique().tolist())
    winter = sorted(hour[month.isin([11, 12, 1, 2])].unique().tolist())
    assert summer == [1, 5, 9, 13, 17, 21], summer
    assert winter == [2, 6, 10, 14, 18, 22], winter
    assert summer != winter, (
        "identical H4 hours in both seasons means the conversion used a fixed "
        "offset and every winter bar is an hour early"
    )


@pytest.mark.parametrize("server_wall, expected_utc", [
    ("2026-08-21 20:00", "2026-08-21 17:00"),   # EEST, +3
    ("2026-01-15 20:00", "2026-01-15 18:00"),   # EET,  +2
])
def test_known_bars_convert_to_the_right_utc_instant(server_wall, expected_utc):
    """`AUTOMATION.md` §9 test 7, now covering both sides of the transition."""
    from backtest.data.broker_clock import server_naive_to_utc_ms

    wall = datetime.strptime(server_wall, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    got = int(server_naive_to_utc_ms([int(wall.timestamp())])[0])
    assert (datetime.fromtimestamp(got / 1000, timezone.utc)
            .strftime("%Y-%m-%d %H:%M")) == expected_utc


def test_stored_bars_carry_those_instants():
    """The same two anchors, read back out of the database this time."""
    d = _have(GOLD, "4h", 2000)
    dt = pd.to_datetime(d["open_time"], unit="ms", utc=True)
    for day, hour in (("2026-08-21", 17), ("2026-01-15", 18)):
        on = dt[dt.dt.strftime("%Y-%m-%d") == day]
        if on.empty:
            pytest.skip(f"no bars stored on {day}")
        assert hour in set(on.dt.hour), (day, sorted(set(on.dt.hour)))


def test_dst_fold_and_gap_are_asserted_not_guessed():
    """A bar inside the ambiguous or nonexistent local hour must raise.

    `GOLD` is shut on the Sunday both happen, so this never fires in practice —
    which is the point of asserting it rather than silently picking a side.
    """
    import numpy as np

    from backtest.data.broker_clock import AmbiguousBrokerTime, server_naive_to_utc_ms

    # 2025-10-26 03:30 local occurs twice in Europe/Athens.
    fold = datetime(2025, 10, 26, 3, 30, tzinfo=timezone.utc)
    with pytest.raises(AmbiguousBrokerTime):
        server_naive_to_utc_ms([int(fold.timestamp())])
    # 2025-03-30 03:30 local does not exist.
    gap = datetime(2025, 3, 30, 3, 30, tzinfo=timezone.utc)
    with pytest.raises(AmbiguousBrokerTime):
        server_naive_to_utc_ms([int(gap.timestamp())])
    # And no stored bar trips either of them.
    d = _have(GOLD, "15m", 5000)
    assert len(d) > 0


def test_h4_partition_is_not_rebucketed_onto_utc():
    """Explicitly the opposite of the usual instinct — see mt5_rates' docstring.

    True in both seasons: +2 and +3 are each odd multiples of an hour against a
    4-hour grid, so neither lands on a UTC boundary.
    """
    d = _have(GOLD, "4h", 500)
    t = d["open_time"].to_numpy(dtype="int64")
    assert not np.any(t % INTERVAL_MS["4h"] == 0), (
        "H4 bars are aligned to UTC boundaries, so they were re-bucketed; the "
        "broker publishes them on a UTC+3 day and the UT level is path-dependent"
    )


def test_intraday_bars_are_aligned_to_their_own_step():
    """UTC+3 is a whole number of hours, so everything below 4h still lands on grid."""
    for tf in ("15m", "30m", "1h"):
        d = _have(GOLD, tf, 500)
        t = d["open_time"].to_numpy(dtype="int64")
        assert np.all(t % INTERVAL_MS[tf] == 0), tf


def test_broker_bars_agree_with_bybit_at_the_same_utc_instant():
    """The end-to-end check on the conversion.

    Two independent feeds for the same metal. If the clock were wrong by even one
    hour the prices at a shared timestamp would diverge by far more than the
    contract basis, and gold moves enough in an hour to make that unmissable.
    """
    with Database() as db:
        repo = CandleRepository(db)
        a = repo.load(GOLD, "1h")
        b = repo.load("XAUUSDT", "1h")
    if len(a) < 100 or len(b) < 100:
        pytest.skip("needs both feeds synced")

    merged = a.merge(b, on="open_time", suffixes=("_mt5", "_byb"))
    assert len(merged) > 500, f"only {len(merged)} overlapping hours"
    diff = (merged["close_mt5"] - merged["close_byb"]).to_numpy()
    med = float(np.median(diff))
    # Spot trades under the perpetual; the measured basis is about -3.4.
    assert -12.0 < med < 2.0, f"median basis {med:.2f} — is the clock right?"

    # DISPERSION is the test, not the median. Measured across shifts of -2..+2
    # hours the median stays near -3.4 at every one of them, so it cannot detect
    # a clock error at all. The scatter can: MAD is 1.34 when the clock is right
    # and 5.9-10.5 when it is an hour or two out.
    mad = float(np.median(np.abs(diff - med)))
    assert mad < 3.0, (
        f"basis MAD {mad:.2f} — the two feeds disagree bar to bar, which is a "
        "clock error rather than a contract basis"
    )


def test_a_one_hour_error_would_be_caught_by_that_check():
    """Guards the guard: shifting the feed by an hour must break the agreement."""
    with Database() as db:
        repo = CandleRepository(db)
        a = repo.load(GOLD, "1h")
        b = repo.load("XAUUSDT", "1h")
    if len(a) < 100 or len(b) < 100:
        pytest.skip("needs both feeds synced")
    for shift in (-1, 1):
        x = a.copy()
        x["open_time"] = x["open_time"] + shift * INTERVAL_MS["1h"]
        merged = x.merge(b, on="open_time", suffixes=("_mt5", "_byb"))
        diff = (merged["close_mt5"] - merged["close_byb"]).to_numpy()
        med = float(np.median(diff))
        mad = float(np.median(np.abs(diff - med)))
        assert -12.0 < med < 2.0, (
            f"shift {shift:+d}h moved the MEDIAN out of range, which would mean "
            "the check above passes for the wrong reason"
        )
        assert mad > 3.0, (
            f"a {shift:+d}h shift left MAD at {mad:.2f}, so the check above "
            "cannot detect a clock error"
        )


def test_turnover_is_not_fabricated():
    """This feed has no traded size. A turnover column invented from tick counts
    would be a number an absolute liquidity gate could silently be built on."""
    d = _have(GOLD, "15m", 500)
    assert (d["turnover"] == 0).all()
    assert (d["volume"] > 0).mean() > 0.95, "tick volume should be populated"


# ---------------------------------------------------------------------------
# The harness must still reproduce the study
# ---------------------------------------------------------------------------


def test_validate_rule_reproduces_entry_rules_on_xauusdt(xau_panels):
    """`ENTRY_RULES.md` §4.3 / §6: n=112, pooled 67.0%, long 70.5 / short 64.7.

    This is the anchor for every number the harness will ever produce. It failed
    at first — 53.0% on n=149 — because the `gold_session` filter was missing and
    timeouts were priced at the bar extreme rather than its close. Anyone editing
    a leg definition, the walk-forward, or the sequencing will break this, which
    is the point.
    """
    s = V.summarise(xau_panels)
    assert s["pooled"]["n"] == 112, s["pooled"]
    assert s["pooled"]["win_pct"] == pytest.approx(67.0, abs=0.1)
    assert s["long"]["win_pct"] == pytest.approx(70.5, abs=0.1)
    assert s["short"]["win_pct"] == pytest.approx(64.7, abs=0.1)


def test_dropping_the_session_filter_changes_the_answer():
    """Records *why* the filter is load-bearing, so removing it cannot look free."""
    _have("XAUUSDT", "15m", 5000)
    with Database() as db:
        loose = V.summarise(V.measure(db, "XAUUSDT", "15m", cost_bps=11.0,
                                      path_tf="1m", session="none"))
    assert loose["pooled"]["n"] > 112
    assert loose["pooled"]["win_pct"] < 60.0, (
        "weekend bars used to cost 14 points of win rate; if they no longer do, "
        "the session filter or the data changed"
    )


def test_entries_are_taken_at_the_next_bars_open(xau_panels):
    """No look-ahead: a trade may never be priced at the signal bar."""
    panels = xau_panels
    with Database() as db:
        d = CandleRepository(db).load("XAUUSDT", "15m")
    opens = dict(zip(d["open_time"].to_numpy(dtype="int64"), d["open"].to_numpy()))
    checked = 0
    for t in panels["long"].trades + panels["short"].trades:
        assert t.entry_price == pytest.approx(opens[t.entry_time], abs=1e-9)
        assert t.exit_time >= t.entry_time
        checked += 1
    assert checked > 50


def test_positions_never_overlap(xau_panels):
    """§6.2: a bar-level screen counts the same trend repeatedly and its
    confidence interval lies."""
    panels = xau_panels
    for side in ("long", "short"):
        ts = sorted(panels[side].trades, key=lambda t: t.entry_time)
        for a, b in zip(ts, ts[1:]):
            assert b.entry_time >= a.exit_time, f"{side} positions overlap"


def test_ties_resolve_as_losses():
    long_tie = V.walk_forward(
        np.array([0], dtype="int64"), np.array([110.0]), np.array([90.0]),
        np.array([100.0]), 0, 100.0, 105.0, 95.0, True, 10_000)
    assert long_tie[0] == "sl" and long_tie[3] is True
    short_tie = V.walk_forward(
        np.array([0], dtype="int64"), np.array([110.0]), np.array([90.0]),
        np.array([100.0]), 0, 100.0, 95.0, 105.0, False, 10_000)
    assert short_tie[0] == "sl" and short_tie[3] is True


def test_timeout_exits_at_the_close_not_the_extreme():
    """Pricing a timeout at the worst point of the bar charges a loss that never
    happened — one of the two bugs that broke the reproduction."""
    r = V.walk_forward(
        np.array([0], dtype="int64"), np.array([101.0]), np.array([99.0]),
        np.array([100.5]), 0, 100.0, 110.0, 90.0, True, 0)
    assert r[0] == "timeout"
    assert r[1] == pytest.approx(100.5)


# ---------------------------------------------------------------------------
# The new legs
# ---------------------------------------------------------------------------


def _ctx(db, symbol="XAUUSDT", anchor="15m", lookback=3):
    return V.build_context(db, symbol, anchor, IndicatorConfig(), lookback)[0]


def test_dip_lookback_includes_the_current_bar():
    from backtest.engine.signals import evaluate_signal

    _have("XAUUSDT", "15m", 5000)
    with Database() as db:
        c1 = _ctx(db, lookback=1)
        c3 = _ctx(db, lookback=3)
    one = np.asarray(evaluate_signal("mtf_dip_long", c1), dtype=bool)
    three = np.asarray(evaluate_signal("mtf_dip_long", c3), dtype=bool)
    ema = c1.ind["ema_14"].to_numpy()
    pierced = c1.low < ema
    assert np.array_equal(one, pierced), "lookback 1 must be the bar itself"
    assert (three & ~one).any(), "a wider lookback must admit more bars"
    assert np.all(one <= three), "widening the lookback may never remove a bar"


def test_reclaim_needs_both_halves():
    from backtest.engine.signals import evaluate_signal

    _have("XAUUSDT", "15m", 5000)
    with Database() as db:
        c = _ctx(db)
    got = np.asarray(evaluate_signal("mtf_reclaim_long", c), dtype=bool)
    ema = c.ind["ema_14"].to_numpy()
    prev = np.roll(c.close, 1)
    prev[0] = np.nan
    with np.errstate(invalid="ignore"):
        assert np.array_equal(got, (c.close > ema) & (c.close > prev))
    assert got.any() and not got.all()


def test_long_and_short_legs_are_exact_mirrors():
    """§4.2 says every leg mirrors. If one side drifts, the symmetry evidence in
    §6 stops meaning anything."""
    assert len(V.LEGS_LONG) == len(V.LEGS_SHORT)
    for (_, a), (_, b) in zip(V.LEGS_LONG, V.LEGS_SHORT):
        assert a.replace("bullish", "X").replace("_long", "_X") == \
               b.replace("bearish", "X").replace("_short", "_X"), (a, b)


def test_a_missing_timeframe_truncates_the_panel_and_says_so():
    """The trap that cost a measurement pass: a leg whose timeframe has no data
    can never be true, so every signal before that date dies with no error."""
    _have(GOLD, "15m", 5000)
    # horizon_bars=2 keeps the forward walk trivial. What is under test is the
    # coverage warning and the panel bounds, not the P&L, and the full walk over
    # 5,600 signals made this the slowest test in the suite by 3x.
    with Database() as db:
        panels = V.measure(db, GOLD, "15m", path_tf="15m", use_5m=True,
                           horizon_bars=2)
        wide = V.measure(db, GOLD, "15m", path_tf="15m", use_5m=False,
                         horizon_bars=2)
    warned = " ".join(panels["long"].warnings)
    assert "5m" in warned and "truncated" in warned, panels["long"].warnings
    assert not wide["long"].warnings
    assert wide["long"].start < panels["long"].start
