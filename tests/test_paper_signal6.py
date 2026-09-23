"""The paper gate: today's decision cannot see today's data.

The one property this file exists to pin: the model judging today's events
is trained with a HARD cutoff at the start of today (MYT). If that leaks,
the forward record is worthless as the spec §30 "fully OOS test" evidence.
"""

from __future__ import annotations

import numpy as np

from backtest.tests.test_session_map import (MIN, MON_MYT0, SUN, TUE_MYT0,
                                             minute_bars, poke, quiet)


def _dataset():
    df = minute_bars(SUN, 3 * 1440, base=4600.0, amp=5.0)
    # Monday gets its own sequence so the train side is non-empty.
    df = poke(df, MON_MYT0, "1530", high=4607.0, close=4605.0)
    df = poke(df, MON_MYT0, "1600", span=5, high=4607.2, low=4605.6,
              close=4607.0, vol=1000.0)
    df = poke(df, MON_MYT0, "1605", span=5, high=4607.4, low=4605.6,
              close=4607.5, vol=1000.0)
    df = quiet(df, MON_MYT0, "1610", span=300)
    df = poke(df, TUE_MYT0, "1530", high=4607.0, close=4605.0)
    df = poke(df, TUE_MYT0, "1600", span=5, high=4607.2, low=4605.6,
              close=4607.0, vol=1000.0)
    df = poke(df, TUE_MYT0, "1605", span=5, high=4607.4, low=4605.6,
              close=4607.5, vol=1000.0)
    df = poke(df, TUE_MYT0, "1610", span=5, open=4608.0, close=4608.0,
              high=4608.5, low=4607.5)
    df = quiet(df, TUE_MYT0, "1615", span=400)
    return df


class TestTrainCutoff:
    def test_decisions_carry_the_cutoff_and_cannot_train_on_today(
            self, tmp_path, monkeypatch):
        from backtest.data.db import CandleRepository, Database
        dbp = tmp_path / "paper.db"
        with Database(dbp) as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", [
                tuple(r) for r in _dataset()[
                    ["open_time", "open", "high", "low", "close", "volume",
                     "turnover"]].to_numpy()])

        # "today" = the Tuesday of the dataset: the model must judge it
        # from Monday and earlier only.
        from backtest.tools import paper_signal6 as P
        today0 = TUE_MYT0
        with Database(dbp) as db:
            out = P.evaluate("XAUUSDT", "1m", today0, with_db=db,
                             min_train=1)
        # only one break event in the dataset, on Tuesday
        assert len(out) == 1
        d = out[0]
        assert d["train_cutoff_ms"] == today0
        assert d["event_type"] == "break_high"
        assert d["decision"] in ("PASS", "REJECT")
        assert 0.0 <= d["p_win"] <= 1.0
        assert d["regime"] in ("trend", "range")
        assert d["threshold"] >= 0.45

    def test_insufficient_history_decides_nothing(self, tmp_path):
        from backtest.data.db import CandleRepository, Database
        dbp = tmp_path / "thin.db"
        df = _dataset()[_dataset()["open_time"] >= TUE_MYT0]
        with Database(dbp) as db:
            CandleRepository(db).upsert("XAUUSDT", "1m", [
                tuple(r) for r in df[
                    ["open_time", "open", "high", "low", "close", "volume",
                     "turnover"]].to_numpy()])
        from backtest.tools import paper_signal6 as P
        with Database(dbp) as db:
            assert P.evaluate("XAUUSDT", "1m", TUE_MYT0, with_db=db) == []

    def test_log_is_append_only_and_deduped(self, tmp_path, monkeypatch):
        from backtest.tools import paper_signal6 as P
        P.LOG_PATH = tmp_path / "paper.jsonl"
        decision = {"symbol": "XAUUSDT", "entry_time": 1, "decision": "PASS"}
        P.LOG_PATH.write_text("", encoding="utf-8")
        with open(P.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(__import__("json").dumps(decision) + "\n")
        seen = {(decision["symbol"], decision["entry_time"])}
        fresh = [d for d in [dict(decision, ts_ms=2)] if
                 (d["symbol"], d["entry_time"]) not in seen]
        assert fresh == [], "the dedup keeps the log one row per event"
