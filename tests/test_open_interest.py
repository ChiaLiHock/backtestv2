"""Open interest: alignment onto bars, the resume point, and what is NOT drawn.

Open interest is the one series in this project that cannot be recomputed. A
candle can always be re-fetched; a *past* OI reading exists at the source for
about 170 days and then it is gone for good. That asymmetry sets what these
tests guard:

* **the record must never silently lose a stretch** -- the old fixed-lookback
  sync turned any outage longer than the window into a permanent hole that
  nothing re-requested and nothing reported;
* **a bar's OI is the LAST point inside it** -- OI is a level, not a flow, so
  summing invents size and averaging smears the moment it turned;
* **an unsampled bar must not be given a value** -- forward-filling a 15m
  reading across 5m bars draws a flat line indistinguishable from genuinely
  flat OI, which is a fabricated observation rather than an admitted gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.analysis.report import _attach_oi, oi_on_bars
from backtest.data.db import INTERVAL_MS, Database, MetaRepository
from backtest.engine.risk_feed import OI_INTERVAL, OI_RETENTION_DAYS

M15 = INTERVAL_MS["15m"]
H1 = INTERVAL_MS["1h"]
M5 = INTERVAL_MS["5m"]
T0 = 1_787_000_000_000 // M15 * M15          # a 15m boundary


@pytest.fixture()
def db(tmp_path):
    with Database(tmp_path / "t.db") as d:
        yield d


def _oi(db, symbol="XAUUSDT", interval=OI_INTERVAL, points=()):
    MetaRepository(db).upsert_open_interest(symbol, interval, points)


# ---------------------------------------------------------------------------
# oi_on_bars -- the alignment shared by the payload build and the live tick
# ---------------------------------------------------------------------------


class TestAlignment:
    def test_one_oi_point_per_bar_maps_one_to_one(self):
        opens = np.array([T0, T0 + M15, T0 + 2 * M15], dtype="int64")
        ts = opens.copy()
        vals = np.array([100.0, 110.0, 120.0])
        assert oi_on_bars(opens, M15, ts, vals) == [100.0, 110.0, 120.0]

    def test_hourly_bar_takes_the_LAST_point_inside_it(self):
        """Not the first, not the mean, not the sum.

        OI is the number of contracts outstanding. The hour's OI is what it was
        at the end of the hour; a mean would report a level that was never true
        at any instant, and would blur the bar where OI actually turned.
        """
        opens = np.array([T0], dtype="int64")
        ts = np.array([T0, T0 + M15, T0 + 2 * M15, T0 + 3 * M15], dtype="int64")
        vals = np.array([100.0, 200.0, 300.0, 400.0])
        got = oi_on_bars(opens, H1, ts, vals)
        assert got == [400.0]
        assert got != [100.0], "took the first point in the bar"
        assert got != [250.0], "averaged the bar, smearing where OI turned"
        assert got != [1000.0], "summed a level, inventing size"

    def test_bars_finer_than_the_oi_grid_get_None_not_a_forward_fill(self):
        """A 5m chart has no 5m OI, and must say so rather than repeat 15m."""
        opens = np.array([T0, T0 + M5, T0 + 2 * M5], dtype="int64")
        ts = np.array([T0], dtype="int64")
        vals = np.array([100.0])
        assert oi_on_bars(opens, M5, ts, vals) is None

    def test_a_bar_with_no_point_stays_None(self):
        """The watcher was off for that bar. A gap must read as a gap."""
        opens = np.array([T0, T0 + M15, T0 + 2 * M15], dtype="int64")
        ts = np.array([T0, T0 + 2 * M15], dtype="int64")
        vals = np.array([100.0, 300.0])
        assert oi_on_bars(opens, M15, ts, vals) == [100.0, None, 300.0]

    def test_a_point_before_the_first_bar_is_dropped(self):
        opens = np.array([T0, T0 + M15], dtype="int64")
        ts = np.array([T0 - M15, T0], dtype="int64")
        vals = np.array([1.0, 100.0])
        assert oi_on_bars(opens, M15, ts, vals) == [100.0, None]

    def test_a_point_after_the_last_bar_closes_is_not_stuck_on_that_bar(self):
        """searchsorted clamps to the last index; without the close check the
        newest OI would be attributed to a bar it happened after."""
        opens = np.array([T0, T0 + M15], dtype="int64")
        ts = np.array([T0, T0 + M15, T0 + 2 * M15], dtype="int64")
        vals = np.array([100.0, 200.0, 999.0])
        assert oi_on_bars(opens, M15, ts, vals) == [100.0, 200.0]

    def test_a_gappy_bar_grid_is_handled_like_a_uniform_one(self):
        """`_series_block` sends explicit times when bars are not evenly spaced;
        the alignment must not assume the uniform case."""
        opens = np.array([T0, T0 + 4 * M15], dtype="int64")   # a missing stretch
        ts = np.array([T0, T0 + M15, T0 + 4 * M15], dtype="int64")
        vals = np.array([100.0, 150.0, 500.0])
        # T0+M15 is past the first 15m bar's close and before the next bar's
        # open, so it belongs to no drawn bar rather than being carried forward.
        assert oi_on_bars(opens, M15, ts, vals) == [100.0, 500.0]

    def test_empty_bar_array(self):
        assert oi_on_bars(np.array([], dtype="int64"), M15,
                          np.array([T0], dtype="int64"), np.array([1.0])) is None


# ---------------------------------------------------------------------------
# The resume point -- the reason an outage no longer scars the record
# ---------------------------------------------------------------------------


class TestResumePoint:
    def test_newest_is_None_when_nothing_is_stored(self, db):
        assert MetaRepository(db).newest_open_interest("XAUUSDT", OI_INTERVAL) is None

    def test_newest_is_the_max_timestamp(self, db):
        _oi(db, points=[(T0, 1.0), (T0 + 2 * M15, 3.0), (T0 + M15, 2.0)])
        assert MetaRepository(db).newest_open_interest(
            "XAUUSDT", OI_INTERVAL) == T0 + 2 * M15

    def test_newest_is_per_symbol_and_per_interval(self, db):
        _oi(db, "XAUUSDT", OI_INTERVAL, [(T0, 1.0)])
        _oi(db, "BTCUSDT", OI_INTERVAL, [(T0 + 5 * M15, 1.0)])
        _oi(db, "XAUUSDT", "1h", [(T0 + 9 * M15, 1.0)])
        m = MetaRepository(db)
        assert m.newest_open_interest("XAUUSDT", OI_INTERVAL) == T0
        assert m.newest_open_interest("BTCUSDT", OI_INTERVAL) == T0 + 5 * M15
        assert m.newest_open_interest("XAUUSDT", "1h") == T0 + 9 * M15

    def test_a_long_outage_is_requested_in_full(self, db):
        """The property the old fixed 200-bar window failed.

        Off for three days, the watcher asked only for the last 50 hours: the
        missing middle was never requested again and never reported, so the hole
        became permanent. Resuming from the newest stored point covers it.
        """
        outage_ms = 3 * 86_400_000
        now = T0 + outage_ms
        _oi(db, points=[(T0, 1.0)])
        last = MetaRepository(db).newest_open_interest("XAUUSDT", OI_INTERVAL)
        start = max(now - OI_RETENTION_DAYS * 86_400_000, last - M15)

        assert start <= T0, "the resume point must not skip past stored data"
        assert now - start >= outage_ms, "the whole outage must be re-requested"
        assert now - start > 200 * M15, "a fixed 200-bar window would have missed it"

    def test_the_request_floors_at_source_retention(self, db):
        """Beyond retention the data does not exist at the source, so asking
        further back only burns pages."""
        now = T0
        floor = now - OI_RETENTION_DAYS * 86_400_000
        ancient = now - 5 * 365 * 86_400_000
        _oi(db, points=[(ancient, 1.0)])
        last = MetaRepository(db).newest_open_interest("XAUUSDT", OI_INTERVAL)
        assert max(floor, last - M15) == floor


class TestCoverage:
    def test_reports_rows_and_span_per_series(self, db):
        _oi(db, "XAUUSDT", OI_INTERVAL, [(T0, 1.0), (T0 + M15, 2.0)])
        _oi(db, "BTCUSDT", OI_INTERVAL, [(T0, 9.0)])
        rows = MetaRepository(db).open_interest_coverage(OI_INTERVAL)
        assert rows == [
            ("BTCUSDT", OI_INTERVAL, 1, T0, T0),
            ("XAUUSDT", OI_INTERVAL, 2, T0, T0 + M15),
        ]

    def test_an_upsert_never_drops_history(self, db):
        """The table is append-only in practice: re-syncing an overlapping
        window must correct values without shortening the record."""
        m = MetaRepository(db)
        _oi(db, points=[(T0, 1.0), (T0 + M15, 2.0)])
        _oi(db, points=[(T0 + M15, 22.0), (T0 + 2 * M15, 3.0)])
        n, lo, hi = m.open_interest_coverage(OI_INTERVAL)[0][2:]
        assert (n, lo, hi) == (3, T0, T0 + 2 * M15)
        got = m.load_open_interest("XAUUSDT", OI_INTERVAL, T0, T0 + 2 * M15)
        assert list(got["oi"]) == [1.0, 22.0, 3.0]


# ---------------------------------------------------------------------------
# _attach_oi -- what the page actually receives
# ---------------------------------------------------------------------------


class TestAttach:
    def _series(self):
        return {
            "5m": {"t0": T0, "n": 6, "step": M5},
            "15m": {"t0": T0, "n": 2, "step": M15},
            "1h": {"t0": T0, "n": 1, "step": H1},
        }

    def test_each_timeframe_gets_its_own_grid(self, db):
        _oi(db, points=[(T0 + i * M15, 100.0 + i) for i in range(4)])
        s = self._series()
        _attach_oi(db, "XAUUSDT", s)
        assert s["5m"]["oi"] is None                    # finer than the OI grid
        assert s["15m"]["oi"] == [100.0, 101.0]
        assert s["1h"]["oi"] == [103.0]                 # last point in the hour

    def test_no_stored_oi_yields_None_everywhere_rather_than_zeros(self, db):
        s = self._series()
        _attach_oi(db, "XAUUSDT", s)
        assert all(blk["oi"] is None for blk in s.values())

    def test_another_symbols_oi_is_not_borrowed(self, db):
        _oi(db, "BTCUSDT", OI_INTERVAL, [(T0, 100.0)])
        s = self._series()
        _attach_oi(db, "XAUUSDT", s)
        assert s["15m"]["oi"] is None

    def test_empty_series_is_a_no_op(self, db):
        s = {}
        _attach_oi(db, "XAUUSDT", s)
        assert s == {}


# ---------------------------------------------------------------------------
# The AI brief -- OI must resolve on every anchor, or say why not
# ---------------------------------------------------------------------------


class TestBriefReadsStoredOI:
    """The regression: `context.gather` asked for OI at the ANCHOR's interval.

    Only `OI_INTERVAL` is ever written, so any anchor other than 15m read back
    nothing and the blob reported "fewer than 2 stored OI points" -- while
    16,320 points sat in the table. A *wrong stated reason* is worse than a
    missing section: it tells the reader to stop looking.
    """

    def _gather(self, symbol, anchor):
        from backtest.analysis import context
        from backtest.data.db import CandleRepository

        with Database() as db:
            if len(CandleRepository(db).load_tail(symbol, anchor, 60)) < 60:
                pytest.skip(f"needs {symbol} {anchor} synced")
            if not MetaRepository(db).newest_open_interest(symbol, OI_INTERVAL):
                pytest.skip(f"needs {symbol} open interest synced")
            return context.gather(db, symbol, anchor)

    @pytest.mark.parametrize("anchor", ["15m", "30m", "1h", "4h"])
    def test_every_anchor_at_or_above_the_grid_resolves(self, anchor):
        oi = self._gather("XAUUSDT", anchor)["open_interest"]
        assert oi.get("available") is not False, oi.get("why")
        assert oi["interval"] == anchor, "the quadrant is read on the anchor's bars"
        assert oi["now"] and oi["prev"]

    def test_a_finer_anchor_states_the_real_reason_not_a_false_one(self):
        oi = self._gather("XAUUSDT", "5m")["open_interest"]
        assert oi["available"] is False
        assert "fewer than 2 stored" not in oi["why"], "the false reason is back"
        assert OI_INTERVAL in oi["why"] and "finer" in oi["why"]
        assert oi["points_total"] > 2, "it must admit the points DO exist"

    def test_the_span_of_the_record_is_reported(self):
        """What the record covers is the point of keeping it."""
        oi = self._gather("XAUUSDT", "15m")["open_interest"]
        assert oi["points_total"] > 0
        assert oi["grid"] == OI_INTERVAL
        assert len(oi["since_myt"]) == 10          # YYYY-MM-DD

    def test_delta_pairs_the_same_two_bars_on_both_axes(self):
        """A hole in the OI record must move both axes together, never pair a
        fresh OI reading against a stale price."""
        oi = self._gather("XAUUSDT", "1h")["open_interest"]
        assert oi["delta"] == round(oi["now"] - oi["prev"], 2)
