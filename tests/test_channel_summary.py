"""The per-channel win rates shown in the Trades panel and the By-signal table.

This is the number the owner reads to decide whether a mined pattern is still
worth watching, so the ways it could flatter a channel are what get pinned:

* an unresolved fire counted as a loss would make the win rate a function of how
  recently the page was opened;
* a scratch exit counted as a win would quietly move every bracket's break-even;
* `0%` where nothing has settled reads as "it lost them all" rather than "it has
  not traded";
* a channel that silently vanished when it had no trades would be indis-
  tinguishable from a channel that does not exist.
"""

from __future__ import annotations

from backtest.analysis.report import DAY_MS, _breakeven, _channel_summary

CHANNELS = ("rule", "signal2", "signal3", "signal4", "signal5", "signal7")
NOW = 1_787_800_000_000


def rows(*specs):
    """(net, resolved) pairs -> trade rows shaped like the lane builders'."""
    return [{"net": n, "resolved": r} for n, r in specs]


def aged(*specs):
    """(net, resolved, days_ago) -> rows carrying an entry_time."""
    return [{"net": n, "resolved": r, "entry_time": NOW - int(d * DAY_MS)}
            for n, r, d in specs]


def by_name(out):
    return {r["signal"]: r for r in out}


def summary(rule=[], s2=[], s3=[], s4=[], s5=[], s7=[], **kw):
    """The six-list call, so no test has to spell six empty lists out."""
    return _channel_summary(rule, s2, s3, s4, s5, s7, **kw)


class TestShape:
    def test_all_six_channels_are_always_present(self):
        out = summary()
        assert [r["signal"] for r in out] == list(CHANNELS)

    def test_an_empty_channel_reports_no_rate_rather_than_zero(self):
        """0% would read as 'lost every trade'. None renders as '—'."""
        out = by_name(summary())
        assert out["signal4"]["win_pct"] is None
        assert out["signal4"]["n"] == 0 and out["signal4"]["net"] == 0.0

    def test_only_the_rule_is_marked_measured(self):
        out = by_name(summary())
        assert out["rule"]["measured"] is True
        assert all(out[c]["measured"] is False for c in CHANNELS if c != "rule")

    def test_none_is_tolerated_for_a_missing_list(self):
        out = _channel_summary(None, None, None, None, None, None)
        assert all(r["n"] == 0 for r in out)


class TestUnresolvedTradesAreNotLosses:
    def test_open_trades_stay_out_of_the_denominator(self):
        out = by_name(summary(
            s2=rows((10.0, True), (-10.0, True), (0.0, False), (0.0, False))))
        s2 = out["signal2"]
        assert s2["n"] == 2, "only settled trades are counted"
        assert s2["open"] == 2
        assert s2["win_pct"] == 50.0, "not 25% -- the open two have no outcome"

    def test_open_trades_do_not_move_net(self):
        out = by_name(summary(
            s2=rows((10.0, True), (999.0, False))))
        assert out["signal2"]["net"] == 10.0

    def test_a_channel_with_only_open_trades_has_no_rate(self):
        out = by_name(summary(
            s2=rows((0.0, False), (0.0, False))))
        assert out["signal2"]["win_pct"] is None
        assert out["signal2"]["open"] == 2

    def test_a_row_without_the_key_counts_as_settled(self):
        """`resolved is not False` -- the backtest rows carry no such field."""
        out = by_name(summary(rule=[{"net": 5.0}]))
        assert out["rule"]["n"] == 1 and out["rule"]["win_pct"] == 100.0


class TestTiesAreLosses:
    def test_exactly_zero_is_not_a_win(self):
        out = by_name(summary(
            s5=rows((0.0, True), (1.0, True))))
        assert out["signal5"]["wins"] == 1
        assert out["signal5"]["win_pct"] == 50.0

    def test_negative_is_a_loss(self):
        out = by_name(summary(rule=rows((-0.01, True))))
        assert out["rule"]["wins"] == 0 and out["rule"]["win_pct"] == 0.0

    def test_signal7_follows_the_same_rule(self):
        """A scratch on the ExpD entry is a loss, like every bracket trade."""
        out = by_name(summary(s7=rows((0.0, True), (2.0, True))))
        assert out["signal7"]["wins"] == 1
        assert out["signal7"]["win_pct"] == 50.0


class TestArithmetic:
    def test_win_pct_is_rounded_to_one_decimal(self):
        out = by_name(summary(
            s3=rows(*[(1.0, True)] * 2, *[(-1.0, True)] * 1)))
        assert out["signal3"]["win_pct"] == 66.7

    def test_net_sums_and_rounds(self):
        """Halves are avoided on purpose -- `round` is banker's rounding and
        2.505 is not exactly representable, so a .5 case here would be testing
        float layout rather than this function."""
        out = by_name(summary(
            s4=rows((1.25, True), (2.0, True), (-0.5, True))))
        assert out["signal4"]["net"] == 2.75
        assert out["signal4"]["n"] == 3

    def test_wins_and_n_agree_with_the_percentage(self):
        out = by_name(summary(
            s5=rows(*[(1.0, True)] * 7, *[(-1.0, True)] * 3)))
        s5 = out["signal5"]
        assert s5["wins"] == 7 and s5["n"] == 10
        assert round(100.0 * s5["wins"] / s5["n"], 1) == s5["win_pct"]

    def test_channels_do_not_leak_into_each_other(self):
        out = by_name(summary(
            rule=rows((1.0, True)), s2=rows((-1.0, True))))
        assert out["rule"]["wins"] == 1 and out["signal2"]["wins"] == 0
        assert out["signal3"]["n"] == 0


class TestBreakEven:
    """The number every win rate on the page has to be read against."""

    def test_symmetric_brackets_are_above_fifty_not_at_it(self):
        """Cost is what puts it above 50%. A page quoting 50% would flatter
        every channel by roughly a point."""
        assert _breakeven(25.0, 25.0, 4600.0) == 50.8
        assert _breakeven(15.0, 15.0, 4600.0) == 51.3
        assert _breakeven(10.0, 10.0, 4600.0) == 52.0

    def test_signal4s_asymmetric_bracket_needs_far_more(self):
        """TP15/SL25 needs 63.5%, so 66% there is a thinner edge than 60% at
        TP25/SL25. This is the whole reason the card shows the difference."""
        assert _breakeven(15.0, 25.0, 4600.0) == 63.5

    def test_it_agrees_with_the_engines_own_constants(self):
        from backtest.engine.signal4 import BREAKEVEN as BE4
        from backtest.engine.signal5 import BREAKEVEN as BE5

        assert _breakeven(15.0, 25.0, 4600.0) == round(BE4 * 100, 1)
        assert _breakeven(10.0, 10.0, 4600.0) == round(BE5 * 100, 1)

    def test_a_richer_bracket_needs_a_lower_win_rate(self):
        assert _breakeven(50.0, 10.0, 4600.0) < _breakeven(10.0, 50.0, 4600.0)

    def test_zero_price_means_no_cost_and_a_clean_fifty(self):
        assert _breakeven(20.0, 20.0, 0.0) == 50.0

    def test_nonsense_returns_none_rather_than_raising(self):
        assert _breakeven(0.0, 0.0, 4600.0) is None
        assert _breakeven("x", 25.0, 4600.0) is None


class TestRollingWindows:
    def test_windows_slice_on_entry_time(self):
        out = {r["signal"]: r for r in summary(
            s2=aged((1.0, True, 1), (1.0, True, 3), (-1.0, True, 20),
                    (-1.0, True, 200)),
            now_ms=NOW, price=4600.0)}
        w = out["signal2"]["windows"]
        assert w["7d"]["n"] == 2 and w["7d"]["wins"] == 2
        assert w["30d"]["n"] == 3 and w["30d"]["wins"] == 2
        assert w["90d"]["n"] == 3, "the 200-day-old trade is outside 90d"
        assert out["signal2"]["n"] == 4, "the whole-window tally keeps it"

    def test_a_decaying_channel_shows_a_falling_sequence(self):
        """The point of the card: recent failure that the pooled number hides."""
        out = {r["signal"]: r for r in summary(
            s4=aged(*[(1.0, True, 60)] * 40, *[(-1.0, True, 2)] * 8),
            now_ms=NOW, price=4600.0)}
        w = out["signal4"]["windows"]
        assert w["7d"]["win_pct"] == 0.0
        assert w["90d"]["win_pct"] > 80.0
        assert out["signal4"]["win_pct"] > 80.0, "pooled still looks fine"

    def test_windows_are_absent_without_a_clock(self):
        out = summary()
        assert all(r["windows"] == {} for r in out)

    def test_a_row_without_an_entry_time_falls_outside_every_window(self):
        out = {r["signal"]: r for r in summary(
            rule=rows((1.0, True)), now_ms=NOW, price=4600.0)}
        assert out["rule"]["n"] == 1
        assert out["rule"]["windows"]["7d"]["n"] == 0

    def test_every_channel_carries_its_bracket_and_breakeven(self):
        out = {r["signal"]: r for r in summary(now_ms=NOW, price=4600.0)}
        assert out["rule"]["bracket"] == "TP 25 / SL 25"
        assert out["rule"]["breakeven"] == 50.8
        assert out["signal2"]["bracket"] == "TP 25 / SL 25"
        assert out["signal2"]["breakeven"] == 50.8
        assert out["signal4"]["bracket"] == "TP 15 / SL 25"
        assert out["signal4"]["breakeven"] == 63.5
        assert out["signal5"]["bracket"] == "TP 10 / SL 10"
        assert out["signal5"]["breakeven"] == 52.0
        assert out["signal3"]["bracket"] == "structural (zone-width)"
        assert out["signal3"]["breakeven"] is None
        assert out["signal7"]["bracket"] == "structural (ExpD gate)"
        assert out["signal7"]["breakeven"] is None


class TestTheStaticReportStillWorks:
    def test_backtest_payload_declares_an_empty_channel_list(self):
        """The page tests one key rather than branching on payload type."""
        import inspect

        import backtest.analysis.report as rep

        src = inspect.getsource(rep.build_payload)
        assert '"channels": []' in src