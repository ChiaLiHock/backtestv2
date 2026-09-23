"""Session liquidity map: levels, sweep/break/reclaim, the US read.

The framework being mechanised is the owner's own (XAUUSD 黄金日内交易口诀与
框架): 亚盘定范围，欧盘扫流动，美盘定方向. The tests pin the properties that
make the map trustworthy as an observation — exact windows, exact sweep vs
break semantics, and the no-lookahead split the rest of this repo treats as
the one test that catches the bug a backtest cannot.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import session_map as sm

DAY = 86_400_000
MIN = 60_000
H = 3_600_000

# 2026-09-20 is a Sunday. SUN = Sunday 00:00 UTC = Sunday 08:00 MYT, so the
# Monday-MYT day starts at SUN + 16h (Sunday 16:00 UTC == Monday 00:00 MYT).
SUN = 1_789_862_400_000
MON_MYT0 = SUN + 16 * H          # Monday 2026-09-21 00:00 MYT
TUE_MYT0 = MON_MYT0 + DAY


def minute_bars(start_ms: int, n: int, base: float = 4600.0,
                amp: float = 1.0, vol: float = 100.0) -> pd.DataFrame:
    """A quiet sinusoid day: oscillates inside a known range, constant volume."""
    t = np.arange(n, dtype="int64")
    open_time = start_ms + t * MIN
    mid = base + amp * np.sin(np.arange(n) / 90.0)
    o = mid + 0.05 * np.sin(np.arange(n) / 37.0)
    c = mid - 0.05 * np.sin(np.arange(n) / 37.0)
    h = np.maximum(o, c) + 0.2
    l = np.minimum(o, c) - 0.2
    return pd.DataFrame({
        "open_time": open_time, "open": o, "high": h, "low": l,
        "close": c, "volume": np.full(n, vol), "turnover": np.full(n, 0.0),
    })


def at(df: pd.DataFrame, myt_day0: int, hhmm: str) -> int:
    """Minute-offset of a MYT wall-clock time within the frame, as an index."""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    target = myt_day0 + h * H + m * MIN
    return int((target - int(df["open_time"].iloc[0])) // MIN)


def poke(df: pd.DataFrame, myt_day0: int, hhmm: str, span: int = 1,
         **kw) -> pd.DataFrame:
    """Overwrite ``span`` minute bars (open/high/low/close/vol) from a MYT time.

    A 5m bar's close is its LAST minute's close and its volume the sum of all
    five, so a test that means "the 16:00 5m bar closed beyond" has to write
    the close into 16:04, or lift all five minutes for the volume test. Giving
    the helper a span keeps each case honest about which it means. ``vol`` is
    the alias for ``volume`` — the real column name, kept short at the calls.
    """
    df = df.copy()
    if "vol" in kw:
        kw["volume"] = kw.pop("vol")
    i = at(df, myt_day0, hhmm)
    for k in range(span):
        for key in ("open", "high", "low", "close", "volume"):
            if kw.get(key) is not None:
                df.loc[df.index[i + k], key] = float(kw[key])
    return df


def quiet(df: pd.DataFrame, myt_day0: int, hhmm: str, span: int = 200,
          asia_high: float = 4600.55, asia_low: float = 4599.45) -> pd.DataFrame:
    """Flatten the rest of the day inside the range.

    The sinusoid that builds the range keeps oscillating after 15:00, and a
    later crest can poke its own Asia high by a hundredth — a real behaviour,
    but one these tests never mean. Everything after the event under test is
    pinned inside the range so an assertion on "what happened" is about the
    poked bar alone.
    """
    mid = (asia_high + asia_low) / 2.0
    return poke(df, myt_day0, hhmm, span=span, open=mid, close=mid,
                high=(asia_high + mid) / 2.0, low=(asia_low + mid) / 2.0)


class TestWindows:
    """The single definition: MYT hours, Europe opens at 15:00, US at 20:00."""

    def test_phase_boundaries(self):
        t = MON_MYT0
        assert sm.phase(t + 6 * H + 59 * MIN) == "off"
        assert sm.phase(t + 7 * H) == "asia"
        assert sm.phase(t + 14 * H + 59 * MIN) == "asia"
        assert sm.phase(t + 15 * H) == "europe"
        assert sm.phase(t + 19 * H + 59 * MIN) == "europe"
        assert sm.phase(t + 20 * H) == "us"
        assert sm.phase(t + 23 * H) == "us"
        assert sm.phase(t + DAY + 4 * H) == "us"          # 04:00 next day
        assert sm.phase(t + DAY + 5 * H) == "off"

    def test_myt_day_midnight(self):
        assert sm.myt_day_ms(MON_MYT0) == MON_MYT0
        assert sm.myt_day_ms(MON_MYT0 + DAY - MIN) == MON_MYT0
        assert sm.myt_day_ms(MON_MYT0 + DAY) == MON_MYT0 + DAY


class TestLevels:
    """亚盘画高低：the range is the Asia window, nothing else."""

    def test_asia_range_is_the_asia_window_only(self):
        df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=1.0)
        df = poke(df, TUE_MYT0, "0600", high=4700.0)   # Tue 06:00 — outside
        df = poke(df, TUE_MYT0, "1000", high=4650.0, low=4550.0)  # inside
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H)
        lv = m["levels"]
        assert lv["asia_high"] == 4650.0, "the 06:00 spike is outside the window"
        assert lv["asia_low"] == 4550.0
        assert lv["asia_done"] is True
        assert lv["asia_range"] == 100.0

    def test_asia_partial_before_fifteen_hundred(self):
        df = minute_bars(SUN, 2 * 1440, base=4600.0)
        m = sm.build_map(df, as_of_ms=MON_MYT0 + 10 * H)
        assert m["levels"]["asia_done"] is False
        assert m["state"] == "in_range"

    def test_prev_day_and_prev_us_levels(self):
        df = minute_bars(SUN, 3 * 1440, base=4600.0)
        df = poke(df, MON_MYT0, "2300", high=4690.0)   # Mon 23:00 — US window
        df = poke(df, MON_MYT0, "1200", high=4660.0)   # Mon 12:00 — day only
        df = poke(df, MON_MYT0, "1205", low=4510.0)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 12 * H)
        lv = m["levels"]
        assert lv["prev_day_high"] == 4690.0
        assert lv["prev_day_low"] == 4510.0
        # prev US window = Mon 20:00 -> Tue 05:00; the 12:00 low is not in it
        assert lv["prev_us_high"] == 4690.0
        assert lv["prev_us_low"] > 4510.0
        assert lv["day_open"] is not None

    def test_waiting_asia_when_no_asia_bars_yet(self):
        df = minute_bars(SUN, 1440)                     # ends Mon 08:00 MYT
        m = sm.build_map(df, as_of_ms=MON_MYT0 + 3 * H)  # Mon 03:00 MYT
        assert m["levels"]["asia_high"] is None
        assert m["state"] == "waiting_asia"

    def test_empty_and_none_inputs(self):
        assert "error" in sm.build_map(pd.DataFrame(), as_of_ms=MON_MYT0)
        assert "error" in sm.build_map(None, as_of_ms=MON_MYT0)


class TestSweepBreakReclaim:
    """欧盘扫亚洲：a poke that returns is a sweep; one that holds is a break."""

    def _days(self):
        """Mon-Wed minute data; Tuesday's Asia range is ~4599.5-4600.6."""
        return minute_bars(SUN, 3 * 1440, base=4600.0, amp=0.3)

    def test_single_bar_wick_poke_is_a_sweep(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", high=4620.0, low=4600.0, close=4600.0)
        df = quiet(df, TUE_MYT0, "1605")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        ev = [e for e in m["events"] if e["type"] == "sweep_high"]
        assert len(ev) == 1
        assert ev[0]["price"] == 4620.0
        assert m["state"] == "swept_high"

    def test_one_close_beyond_then_back_inside_is_a_sweep(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", span=5, high=4625.0, close=4610.0)
        df = poke(df, TUE_MYT0, "1605", span=5, high=4611.0, close=4600.0)
        df = quiet(df, TUE_MYT0, "1610")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        types = [e["type"] for e in m["events"]]
        assert "sweep_high" in types and "break_high" not in types

    def test_two_consecutive_closes_beyond_is_a_break(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", span=5, high=4630.0, close=4610.0,
                  vol=1000.0)
        df = poke(df, TUE_MYT0, "1605", span=5, high=4632.0, close=4612.0,
                  vol=1000.0)
        # as-of 16:11: the confirming bucket closed at 16:10, the 16:10
        # bucket has not — so no reclaim can exist yet.
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H + 11 * MIN)
        ev = [e for e in m["events"] if e["type"] == "break_high"]
        assert len(ev) == 1
        assert ev[0]["vol_ok"] is True
        assert m["state"] == "broke_high"

    def test_low_volume_break_is_flagged_not_hidden(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", span=5, high=4630.0, close=4610.0)
        df = poke(df, TUE_MYT0, "1605", span=5, high=4632.0, close=4612.0)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H + 11 * MIN)
        ev = next(e for e in m["events"] if e["type"] == "break_high")
        assert ev["vol_ok"] is False, "a quiet break is still a break, said so"

    def test_break_then_close_back_inside_is_a_reclaim(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", span=5, high=4630.0, close=4610.0,
                  vol=1000.0)
        df = poke(df, TUE_MYT0, "1605", span=5, high=4632.0, close=4612.0,
                  vol=1000.0)
        df = poke(df, TUE_MYT0, "1610", span=5, high=4601.0, close=4600.0)
        df = quiet(df, TUE_MYT0, "1615")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        types = [e["type"] for e in m["events"]]
        assert "break_high" in types and "reclaim_high" in types
        assert m["state"] == "in_range", "a reclaimed break leaves no break open"

    def test_sweep_low_mirrors_sweep_high(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", high=4600.5, low=4580.0, close=4600.2)
        df = quiet(df, TUE_MYT0, "1605")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        ev = [e for e in m["events"] if e["type"] == "sweep_low"]
        assert len(ev) == 1
        assert ev[0]["price"] == 4580.0
        assert m["state"] == "swept_low"

    def test_adjacent_bucket_pokes_are_one_event_not_five(self):
        df = self._days()
        df = poke(df, TUE_MYT0, "1600", high=4620.0, close=4600.0)
        df = poke(df, TUE_MYT0, "1605", high=4620.0, close=4600.0)
        df = quiet(df, TUE_MYT0, "1610")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        ev = [e for e in m["events"] if e["type"] == "sweep_high"]
        assert len(ev) == 1, "a run of pokes is one visit to the level"

    def test_no_events_from_inside_the_asia_window(self):
        """The Asia session cannot sweep its own range: it IS the range."""
        df = self._days()
        df = poke(df, TUE_MYT0, "0900", high=4650.0)
        df = quiet(df, TUE_MYT0, "1500")
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 18 * H)
        assert m["events"] == [], "the 09:00 spike only widened the range"
        assert m["levels"]["asia_high"] == 4650.0


class TestUsRead:
    """美盘定方向：confirm or reverse Europe's move, after 20:00 only."""

    def test_us_block_absent_before_twenty_hundred(self):
        df = minute_bars(SUN, 3 * 1440)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 17 * H)
        assert m["us"] is None

    def test_us_block_present_after_twenty_hundred(self):
        df = minute_bars(SUN, 3 * 1440)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 21 * H)
        assert m["us"] is not None
        assert m["us"]["verdict"] in ("confirms", "reverses", "flat")


class TestNoLookahead:
    """The forming minute and everything after ``as_of_ms`` do not exist."""

    def test_truncation_invariance(self):
        """Reading the full frame as-of T equals reading the bars closed by T."""
        rng = np.random.default_rng(7)
        n = 3 * 1440
        walk = 4600.0 + np.cumsum(rng.normal(0, 0.3, n))
        df = pd.DataFrame({
            "open_time": SUN + np.arange(n, dtype="int64") * MIN,
            "open": walk, "high": walk + 0.5, "low": walk - 0.5,
            "close": walk, "volume": rng.uniform(50, 150, n),
            "turnover": np.zeros(n),
        })
        t = TUE_MYT0 + 17 * H                    # Tuesday afternoon MYT
        i = int((t - SUN) // MIN)
        full = sm.build_map(df, as_of_ms=t)
        cut = sm.build_map(df.iloc[:i].copy(), as_of_ms=t)
        assert full["levels"] == cut["levels"]
        assert full["state"] == cut["state"]
        assert [e["ts_ms"] for e in full["events"]] == \
               [e["ts_ms"] for e in cut["events"]]

    def test_events_only_from_5m_bars_closed_by_the_as_of(self):
        df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=0.3)
        df = poke(df, TUE_MYT0, "1600", high=4625.0, low=4600.0, close=4600.0)
        # The 16:00 5m bar closes at 16:05; by 16:06 it is confirmed.
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H + 6 * MIN)
        assert m["events"], "the 16:00 bucket is closed and confirmed by 16:06"
        assert all(e["ts_ms"] <= TUE_MYT0 + 16 * H for e in m["events"])

    def test_a_poke_bar_still_forming_produces_no_event(self):
        df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=0.3)
        df = poke(df, TUE_MYT0, "1600", high=4625.0, low=4600.0, close=4600.0)
        # as-of 16:00:01 — the 16:00 bar closes at 16:01 and is not closed yet
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H + 1)
        assert m["events"] == []

    def test_as_of_defaults_to_all_bars_closed(self):
        df = minute_bars(SUN, 1440)
        m = sm.build_map(df)
        assert m["as_of_ms"] == int(df["open_time"].iloc[-1]) + MIN


class TestBiasRead:
    """亚盘定范围 → 欧盘表态 → 美盘 20:30 判定, each with its stated basis.

    The bias is only ever read from facts the map already holds — the tests
    pin the mapping from those facts to the verdict, and that the verdict
    genuinely waits for its time (20:30) rather than being guessed early.
    """

    def _day(self):
        """A quiet Mon-Wed frame; Tuesday's Asia range is ~4594.5-4605.5."""
        df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=5.0)
        df = quiet(df, MON_MYT0, "1500", span=540)
        df = quiet(df, TUE_MYT0, "1500", span=540)
        return df

    def _mid(self, df):
        """The FULL-day Asia mid — a partial-window map has a different one,
        and judging a Europe statement against a mid that moves with the
        clock is exactly the bug these tests exist to avoid."""
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H)
        return (m["levels"]["asia_high"] + m["levels"]["asia_low"]) / 2.0

    def test_asia_is_range_by_definition(self):
        m = sm.build_map(self._day(), as_of_ms=TUE_MYT0 + 10 * H)
        b = m["bias"]
        assert b["bias"] == "range" and b["ready"] is False
        assert any("not called in Asia" in s for s in b["basis"])

    def test_europe_break_statement_is_long(self):
        df = self._day()
        df = poke(df, TUE_MYT0, "1600", span=5, high=4607.2, low=4605.6,
                  close=4607.0, vol=1000.0)
        df = poke(df, TUE_MYT0, "1605", span=5, high=4607.4, low=4605.6,
                  close=4607.5, vol=1000.0)
        # The aftermath stays ABOVE the level: a break that is reclaimed is
        # not a break holding, and the statement must say long only while it
        # actually holds.
        df = poke(df, TUE_MYT0, "1610", span=180, open=4607.5, close=4607.5,
                  high=4608.0, low=4606.0)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 17 * H)
        b = m["bias"]
        assert m["state"] == "broke_high", m["state"]
        assert b["bias"] == "long" and b["ready"] is False
        assert any("break holding above" in s for s in b["basis"])

    def test_europe_sweep_statement_follows_the_mid(self):
        df = self._day()
        mid = self._mid(df)
        df = poke(df, TUE_MYT0, "1600", high=4620.0, close=mid - 3.0)
        # Aftermath pinned below the mid, inside the range.
        df = poke(df, TUE_MYT0, "1605", span=180, open=mid - 3.0,
                  close=mid - 3.0, high=mid - 2.5, low=mid - 3.5)
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 17 * H)
        assert m["bias"]["bias"] == "short"
        assert any("under the mid" in s for s in m["bias"]["basis"])

    def test_us_verdict_waits_for_half_past(self):
        m = sm.build_map(self._day(), as_of_ms=TUE_MYT0 + 20 * H + 10 * MIN)
        b = m["bias"]
        assert b["ready"] is False
        assert b["bias"] == "range", "quiet day carries no statement"
        assert any("20:30" in s for s in b["basis"])

    def _us_day(self, europe_close, us_close):
        """Europe pinned to a close, US pinned to a close, both inside data."""
        df = self._day()
        df = poke(df, TUE_MYT0, "1930", span=30, open=europe_close,
                  close=europe_close, high=europe_close + 0.5,
                  low=europe_close - 0.5)
        df = poke(df, TUE_MYT0, "2030", span=30, open=us_close, close=us_close,
                  high=us_close + 0.5, low=us_close - 0.5)
        return df

    def test_us_confirms_europe_long(self):
        mid = self._mid(self._day())
        eu = mid + 4.0                      # ~ +0.36R, past the 0.15R gate
        df = self._us_day(eu, eu + 4.0)     # US extends further up
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 21 * H)
        b = m["bias"]
        assert b["ready"] is True and b["bias"] == "long"
        assert any("confirmed" in s for s in b["basis"])

    def test_us_reversal_not_through_mid_is_range(self):
        mid = self._mid(self._day())
        eu = mid + 5.0
        df = self._us_day(eu, eu - 4.0)     # reverses, but stays above mid
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 21 * H)
        b = m["bias"]
        assert b["ready"] is True and b["bias"] == "range"
        assert any("two-sided" in s for s in b["basis"])

    def test_us_reversal_through_the_mid_flips_the_day(self):
        mid = self._mid(self._day())
        eu = mid + 5.0
        df = self._us_day(eu, mid - 6.0)    # reversal clears the mid
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 21 * H)
        b = m["bias"]
        assert b["ready"] is True and b["bias"] == "short"
        assert any("flipped" in s for s in b["basis"])

    def test_flat_europe_is_a_range_day(self):
        mid = self._mid(self._day())
        df = self._us_day(mid + 0.5, mid - 0.5)   # < 0.15R either way
        m = sm.build_map(df, as_of_ms=TUE_MYT0 + 21 * H)
        b = m["bias"]
        assert b["ready"] is True and b["bias"] == "range"
        assert any("never travelled" in s for s in b["basis"])

    def test_waiting_asia_has_no_read(self):
        df = minute_bars(SUN, 1440)
        m = sm.build_map(df, as_of_ms=MON_MYT0 + 3 * H)
        assert m["bias"]["bias"] == "undecided"


class TestFold5m:
    def test_fold_is_exact_ohlcv(self):
        df = minute_bars(SUN, 30, base=4600.0)
        f = sm._fold_5m(df)
        assert len(f) == 6
        row = f.iloc[0]
        assert row["open_time"] == SUN
        assert row["high"] == float(df["high"].iloc[:5].max())
        assert row["low"] == float(df["low"].iloc[:5].min())
        assert row["close"] == float(df["close"].iloc[4])
        assert row["volume"] == float(df["volume"].iloc[:5].sum())


class TestWatcherTick:
    """The fast-loop integration: freshness, silent seeding, one announcement.

    The property worth pinning is the restart one: a watcher that starts at
    16:00 must not replay the day's sweeps onto a phone as though they had all
    just happened. The first evaluation absorbs whatever the data holds; only
    what changes AFTER that gets announced.
    """

    def _watcher(self, tmp_path):
        from pathlib import Path

        from backtest.watch import Watcher
        cfg = Path(__file__).resolve().parents[1] / "configs" / "ut_1h_long_v1.yaml"
        return Watcher(
            cfg, db_path=tmp_path / "w.db", live_mode=False,
            report_dir=tmp_path / "reports", symbols=["XAUUSDT"],
        )

    def _seed_stored(self, tmp_path, db_path):
        """Days 1-2 in the DB; day 3 arrives as the fresh minute frame."""
        from backtest.data.db import CandleRepository, Database
        stored = minute_bars(SUN, 2 * 1440, base=4600.0, amp=0.3)
        with Database(db_path) as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", [
                tuple(r) for r in stored[
                    ["open_time", "open", "high", "low", "close", "volume",
                     "turnover"]].to_numpy()
            ])
        return stored

    def test_first_tick_absorbs_silently(self, tmp_path):
        from backtest.data.db import CandleRepository, Database
        w = self._watcher(tmp_path)
        self._seed_stored(tmp_path, w.db_path)
        # A Tuesday frame that already contains a sweep at 16:00.
        minute = minute_bars(SUN, 3 * 1440, base=4600.0, amp=0.3)
        minute = poke(minute, TUE_MYT0, "1600", high=4620.0, low=4600.0,
                      close=4600.0)
        minute = quiet(minute, TUE_MYT0, "1605")
        minute = minute[minute["open_time"] >= TUE_MYT0].reset_index(drop=True)
        with Database(w.db_path) as db:
            repo = CandleRepository(db)
            w._tick_sessions(repo, "XAUUSDT", minute, TUE_MYT0 + 18 * H)
        assert w._sessions["XAUUSDT"]["state"] == "swept_high"
        assert w._session_events == [], \
            "the first evaluation this process absorbs, it does not announce"

    def test_a_new_event_after_the_first_tick_is_announced(self, tmp_path):
        from backtest.data.db import CandleRepository, Database
        w = self._watcher(tmp_path)
        self._seed_stored(tmp_path, w.db_path)
        base = minute_bars(SUN, 3 * 1440, base=4600.0, amp=0.3)

        # Tick 1: Tuesday 16:00, nothing has happened yet.
        m1 = base[base["open_time"] < TUE_MYT0 + 16 * H].reset_index(drop=True)
        m1 = m1[m1["open_time"] >= TUE_MYT0].reset_index(drop=True)
        with Database(w.db_path) as db:
            repo = CandleRepository(db)
            w._tick_sessions(repo, "XAUUSDT", m1, TUE_MYT0 + 16 * H)

        # Tick 2: the same feed advanced and now carries a poke at 16:00,
        # confirmed once the 16:00 bucket closes (16:06 read).
        m2 = poke(base, TUE_MYT0, "1600", high=4620.0, low=4600.0,
                  close=4600.0)
        m2 = quiet(m2, TUE_MYT0, "1605")
        m2 = m2[(m2["open_time"] >= TUE_MYT0)
                & (m2["open_time"] < TUE_MYT0 + 16 * H + 10 * MIN)]
        m2 = m2.reset_index(drop=True)
        with Database(w.db_path) as db:
            repo = CandleRepository(db)
            w._tick_sessions(repo, "XAUUSDT", m2, TUE_MYT0 + 16 * H + 10 * MIN)

        kinds = [e["type"] for e in w._session_events]
        assert kinds == ["sweep_high"], "announced once, and only once"
        ev = w._session_events[0]
        assert ev["id"].startswith("ses:XAUUSDT@")
        assert ev["kind"] == "session_event"
        assert ev["symbol"] == "XAUUSDT"

        # Tick 3: the same data again — nothing new, nothing announced.
        with Database(w.db_path) as db:
            repo = CandleRepository(db)
            w._tick_sessions(repo, "XAUUSDT", m2, TUE_MYT0 + 16 * H + 10 * MIN)
        assert len(w._session_events) == 1

    def test_a_dead_minute_frame_costs_nothing(self, tmp_path):
        from backtest.data.db import CandleRepository, Database
        w = self._watcher(tmp_path)
        self._seed_stored(tmp_path, w.db_path)
        with Database(w.db_path) as db:
            w._tick_sessions(CandleRepository(db), "XAUUSDT",
                             pd.DataFrame(), TUE_MYT0 + 18 * H)
        assert "XAUUSDT" not in w._sessions
        assert w._session_events == []

    def test_the_day_verdict_is_announced_once_on_change(self, tmp_path):
        """美盘开始半小时后: long/short/range, one push per change."""
        from backtest.data.db import CandleRepository, Database

        def frame(until_hhmm):
            """Tuesday quiet day with Europe +0.4R and US extending it."""
            df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=5.0)
            df = quiet(df, MON_MYT0, "1500", span=540)
            df = quiet(df, TUE_MYT0, "1500", span=540)
            base = sm.build_map(df, as_of_ms=TUE_MYT0 + 16 * H)
            mid = (base["levels"]["asia_high"]
                   + base["levels"]["asia_low"]) / 2.0
            df = poke(df, TUE_MYT0, "1930", span=30, open=mid + 4.0,
                      close=mid + 4.0, high=mid + 4.5, low=mid + 3.5)
            # Span to the end of the frame: the tail must not fall back to
            # the quiet level and reverse the read it exists to confirm.
            df = poke(df, TUE_MYT0, "2030", span=40, open=mid + 8.0,
                      close=mid + 8.0, high=mid + 8.5, low=mid + 7.5)
            hh, mm = int(until_hhmm[:2]), int(until_hhmm[2:])
            df = df[(df["open_time"] >= TUE_MYT0)
                    & (df["open_time"] < TUE_MYT0 + hh * H + mm * MIN)]
            return df.reset_index(drop=True)

        w = self._watcher(tmp_path)
        self._seed_stored(tmp_path, w.db_path)
        with Database(w.db_path) as db:
            repo = CandleRepository(db)
            # Tick 1 (20:10): the verdict has not unlocked; nothing to say.
            w._tick_sessions(repo, "XAUUSDT", frame("2010"),
                             TUE_MYT0 + 20 * H + 10 * MIN)
            assert w._session_events == []
            # Tick 2 (21:10): verdict ready — announced exactly once.
            w._tick_sessions(repo, "XAUUSDT", frame("2110"),
                             TUE_MYT0 + 21 * H + 10 * MIN)
            verdicts = [e for e in w._session_events
                        if e["type"] == "day_verdict"]
            assert len(verdicts) == 1
            assert verdicts[0]["bias"] == "long"
            assert verdicts[0]["id"].endswith(":verdict:long")
            assert verdicts[0]["basis"]
            # Tick 3 (same data): no repeat.
            w._tick_sessions(repo, "XAUUSDT", frame("2110"),
                             TUE_MYT0 + 21 * H + 10 * MIN)
            assert len([e for e in w._session_events
                        if e["type"] == "day_verdict"]) == 1

    def test_a_six_hour_live_pull_still_sees_the_whole_day(self, tmp_path):
        """The panel's evening collapse, pinned.

        Past 21:00 MYT the Asia window (07:00–15:00) falls out of the fast
        loop's 6-hour 1m pull, and the map used to collapse to waiting_asia
        with null levels — thrashing the day verdict and the Telegram feed.
        The stored 1m tail must bridge the gap: the live frame carries only
        the last six hours, the map still carries the full day.
        """
        from backtest.data.db import CandleRepository, Database

        w = self._watcher(tmp_path)
        self._seed_stored(tmp_path, w.db_path)
        full = minute_bars(SUN, 3 * 1440, base=4600.0, amp=5.0)
        full = quiet(full, TUE_MYT0, "1500", span=540)
        # The DB carries everything through 21:30 MYT Tuesday.
        with Database(w.db_path) as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", [
                tuple(r) for r in full[
                    ["open_time", "open", "high", "low", "close", "volume",
                     "turnover"]].to_numpy()
                if r[0] < TUE_MYT0 + 21 * H + 30 * MIN
            ])
        # The live pull only reaches 6h back: 15:30 onward. Without the
        # merge, Asia is outside the frame and the levels go null.
        live = full[(full["open_time"] >= TUE_MYT0 + 15 * H + 30 * MIN)
                    & (full["open_time"] < TUE_MYT0 + 21 * H + 30 * MIN)
                    ].reset_index(drop=True)
        with Database(w.db_path) as db:
            w._tick_sessions(CandleRepository(db), "XAUUSDT", live,
                             TUE_MYT0 + 21 * H + 30 * MIN)
        lv = w._sessions["XAUUSDT"]["levels"]
        assert lv["asia_high"] is not None and lv["asia_low"] is not None
        assert w._sessions["XAUUSDT"]["state"] != "waiting_asia"
        assert lv["prev_day_high"] is not None, \
            "yesterday's H/L come from the stored tail"
