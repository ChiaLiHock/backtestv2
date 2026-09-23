"""Engine-level tests: determinism, no look-ahead in the fill path, and the
multi-timeframe feature alignment.

These run against a synthetic in-memory database so they need no network and no
synced data.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine import features as F
from backtest.engine.backtester import Backtester
from backtest.engine.config import StrategyConfig

CONFIG_TEXT = """
name: test_strategy
symbol: TESTUSDT
timeframe: 1h
period:
  start: 1970-01-01
  end: 2100-01-01
session_filter:
  mode: none
entry:
  side: long
  any_of:
    - signal: ut_cross_up
  cooldown_bars: 2
exit:
  take_profit: { mode: price_delta, value: 20 }
  stop_loss:   { mode: price_delta, value: 20 }
  time_stop_bars: 24
sizing:
  mode: fixed_qty
  qty: 1.0
costs:
  taker_fee_bps: 5.5
  slippage_ticks: 1
  apply_funding: false
execution:
  entry_fill: next_bar_open
  intrabar_resolution: 1m
features:
  timeframes: [1h, 4h]
  include: [atr_14, rsi_14, ut_bias, confirmed_bias]
  derived: [hour_of_day_myt, session, atr_percentile_250]
indicators:
  warmup_bars: 60
"""


def _series(n: int, interval: str, seed: int = 3) -> pd.DataFrame:
    """A market-like 1m series: oscillating trend plus noise.

    Deliberately NOT a pure random walk with drift — a constant drift over 57,600
    minutes runs price from 2,000 to 25,000 and the UT trailing stop never flips
    back, so the fixture yields zero entries and every engine test passes
    vacuously. Cycling through up and down legs is what makes the crossings the
    tests depend on actually occur.
    """
    rng = np.random.default_rng(seed)
    step = INTERVAL_MS[interval]
    i = np.arange(n)
    cycle = 60.0 * np.sin(2 * np.pi * i / 2880.0)      # ~2-day swings, 60 wide
    slow = 25.0 * np.sin(2 * np.pi * i / 20160.0)      # ~2-week drift
    close = 2000.0 + cycle + slow + np.cumsum(rng.normal(0.0, 0.35, n))
    spread = np.abs(rng.normal(0, 0.6, n)) + 0.15
    return pd.DataFrame({
        "open_time": np.arange(n, dtype="int64") * step,
        "open": close - rng.normal(0, 1.0, n),
        "high": close + spread,
        "low": close - spread,
        "close": close,
        "volume": np.abs(rng.normal(100, 20, n)),
        "turnover": np.abs(rng.normal(200000, 40000, n)),
    })


def _resample(minute: pd.DataFrame, interval: str) -> pd.DataFrame:
    step = INTERVAL_MS[interval]
    bucket = minute["open_time"] // step * step
    g = minute.groupby(bucket)
    return pd.DataFrame({
        "open_time": g["open_time"].min().index.astype("int64"),
        "open": g["open"].first().to_numpy(),
        "high": g["high"].max().to_numpy(),
        "low": g["low"].min().to_numpy(),
        "close": g["close"].last().to_numpy(),
        "volume": g["volume"].sum().to_numpy(),
        "turnover": g["turnover"].sum().to_numpy(),
    }).reset_index(drop=True)


@pytest.fixture(scope="module")
def seeded_db():
    """A DB whose 1h/4h bars are genuinely aggregated from the 1m series, so the
    intrabar replay sees a consistent world rather than three unrelated randoms."""
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    db = Database(tmp)
    repo = CandleRepository(db)

    minute = _series(60 * 24 * 40, "1m", seed=11)  # 40 days of 1m
    repo.upsert("TESTUSDT", "1m", minute[list(
        ("open_time", "open", "high", "low", "close", "volume", "turnover")
    )].itertuples(index=False, name=None))

    for interval in ("1h", "4h"):
        agg = _resample(minute, interval)
        repo.upsert("TESTUSDT", interval, agg.itertuples(index=False, name=None))

    yield db
    db.close()


def _cfg() -> StrategyConfig:
    return StrategyConfig.from_text(CONFIG_TEXT)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_config_same_hash():
    assert _cfg().config_hash() == _cfg().config_hash()


def test_hash_changes_when_a_threshold_changes():
    a = _cfg()
    b = StrategyConfig.from_text(CONFIG_TEXT.replace("value: 20", "value: 30"))
    assert a.config_hash() != b.config_hash()


def test_hash_is_stable_under_reformatting():
    """Reordering keys or reflowing YAML must not invent a new run."""
    reordered = CONFIG_TEXT.replace(
        "name: test_strategy\nsymbol: TESTUSDT\ntimeframe: 1h",
        "timeframe: 1h\nsymbol: TESTUSDT\nname: test_strategy",
    )
    assert _cfg().config_hash() == StrategyConfig.from_text(reordered).config_hash()


def test_rerun_produces_identical_trades(seeded_db):
    a = Backtester(seeded_db, _cfg()).run(run_id="fixed_a")
    b = Backtester(seeded_db, _cfg()).run(run_id="fixed_b")
    assert len(a.trades) == len(b.trades)
    assert len(a.trades) > 0, "fixture produced no trades — the test proves nothing"
    for x, y in zip(a.trades, b.trades):
        assert x.entry_time == y.entry_time
        assert x.entry_price == y.entry_price
        assert x.exit_time == y.exit_time
        assert x.exit_price == y.exit_price
        assert x.exit_reason == y.exit_reason
        assert x.net_pnl == y.net_pnl
        assert x.signal_id == y.signal_id


# ---------------------------------------------------------------------------
# Fill semantics
# ---------------------------------------------------------------------------


def test_entry_fills_on_the_bar_after_the_signal(seeded_db):
    """A signal confirmed at the close of t must fill at the OPEN of t+1."""
    cfg = _cfg()
    result = Backtester(seeded_db, cfg).run(run_id="fill_check")
    raw = CandleRepository(seeded_db).load("TESTUSDT", "1h")
    open_time = raw["open_time"].to_numpy()
    opens = raw["open"].to_numpy()
    tick = cfg.costs.tick_size * cfg.costs.slippage_ticks

    assert result.trades
    for t in result.trades:
        i = int(np.searchsorted(open_time, t.entry_time))
        assert open_time[i] == t.entry_time
        # entry price is that bar's OPEN plus adverse slippage
        assert t.entry_price == pytest.approx(opens[i] + tick)
        # and the signal bar is strictly earlier
        assert t.entry_index < i


def test_exit_never_precedes_entry(seeded_db):
    result = Backtester(seeded_db, _cfg()).run(run_id="order_check")
    for t in result.trades:
        assert t.exit_time is not None and t.exit_time > t.entry_time


def test_only_one_position_at_a_time(seeded_db):
    result = Backtester(seeded_db, _cfg()).run(run_id="overlap_check")
    ordered = sorted(result.trades, key=lambda t: t.entry_time)
    for prev, nxt in zip(ordered, ordered[1:]):
        assert prev.exit_time is not None
        assert nxt.entry_time >= prev.exit_time - INTERVAL_MS["1h"], (
            "positions overlapped despite max_concurrent_positions = 1"
        )


def test_every_trade_has_features(seeded_db):
    """Acceptance criterion: no trade may carry an empty feature snapshot."""
    result = Backtester(seeded_db, _cfg()).run(run_id="feature_check")
    assert result.trades
    with_features = {row[0] for row in result.feature_rows}
    for t in result.trades:
        assert t.trade_id in with_features, f"trade {t.trade_id} has no features"


def test_features_include_both_timeframes(seeded_db):
    result = Backtester(seeded_db, _cfg()).run(run_id="tf_check")
    names = {row[1] for row in result.feature_rows}
    assert any(n.startswith("1h_") for n in names)
    assert any(n.startswith("4h_") for n in names)
    assert "session" in names and "hour_of_day_myt" in names


# ---------------------------------------------------------------------------
# The look-ahead trap: higher-timeframe alignment
# ---------------------------------------------------------------------------


def test_htf_alignment_never_uses_an_unclosed_bar():
    """At the close of a 1H bar, the 4H bar covering it has usually NOT closed.

    Using it would leak up to 4 hours of future into the feature snapshot.
    """
    hour = INTERVAL_MS["1h"]
    four = INTERVAL_MS["4h"]
    anchor_opens = np.arange(0, 24) * hour
    h4_opens = np.arange(0, 6) * four

    idx = F.build_htf_alignment(anchor_opens, "1h", {"4h": (h4_opens, pd.DataFrame())})["4h"]

    for i, a_open in enumerate(anchor_opens):
        a_close = a_open + hour
        chosen = idx[i]
        if chosen >= 0:
            # the chosen 4H bar must have CLOSED by the anchor's close
            assert h4_opens[chosen] + four <= a_close
            # and it must be the newest such bar
            if chosen + 1 < h4_opens.size:
                assert h4_opens[chosen + 1] + four > a_close
        else:
            # nothing had closed yet -> only legitimate early on
            assert a_close < four


def test_closed_bar_index_boundary():
    four = INTERVAL_MS["4h"]
    opens = np.array([0, four, 2 * four], dtype="int64")
    # exactly at the close of the first 4H bar, it counts
    assert F.closed_bar_index(opens, "4h", four) == 0
    # one ms before, it does not
    assert F.closed_bar_index(opens, "4h", four - 1) == -1


def test_aligned_frame_blanks_rows_with_nothing_closed():
    frame = pd.DataFrame({
        "atr_14": [1.0, 2.0, 3.0],
        "ut_bias": np.array([1, -1, 0], dtype="int8"),
        "usable": np.array([True, True, True]),
    })
    out = F.aligned_frame(frame, np.array([-1, 0, 2]))
    assert np.isnan(out["atr_14"].iloc[0])
    assert np.isnan(out["ut_bias"].iloc[0])      # widened to float to hold NaN
    assert out["usable"].iloc[0] == False        # noqa: E712 - bools blank to False
    assert out["atr_14"].iloc[1] == 1.0
    assert out["atr_14"].iloc[2] == 3.0


def test_session_labels_are_exhaustive():
    hour_ms = 3_600_000
    seen = {F.session_myt(h * hour_ms - 8 * hour_ms) for h in range(24)}
    assert seen <= {"asia", "london", "ny", "off"}
    assert {"asia", "london", "ny"} <= seen


def test_gold_session_excludes_the_weekend():
    # 2026-08-22 is a Saturday.
    sat = int(pd.Timestamp("2026-08-22T12:00:00Z").timestamp() * 1000)
    assert not F.is_gold_session(sat)
    # Friday 23:00 UTC is after the 22:00 close.
    fri_late = int(pd.Timestamp("2026-08-21T23:00:00Z").timestamp() * 1000)
    assert not F.is_gold_session(fri_late)
    # Friday 20:00 UTC is still open — the NY afternoon a naive Mon-Fri MYT
    # filter would discard.
    fri_pm = int(pd.Timestamp("2026-08-21T20:00:00Z").timestamp() * 1000)
    assert F.is_gold_session(fri_pm)
    assert not F.is_weekday_myt(fri_pm), "this is the disagreement worth knowing about"
    # Sunday 22:00 UTC — reopened.
    sun_open = int(pd.Timestamp("2026-08-23T22:00:00Z").timestamp() * 1000)
    assert F.is_gold_session(sun_open)


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def test_run_refuses_unfilled_gaps(seeded_db):
    """A backtest over a hole in the data is not a backtest."""
    repo = CandleRepository(seeded_db)
    with seeded_db.tx() as cur:
        cur.execute(
            "DELETE FROM candles WHERE symbol='TESTUSDT' AND interval='1h' "
            "AND open_time BETWEEN ? AND ?",
            (200 * INTERVAL_MS["1h"], 205 * INTERVAL_MS["1h"]),
        )
    try:
        assert repo.find_gaps("TESTUSDT", "1h")
        with pytest.raises(SystemExit, match="unfilled gaps"):
            Backtester(seeded_db, _cfg()).run(run_id="gap_check")
        # ...but --allow-gaps lets it through deliberately.
        result = Backtester(seeded_db, _cfg()).run(run_id="gap_ok", allow_gaps=True)
        assert result.gaps["1h"] > 0
    finally:
        minute = _series(60 * 24 * 40, "1m", seed=11)
        repo.upsert("TESTUSDT", "1h", _resample(minute, "1h").itertuples(index=False, name=None))
