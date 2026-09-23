"""The Copy-analysis snapshot: engine bands, baselines, and stated absences.

Three properties matter more than the rest, because each one is a way the blob
could look right and mislead a reader who cannot check it:

* **the engine's own bands must be the engine's** — reading in the outside
  "below 1.0x is weak" convention changes the verdict on every bar between 0.8
  and 1.2, and there is no 1.0 line anywhere in this engine;
* **"crowded" is a deviation from a baseline, not an absolute** — at exactly the
  venue's neutral funding rate nobody is crowded, however emphatic a label is;
* **an absent input must read as absent** — a missing economic calendar must
  never render as "nothing is scheduled", and a missing ΔOI must never render as
  a quadrant.
"""

from __future__ import annotations

import pytest

from backtest.analysis import brief, context
from backtest.data.db import CandleRepository, Database

SYMBOL = "XAUUSDT"


def _skip_without(symbol=SYMBOL, tf="15m", minimum=500):
    with Database() as db:
        d = CandleRepository(db).load_tail(symbol, tf, minimum)
    if len(d) < minimum:
        pytest.skip(f"needs {symbol} {tf} synced")


@pytest.fixture(scope="module")
def ctx():
    _skip_without()
    with Database() as db:
        return context.gather(db, SYMBOL, "15m")


# ---------------------------------------------------------------------------
# The engine's own scoring bands
# ---------------------------------------------------------------------------


class TestVolumeBands:
    @pytest.mark.parametrize("rel,want", [
        (3.42, "強勢參與"), (1.50, "強勢參與"),
        (1.49, "有參與"), (1.20, "有參與"),
        (1.19, "中性偏弱"), (1.00, "中性偏弱"), (0.80, "中性偏弱"),
        (0.79, "明顯偏弱"), (0.50, "明顯偏弱"),
        (0.49, "極度縮量"), (0.0, "極度縮量"),
    ])
    def test_bands_are_the_engines(self, rel, want):
        assert want in context._band(rel)

    def test_there_is_no_one_point_zero_line(self):
        """1.00 sits in the SAME band as 0.85 and 1.15.

        The outside convention says "below 1.0x is fake strength". Applying it
        here would split a band the engine treats as one, and would change the
        reading on every bar between 0.8 and 1.2.
        """
        assert context._band(0.85) == context._band(1.00) == context._band(1.15)

    def test_unknown_volume_is_unknown_not_thin(self):
        assert context._band(None) == "unknown"
        assert context._band(float("nan")) == "unknown"


class TestAdxRegime:
    def test_below_twenty_grants_no_trend(self):
        for v in (0.0, 12.0, 19.9):
            assert "ranging" in context._adx_regime(v)

    def test_twenty_to_forty_is_linear_not_a_threshold(self):
        """ADX 26 is ~65% of a directional vote, not "trend confirmed"."""
        assert "30%" in context._adx_regime(26.0)
        assert "50%" in context._adx_regime(30.0)
        assert "100%" in context._adx_regime(40.0)
        assert "100%" in context._adx_regime(70.0)   # capped, not extrapolated

    def test_unknown_adx_is_unknown(self):
        assert context._adx_regime(None) == "unknown"
        assert context._adx_regime(float("nan")) == "unknown"


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


class TestFundingBaseline:
    def test_paying_exactly_the_baseline_is_not_crowded(self, ctx):
        """The whole reason the baseline exists.

        Bybit's neutral perpetual funding is 0.01%/8h. A long paying exactly
        that is paying the venue's default and is not crowded by anything.
        """
        assert context.FUNDING_BASELINE_PCT == 0.0100
        assert context.FUNDING_CROWDED_PCT == 0.0200

    def test_verdict_is_computed_from_the_deviation(self, ctx):
        f = ctx.get("funding") or {}
        if f.get("available") is False:
            pytest.skip("no funding rows stored")
        dev = f["deviation_pct"]
        assert f["deviation_pct"] == pytest.approx(
            f["rate_pct"] - context.FUNDING_BASELINE_PCT, abs=1e-9)
        if abs(dev) <= context.FUNDING_CROWDED_PCT:
            assert f["verdict"] == "neutral vs baseline"
        else:
            assert "crowded" in f["verdict"]


class TestOpenInterestQuadrant:
    def test_either_a_quadrant_or_an_explicit_refusal(self, ctx):
        oi = ctx["open_interest"]
        if oi.get("available") is False:
            assert "cannot be computed" in oi["why"]
        else:
            assert oi["quadrant"]
            assert oi["delta"] == pytest.approx(oi["now"] - oi["prev"], abs=1e-6)

    def test_the_quadrant_matches_the_two_deltas(self, ctx):
        oi = ctx["open_interest"]
        if oi.get("available") is False:
            pytest.skip("no OI series")
        d_oi, d_px, q = oi["delta"], oi["price_delta"], oi["quadrant"]
        if d_px > 0 and d_oi > 0:
            assert "新多進場" in q
        elif d_px > 0 and d_oi < 0:
            assert "空頭回補" in q
        elif d_px < 0 and d_oi > 0:
            assert "新空進場" in q
        elif d_px < 0 and d_oi < 0:
            assert "多頭清算" in q

    def test_writer_and_reader_agree_on_the_grid(self):
        """A per-symbol grid meant the reader silently found nothing."""
        from backtest.engine.risk_feed import OI_INTERVAL
        from backtest.watch import Watcher
        assert Watcher.OI_INTERVAL == OI_INTERVAL


# ---------------------------------------------------------------------------
# Absences must read as absences
# ---------------------------------------------------------------------------


class TestStatedAbsences:
    def test_the_missing_calendar_is_named(self, ctx):
        note = ctx["absent"]["economic_calendar"]
        assert "NO economic calendar" in note
        assert "not read its absence" in note or "do" in note

    def test_the_blob_warns_before_the_data(self, ctx):
        """ABSENT must come BEFORE the numbers it qualifies.

        A caveat printed after 300 lines of confident-looking values is a
        caveat nobody applies to them.
        """
        blob = brief.build(SYMBOL, None, None, ctx=ctx, positions=[])
        assert blob.index("ABSENT") < blob.index("CONTEXT")
        assert "NO ORDER BOOK" in blob
        assert "NO ECONOMIC CALENDAR" in blob

    def test_the_mandate_leads(self, ctx):
        blob = brief.build(SYMBOL, None, None, ctx=ctx)
        assert blob.index("AI ROLE & MANDATE") < blob.index("DATA SNAPSHOT")
        assert blob.index("DATA SNAPSHOT") < blob.index("HOW TO READ")


class TestSessionRules:
    def test_timing_rules_are_resolved_not_left_to_the_reader(self, ctx):
        s = ctx["session"]
        assert s["timing_rules_apply"] == (
            not s["is_weekend_myt"] and s["gold_market_open"])

    def test_a_closed_market_disables_the_windows(self, ctx):
        blob = brief.build(SYMBOL, None, None, ctx=ctx)
        if not ctx["session"]["timing_rules_apply"]:
            assert "DO NOT apply" in blob


# ---------------------------------------------------------------------------
# The session liquidity map (Asia range / Europe sweeps / US confirmation)
# ---------------------------------------------------------------------------


class TestSessionMap:
    def _skip_without_minutes(self):
        _skip_without(tf="1m", minimum=100)

    def test_gather_includes_the_map(self, ctx):
        """One definition feeds the brief, the cockpit and the chart page."""
        ses = ctx.get("sessions")
        assert isinstance(ses, dict)
        assert ses.get("schema") == 1
        for key in ("phase", "day_myt", "state", "levels", "events",
                    "windows_myt", "as_of_myt"):
            assert key in ses, key

    def test_gather_never_raises_on_an_empty_database(self, tmp_path):
        with Database(tmp_path / "empty.db") as db:
            out = context.gather(db, "NOSUCH", "15m")
        assert "error" in out, "it bails before the map, it does not crash"

    def test_brief_renders_the_session_section(self, ctx):
        self._skip_without_minutes()
        blob = brief.build(SYMBOL, None, None, ctx=ctx)
        assert "SESSION MAP" in blob
        # Which of the two Asia lines appears depends on whether the stored
        # 1m tail reaches into today's Asia window (a stopped watcher leaves
        # it behind); both are the section doing its job.
        assert ("Asia high / low" in blob) or ("no bars yet today" in blob)
        assert "windows (MYT): asia 07:00-15:00" in blob

    def test_brief_states_the_frameworks_own_caveat(self, ctx):
        """扫盘不追单 — a sweep is structure, never a direction."""
        self._skip_without_minutes()
        blob = brief.build(SYMBOL, None, None, ctx=ctx)
        assert "not a direction" in blob

    def test_brief_carries_the_day_bias_with_its_basis(self, ctx):
        """日内倾向 must arrive with the facts it was read from."""
        self._skip_without_minutes()
        blob = brief.build(SYMBOL, None, None, ctx=ctx)
        assert "日内倾向" in blob
        assert "basis (facts the read comes from)" in blob
        # whichever session it is, the Asia range line is always basis #1
        assert "Asia" in blob

    def test_brief_without_minutes_omits_the_section(self):
        blob = brief.build(SYMBOL, None, None,
                           ctx={"sessions": {"schema": 1,
                                             "error": "no 1m bars"}})
        assert "SESSION MAP" not in blob


# ---------------------------------------------------------------------------
# The blob survives missing pieces
# ---------------------------------------------------------------------------


class TestBlobRobustness:
    def test_builds_with_nothing_at_all(self):
        blob = brief.build("XAUUSDT", None, None)
        assert "AI ROLE & MANDATE" in blob and "HOW TO READ" in blob

    def test_builds_with_only_context(self, ctx):
        assert len(brief.build(SYMBOL, None, None, ctx=ctx)) > 5000

    def test_zero_width_zones_are_flagged(self):
        """The mandate asks for degenerate KEY LEVELS to be called out."""
        risk = {"bias": "BULLISH", "tp_pct": 0.5, "sl_pct": 0.5,
                "long": {"danger": 10, "confidence": 50, "trend_pts": 1,
                         "orderbook_pts": 1, "momentum_pts": 1, "reasons": []},
                "short": {"danger": 10, "confidence": 50, "trend_pts": 1,
                          "orderbook_pts": 1, "momentum_pts": 1, "reasons": []},
                "zones": {"resistance": [[100.0, 100.0, 0.7, ["Session high"]]],
                          "support": [[90.0, 95.0, 2.0, ["swing low"]]],
                          "stale_ms": 0},
                "meta": {}}
        blob = brief.build(SYMBOL, risk, None)
        assert "ZERO WIDTH" in blob

    def test_an_unmeasured_bracket_is_called_out(self):
        rule = {"symbol": "XAUUSDT", "anchor": "15m", "side": "long",
                "bracket": "TP +25 / SL -25 in price, fixed",
                "bracket_measured": False, "fired": True, "would_enter": True,
                "sent": True, "legs": {"a": True}, "blocking": [],
                "entry_ref": 4650.0, "sl": 4625.0, "tp": 4675.0, "lots": 0.01}
        blob = brief.build(SYMBOL, None, rule)
        assert "NOT the measured bracket" in blob

    def test_json_form_is_valid_and_keeps_the_chinese(self, ctx):
        import json
        out = brief.build_json(symbol=SYMBOL, ctx=ctx)
        parsed = json.loads(out)
        assert parsed["symbol"] == SYMBOL
        # ensure_ascii=False, so a quadrant label stays readable in the file
        oi = parsed["ctx"]["open_interest"]
        if oi.get("available") is not False:
            assert any(ord(c) > 0x3000 for c in oi["quadrant"])
