"""The cockpit's geometry: what is above, what is below, and how far.

Two bugs were found building this, both of which produced confident, plausible
numbers that pointed at the wrong level. They are what most of this file pins.

**1. Side is a function of the CURRENT price, not the snapshot's label.**
Snapshots are built on a closed H1 bar and labelled at that bar's close. Price
keeps moving inside the hour. Measured on 2026-08-28: the cached snapshot called
4637.95 "support" while spot was 4597 -- price had broken through, so the stored
label put a level *above* price into the support list, and `position` came out
at -6.1 on a scale defined to run 0 to 1.

**2. A zone price is standing INSIDE is the nearest obstacle in both
directions.** Same day: spot 4595.61 sat inside a 4566.82-4637.95 zone of
strength 22.2, while the nearest zone entirely above was 4644.60 at strength
2.8. Reporting 4644.60 as "next resistance" named the weaker level 49 points
away and skipped the far stronger edge 42 points away that price was already
pressed against.

Both were wrong in the same direction: they made the room look cleaner than it
was, which is the failure mode that matters on a page built to answer "wait or
go".
"""

from __future__ import annotations

import pytest

from backtest.analysis.cockpit import _room, _split

ATR = 10.0


class Snap:
    """The two fields `_split` reads off a ZoneSnapshot."""

    def __init__(self, resistance=(), support=()):
        self.resistance = tuple(resistance)
        self.support = tuple(support)


def z(low, high, strength=1.0, sources=("30m swing high",)):
    return (low, high, strength, sources)


def split(snap, price):
    return _split(snap, price, ATR)


class TestSideComesFromCurrentPrice:
    def test_a_broken_support_is_reported_as_resistance(self):
        """The 2026-08-28 case: price fell through a level labelled support."""
        above, below, inside = split(Snap(support=[z(4630, 4640)]), price=4597.0)
        assert len(above) == 1 and not below
        assert above[0]["side"] == "resistance"
        assert above[0]["was"] == "support"
        assert above[0]["flipped"] is True

    def test_a_reclaimed_resistance_is_reported_as_support(self):
        above, below, _ = split(Snap(resistance=[z(4500, 4510)]), price=4600.0)
        assert not above and len(below) == 1
        assert below[0]["side"] == "support" and below[0]["flipped"] is True

    def test_a_level_that_still_agrees_is_not_flagged(self):
        above, _, _ = split(Snap(resistance=[z(4650, 4660)]), price=4600.0)
        assert above[0]["flipped"] is False

    def test_price_within_the_band_is_inside_not_either_side(self):
        above, below, inside = split(Snap(support=[z(4590, 4610)]), price=4600.0)
        assert not above and not below
        assert len(inside) == 1 and inside[0]["side"] == "inside"
        assert inside[0]["dist"] == 0.0

    def test_the_band_edges_are_inclusive(self):
        """Exactly on the edge is inside it, not clear of it."""
        _, _, on_low = split(Snap(support=[z(4590, 4610)]), price=4590.0)
        _, _, on_high = split(Snap(support=[z(4590, 4610)]), price=4610.0)
        assert len(on_low) == 1 and len(on_high) == 1


class TestDistance:
    def test_distance_is_to_the_near_edge_not_the_middle(self):
        above, _, _ = split(Snap(resistance=[z(4650, 4700)]), price=4600.0)
        assert above[0]["edge"] == 4650.0
        assert above[0]["dist"] == 50.0, "the far edge would say 100"

    def test_below_uses_the_high_edge(self):
        _, below, _ = split(Snap(support=[z(4500, 4550)]), price=4600.0)
        assert below[0]["edge"] == 4550.0 and below[0]["dist"] == 50.0

    def test_atr_distance_divides_by_the_reference_atr(self):
        above, _, _ = split(Snap(resistance=[z(4625, 4630)]), price=4600.0)
        assert above[0]["dist_atr"] == 2.5

    def test_nearest_comes_first(self):
        above, _, _ = split(Snap(resistance=[z(4700, 4710), z(4620, 4630),
                                             z(4660, 4670)]), price=4600.0)
        assert [r["edge"] for r in above] == [4620.0, 4660.0, 4700.0]

    def test_a_zone_beyond_two_atr_is_marked_out_of_play(self):
        above, _, _ = split(Snap(resistance=[z(4615, 4620), z(4700, 4710)]),
                            price=4600.0)
        assert above[0]["in_play"] is True
        assert above[1]["in_play"] is False

    def test_a_malformed_zone_is_skipped_not_fatal(self):
        above, _, _ = split(Snap(resistance=[("x", "y", 1.0, ()), z(4650, 4660)]),
                            price=4600.0)
        assert len(above) == 1


class TestTheZoneYouAreStandingIn:
    def test_its_far_edge_is_the_nearest_obstacle_each_way(self):
        """The second 2026-08-28 bug, in miniature."""
        above, below, inside = split(
            Snap(resistance=[z(4644, 4648, strength=2.8)],
                 support=[z(4566, 4638, strength=22.2)]),
            price=4595.61)
        room = _room(above, below, inside, 4595.61, ATR)
        assert room["up"]["edge"] == 4638.0, \
            "the containing zone's top, not the weaker zone beyond it"
        assert room["up"]["strength"] == 22.2
        assert room["up"]["containing"] is True
        assert room["down"]["edge"] == 4566.0

    def test_a_nearer_outside_zone_still_wins(self):
        """Containing does not mean automatically nearest."""
        above, below, inside = split(
            Snap(resistance=[z(4605, 4610)], support=[z(4500, 4700)]),
            price=4600.0)
        room = _room(above, below, inside, 4600.0, ATR)
        assert room["up"]["edge"] == 4605.0
        assert not room["up"].get("containing")

    def test_being_inside_is_reported_in_its_own_right(self):
        above, below, inside = split(Snap(support=[z(4590, 4610)]), price=4600.0)
        room = _room(above, below, inside, 4600.0, ATR)
        assert room["inside"] is not None and room["inside_count"] == 1

    def test_position_stays_within_the_range_it_claims(self):
        """`position` is documented as 0 at support, 1 at resistance. Before the
        side fix it returned -6.1 because both bounds were above price."""
        above, below, inside = split(
            Snap(support=[z(4630, 4640), z(4400, 4420)]), price=4597.0)
        room = _room(above, below, inside, 4597.0, ATR)
        assert 0.0 <= room["position"] <= 1.0


class TestRoomArithmetic:
    def _room_at(self, price, res, sup):
        above, below, inside = split(Snap(resistance=res, support=sup), price)
        return _room(above, below, inside, price, ATR)

    def test_span_and_position(self):
        r = self._room_at(4600.0, [z(4650, 4660)], [z(4540, 4550)])
        assert r["span"] == 100.0 and r["span_atr"] == 10.0
        assert r["position"] == 0.5

    def test_position_near_resistance_reads_high(self):
        r = self._room_at(4640.0, [z(4650, 4660)], [z(4540, 4550)])
        assert r["position"] == 0.9

    def test_reward_ratios_are_reciprocal(self):
        r = self._room_at(4600.0, [z(4700, 4710)], [z(4540, 4550)])
        assert r["long_rr"] == 2.0 and r["short_rr"] == 0.5

    def test_a_missing_side_yields_none_not_zero(self):
        r = self._room_at(4600.0, [z(4650, 4660)], [])
        assert r["down"] is None
        assert r["long_rr"] is None and r["span"] is None

    def test_no_zones_at_all_is_survivable(self):
        r = self._room_at(4600.0, [], [])
        assert r["up"] is None and r["down"] is None and r["position"] is None

    def test_the_ladder_carries_more_than_the_first_level(self):
        r = self._room_at(4600.0, [z(4620, 4625), z(4650, 4655), z(4680, 4685)],
                          [z(4550, 4555)])
        assert [x["edge"] for x in r["ups"]] == [4620.0, 4650.0, 4680.0]


class TestBriefFormatting:
    def test_it_never_raises_on_an_error_block(self):
        from backtest.analysis import cockpit_brief

        assert "unavailable" in cockpit_brief.build({"error": "boom"})
        assert "unavailable" in cockpit_brief.build({})

    def test_mixed_ema_alignment_is_named_not_printed_raw(self):
        """`ema_alignment` is a four-state enum; 2 means MIXED and used to
        render as a bare "2" in the timeframe column."""
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4600.0, "as_of_ms": 1787918865373,
            "levels": {"available": False, "why": "none"},
            "alignment": [{"tf": "1h", "ema_alignment": 2, "confirmed_bias": -1,
                           "adx": 21.612755, "percent_b": 0.1070999,
                           "rel_volume": 1.5396583}],
        })
        assert "mixed" in text
        assert "21.6" in text and "21.612755" not in text, "floats are rounded"

    def test_a_stale_level_set_says_so(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4600.0, "as_of_ms": 1787918865373,
            "levels": {"available": True, "age_hours": 14.0, "stale": True,
                       "snapshot_price": 4650.0, "drift": -50.0,
                       "drift_atr": -3.1, "reference_atr": 16.0,
                       "resistance": [], "support": [], "inside": [],
                       "flipped": [], "room": {}},
        })
        assert "STALE" in text


class TestChannelsStayDistinguishable:
    def test_mined_channels_are_marked_in_the_brief(self):
        """Four of the five carry no measured evidence; an unmarked list of
        five names would imply they are alike."""
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4600.0, "as_of_ms": 1787918865373,
            "levels": {"available": False, "why": "none"},
            "channels": [
                {"name": "rule", "measured": True, "fired": False,
                 "blocking": ["adx"], "side": "long"},
                {"name": "signal5", "measured": False, "fired": True,
                 "blocking": [], "side": "short"},
            ],
        })
        assert "rule (measured)" in text
        assert "signal5 (MINED)" in text


class TestMarketClock:
    """Added because the page failed a real question.

    On 2026-08-29 at 03:36 MYT it read "Saturday · ny session · market open" --
    every word true, and together useless: gold shut 2.4 hours later. "Wait or
    go" cannot be answered without knowing that, since a position taken then
    either closes inside two hours or carries the weekend gap.
    """

    def _at(self, y, mo, d, h, mi=0, symbol="MT5:GOLD"):
        """Defaults to the BROKER symbol: the countdown only exists there.

        The Bybit contracts never close -- measured 2026-08-29, all three
        printed 94 hourly bars with real volume inside the gold-shut window --
        so asking them for a close time is the wrong question, and answering it
        was the bug this class grew to cover.
        """
        from datetime import datetime, timezone

        from backtest.analysis.cockpit import market_clock

        return market_clock(int(datetime(y, mo, d, h, mi,
                                         tzinfo=timezone.utc).timestamp() * 1000),
                            symbol)

    def test_friday_evening_is_closing_soon(self):
        c = self._at(2026, 8, 28, 19, 36)      # Sat 03:36 MYT
        assert c["open"] is True
        assert c["closing_soon"] is True
        assert 2.0 < c["hours"] < 2.6

    def test_saturday_is_shut_and_counts_to_the_reopen(self):
        c = self._at(2026, 8, 29, 4)
        assert c["open"] is False
        assert c["closing_soon"] is False, "a shut market is not 'closing soon'"
        assert c["hours"] > 24

    def test_monday_morning_still_finds_the_close(self):
        """The horizon must clear a full open stretch. At 3 days this returned
        None for the whole of Monday and Tuesday -- gold runs Sunday 21:00 UTC
        to Friday 22:00 UTC, so the next close can be ~117 hours out."""
        c = self._at(2026, 8, 31, 1)
        assert c["open"] is True
        assert c["hours"] is not None, "the countdown must not vanish mid-week"
        assert 110 < c["hours"] < 125

    def test_midweek_is_not_flagged(self):
        c = self._at(2026, 9, 2, 7)
        assert c["open"] is True and c["closing_soon"] is False

    def test_the_three_hour_threshold(self):
        assert self._at(2026, 8, 28, 19, 30)["closing_soon"] is True   # 2.5h
        assert self._at(2026, 8, 28, 18, 0)["closing_soon"] is False   # 4h

    def test_it_agrees_with_the_flag_beside_it(self):
        """One definition: the countdown walks `is_gold_session` rather than
        restating its rules, so the two can never disagree."""
        from datetime import datetime, timedelta, timezone

        from backtest.analysis.cockpit import market_clock
        from backtest.engine.features import is_gold_session

        t = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
        for _ in range(60):                    # 30 hours, half-hourly
            ms = int(t.timestamp() * 1000)
            assert market_clock(ms, "MT5:GOLD")["open"] == bool(is_gold_session(ms))
            t += timedelta(minutes=30)


class TestTheVenueIsNotTheBroker:
    """ETHUSDT read "market CLOSED" on a Saturday while trading normally.

    `is_gold_session` describes the MT5 broker's gold CFD hours, and it was
    being applied to whatever symbol was asked for. Measured 2026-08-29: all
    three Bybit symbols -- XAUUSDT included -- printed 94 hourly bars with
    non-zero volume inside that same "shut" window. The venue never closes; the
    broker does.
    """

    def _at(self, symbol, y=2026, mo=8, d=29, h=4):
        from datetime import datetime, timezone

        from backtest.analysis.cockpit import market_clock

        return market_clock(
            int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1000),
            symbol)

    def test_crypto_is_never_closed(self):
        for sym in ("ETHUSDT", "BTCUSDT"):
            c = self._at(sym)
            assert c["open"] is True, f"{sym} trades through the weekend"
            assert c["venue"] == "24/7"
            assert c["closing_soon"] is False

    def test_crypto_gets_no_gold_countdown(self):
        c = self._at("ETHUSDT")
        assert c["hours"] is None and c["changes_ms"] is None

    def test_crypto_is_not_told_about_spot_gold(self):
        assert self._at("ETHUSDT")["cash_gold_open"] is None

    def test_the_gold_perp_stays_open_but_flags_the_cash_session(self):
        """XAUUSDT keeps trading; what it loses is price discovery."""
        c = self._at("XAUUSDT")
        assert c["open"] is True and c["venue"] == "24/7"
        assert c["cash_gold_open"] is False
        assert "thin drift" in c["note"]

    def test_the_gold_perp_says_nothing_special_midweek(self):
        c = self._at("XAUUSDT", d=2, mo=9, h=7)
        assert c["cash_gold_open"] is True and "note" not in c

    def test_only_the_broker_symbol_actually_closes(self):
        assert self._at("MT5:GOLD")["open"] is False
        assert self._at("MT5:GOLD")["venue"] == "broker"

    def test_the_brief_does_not_say_closed_for_a_247_venue(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "ETHUSDT", "spot": 2440.0, "as_of_ms": 1787973540000,
            "levels": {"available": False, "why": "none"},
            "session": {"weekday_myt": "Saturday", "bar_myt": "x",
                        "label": "asia", "gold_market_open": False},
            "clock": {"venue": "24/7", "open": True, "hours": None,
                      "changes_ms": None, "closing_soon": False,
                      "cash_gold_open": None},
        })
        assert "market open 24/7" in text
        assert "CLOSED" not in text

    def test_the_brief_warns_that_gold_drift_is_not_discovery(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4470.0, "as_of_ms": 1787973540000,
            "levels": {"available": False, "why": "none"},
            "session": {"weekday_myt": "Saturday", "bar_myt": "x",
                        "label": "asia", "gold_market_open": False},
            "clock": {"venue": "24/7", "open": True, "hours": None,
                      "changes_ms": None, "closing_soon": False,
                      "cash_gold_open": False},
        })
        assert "spot gold is shut" in text and "price discovery" in text


class TestTheReadIsTiedToItsSymbol:
    def test_the_page_refuses_to_show_a_read_from_another_symbol(self):
        """A read written about ETH must not be measured against gold's price:
        the staleness check is 'how far has price moved since', and across two
        instruments that number is meaningless."""
        import pathlib

        src = (pathlib.Path(__file__).resolve().parents[1]
               / "analysis" / "templates" / "cockpit.html").read_text(encoding="utf-8")
        assert "ai.symbol !== BLOCK.symbol" in src

    def test_the_writer_honours_a_requested_symbol(self):
        import inspect

        from backtest.watch import Watcher

        src = inspect.getsource(Watcher.put_ai_read)
        assert 'parsed.get("symbol")' in src,             "a read must be stamped with the symbol it is about"
        assert "self.symbols" in src, "and only with one this watcher tracks"

    def test_the_brief_states_it(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4470.0, "as_of_ms": 1787952960000,
            "levels": {"available": False, "why": "none"},
            "session": {"weekday_myt": "Saturday", "bar_myt": "2026-08-29 03:15",
                        "label": "ny", "gold_market_open": True},
            "clock": {"open": True, "hours": 2.4, "closing_soon": True,
                      "changes_ms": 1787961720000},
        })
        assert "closes in 2.4h" in text
        assert "CLOSING SOON" in text

    def test_the_brief_says_prices_are_stale_when_shut(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "XAUUSDT", "spot": 4470.0, "as_of_ms": 1787952960000,
            "levels": {"available": False, "why": "none"},
            "session": {"weekday_myt": "Saturday", "bar_myt": "x",
                        "label": "off", "gold_market_open": False},
            "clock": {"open": False, "hours": 41.0, "closing_soon": False,
                      "changes_ms": 1788100000000},
        })
        assert "SHUT" in text and "not a live quote" in text


class TestThePageIsActuallyServed:
    """The cockpit 404'd on first use: the call that writes it was placed in
    `_cycle_backtest`, which a live watcher never enters. Everything else about
    the feature worked -- the API answered 200 -- so the only symptom was a
    missing file, and nothing failed anywhere to say so."""

    def _src(self, name: str) -> str:
        import inspect

        from backtest.watch import Watcher

        return inspect.getsource(getattr(Watcher, name))

    def test_the_live_cycle_writes_it(self):
        assert "write_cockpit_page" in self._src("_cycle_live"), \
            "the live path is the one that runs; a backtest-only call 404s"

    def test_the_backtest_cycle_writes_it_too(self):
        assert "write_cockpit_page" in self._src("_cycle_backtest")

    def test_it_exists_before_the_first_cycle_finishes(self):
        """A slow cycle can take a minute. Opening the second window into a 404
        reads as a broken install rather than a page not generated yet."""
        assert "write_cockpit_page" in self._src("serve")

    def test_the_writer_produces_the_real_template(self, tmp_path):
        from backtest.analysis.report import write_cockpit_page

        out = write_cockpit_page(tmp_path / "cockpit.html")
        text = out.read_text(encoding="utf-8")
        assert "<title>Cockpit</title>" in text
        assert "api/cockpit" in text, "the page must fetch the live block"

    def test_rewriting_is_idempotent(self, tmp_path):
        """It is re-copied every cycle so template edits show up on refresh."""
        from backtest.analysis.report import write_cockpit_page

        a = write_cockpit_page(tmp_path / "c.html").read_text(encoding="utf-8")
        b = write_cockpit_page(tmp_path / "c.html").read_text(encoding="utf-8")
        assert a == b

    def test_the_routes_the_page_calls_all_exist(self):
        """The page fetches these by name; a renamed route would 404 silently
        inside a try/catch and show an empty panel."""
        import inspect

        from backtest.watch import _QuietHandler

        src = inspect.getsource(_QuietHandler._api)
        for route in ("cockpit", "cockpit_brief", "ai_read"):
            assert f'route == "{route}"' in src, f"/api/{route} is not routed"


@pytest.mark.parametrize("price", [4000.0, 4595.61, 5000.0])
def test_split_and_room_never_raise_on_real_shapes(price):
    snap = Snap(resistance=[z(4644.6, 4650.0, 2.8, ("1H swing high",))],
                support=[z(4566.82, 4637.95, 22.2, ("4H swing low", "1H swing low"))])
    above, below, inside = split(snap, price)
    room = _room(above, below, inside, price, ATR)
    assert set(room) >= {"up", "down", "span", "position", "long_rr", "short_rr"}


class TestTheBriefShowsWhatIsBehindTheFirstLevel:
    """The first level alone cannot say whether the stop has a floor under it.

    On BTCUSDT 2026-08-29 the nearest support sat 0.76 ATR away at strength
    19.2 -- a tight, well-defined stop -- and the next zone of any substance was
    22.78 ATR below it. "Risk 0.76 ATR" and "0.76 ATR, then a void" are
    different trades, and only the ladder distinguishes them. The brief printed
    just the first level, so reading it took a separate API call.
    """

    def _text(self):
        from backtest.analysis import cockpit_brief

        def zone(edge, dist_atr, strength):
            return {"edge": edge, "dist": edge, "dist_atr": dist_atr,
                    "strength": strength, "n_sources": 9, "sources": ["1H swing low"],
                    "low": edge, "high": edge, "flipped": False, "was": "support"}

        return cockpit_brief.build({
            "symbol": "BTCUSDT", "spot": 77743.2, "as_of_ms": 1787976540000,
            "levels": {
                "available": True, "age_hours": 1.2, "stale": False,
                "snapshot_price": 77762.5, "drift": -19.3, "drift_atr": -0.04,
                "reference_atr": 526.454, "resistance": [], "support": [],
                "inside": [], "flipped": [],
                "room": {
                    "up": zone(80124.0, 4.52, 19.236),
                    "down": zone(77376.3, 0.7, 19.236),
                    "ups": [zone(80124.0, 4.52, 19.236), zone(81260.0, 6.62, 1.242)],
                    "downs": [zone(77376.3, 0.7, 19.236),
                              zone(76853.8, 1.75, 5.196),
                              zone(75550.0, 4.23, 1.104),
                              zone(65784.7, 22.78, 24.687)],
                    "span": 2747.7, "span_atr": 5.22, "position": 0.134,
                    "long_rr": 6.49, "short_rr": 0.15,
                    "inside": None, "inside_count": 0,
                },
            },
        })

    def test_the_void_under_the_stop_is_visible(self):
        text = self._text()
        assert "behind it, below" in text
        assert "22.78 ATR" in text, "the next real floor must be stated"
        assert "65784.7" in text

    def test_the_weak_levels_in_between_are_shown_with_their_strength(self):
        text = self._text()
        assert "76853.8" in text and "str 5.196" in text
        assert "75550.0" in text and "str 1.104" in text

    def test_the_upside_ladder_is_shown_too(self):
        assert "behind it, above" in self._text()

    def test_it_is_capped_so_the_message_stays_readable(self):
        """At most three behind each side -- this is pasted into a chat."""
        text = self._text()
        below = [ln for ln in text.splitlines() if "behind it, below" in ln][0]
        assert below.count("ATR") <= 3

    def test_a_single_level_produces_no_ladder_line(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build({
            "symbol": "BTCUSDT", "spot": 1.0, "as_of_ms": 1787976540000,
            "levels": {"available": True, "age_hours": 1.0, "stale": False,
                       "snapshot_price": 1.0, "drift": 0.0, "drift_atr": 0.0,
                       "reference_atr": 1.0, "resistance": [], "support": [],
                       "inside": [], "flipped": [],
                       "room": {"up": None, "down": None, "ups": [], "downs": [],
                                "span": None, "span_atr": None, "position": None,
                                "long_rr": None, "short_rr": None,
                                "inside": None, "inside_count": 0}},
        })
        assert "behind it" not in text


class TestSessionMapBrief:
    """The cockpit's pasteable text carries the intraday framework's numbers.

    The brief is what goes into a chat window, so the section must be legible
    on its own: the levels, the state, and the day's events, with no chart
    next to them to explain what any of it means.
    """

    def _block(self, **over):
        block = {
            "symbol": "XAUUSDT", "spot": 4600.0,
            "as_of_ms": 1_790_064_600_000,
            "levels": {"available": False, "why": "test"},
            "sessions": {
                "schema": 1, "day_myt": "2026-09-22", "phase": "europe",
                "weekend": False,
                "windows_myt": {"asia": "07:00-15:00",
                                "europe": "15:00-20:00", "us": "20:00-05:00"},
                "levels": {"asia_high": 4600.55, "asia_low": 4599.45,
                           "asia_range": 1.1, "asia_done": True,
                           "prev_day_high": 4650.0, "prev_day_low": 4580.0,
                           "prev_us_high": 4640.0, "prev_us_low": 4590.0,
                           "day_open": 4598.0},
                "state": "swept_high",
                "us": {"verdict": "confirms", "europe_close": 4601.0,
                       "move": 8.0, "threshold": 0.17},
                "events": [{"ts_ms": 1_790_064_000_000,
                            "ts_myt": "2026-09-22 16:00",
                            "type": "sweep_high", "price": 4620.0,
                            "level": 4600.55, "vol_ok": None}],
            },
        }
        block.update(over)
        return block

    def test_the_section_renders_with_levels_state_and_events(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build(self._block())
        assert "SESSION MAP" in text
        assert "Asia 4600.55 / 4599.45" in text
        assert "state: swept_high" in text
        assert "sweep_high" in text and "4620" in text
        assert "prev US 4640.0 / 4590.0" in text
        assert "US read: confirms" in text

    def test_no_map_no_section(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build(self._block(sessions=None))
        assert "SESSION MAP" not in text

    def test_an_error_map_is_not_rendered_as_numbers(self):
        from backtest.analysis import cockpit_brief

        text = cockpit_brief.build(
            self._block(sessions={"schema": 1, "error": "no 1m bars"}))
        assert "SESSION MAP" not in text

    def test_a_map_without_asia_bars_says_so(self):
        from backtest.analysis import cockpit_brief

        block = self._block()
        block["sessions"]["levels"] = {"asia_high": None, "asia_low": None}
        block["sessions"]["state"] = "waiting_asia"
        text = cockpit_brief.build(block)
        assert "Asia range" in text and "no bars yet today" in text

    def test_the_day_bias_renders_with_its_basis(self):
        from backtest.analysis import cockpit_brief

        block = self._block()
        block["sessions"]["bias"] = {
            "phase": "us", "bias": "long", "ready": True,
            "verdict_myt": "20:30",
            "basis": ["Asia 4599.45–4600.55 (R=1.1)",
                      "16:00 break_high (volume confirmed)",
                      "US confirmed Europe's direction — 美盘定方向"],
            "close_r": 0.42,
        }
        text = cockpit_brief.build(block)
        assert "看多 LONG" in text
        assert "当日判定" in text
        assert "美盘定方向" in text

    def test_an_unready_bias_says_what_it_waits_for(self):
        from backtest.analysis import cockpit_brief

        block = self._block()
        block["sessions"]["bias"] = {
            "phase": "us", "bias": "range", "ready": False,
            "verdict_myt": "20:30", "basis": ["US verdict at 20:30"],
            "close_r": 0.0,
        }
        text = cockpit_brief.build(block)
        assert "US verdict at 20:30" in text
