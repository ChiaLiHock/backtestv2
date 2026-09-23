"""The live feed: bar folding, tail exactness, and cross-symbol normalisation.

The live path recomputes indicators over a short tail instead of the whole
series, and manufactures the newest bars from 1-minute data instead of waiting
for the slow sync. Both are shortcuts, and both are only safe under conditions
that are easy to break by accident — so they are pinned here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.data.db import INTERVAL_MS, CandleRepository, Database
from backtest.engine.config import StrategyConfig
from backtest.engine.symbols import _round_step, common_period, retarget
from backtest.indicators.base import IndicatorConfig
from backtest.indicators.registry import compute_indicators
from backtest.watch import LIVE_COLUMNS, TAIL_BARS, fold_from_minute

MIN = 60_000


def minute_frame(start_ms: int, n: int, base: float = 100.0) -> pd.DataFrame:
    """A deterministic 1-minute series; the shape does not matter, only the folds."""
    rows = []
    for i in range(n):
        o = base + i
        rows.append({
            "open_time": start_ms + i * MIN,
            "open": o, "high": o + 2.0, "low": o - 1.0, "close": o + 0.5,
            "volume": 10.0 + i, "turnover": (10.0 + i) * o,
        })
    return pd.DataFrame(rows)


def upto(m: pd.DataFrame, at_ms: int) -> pd.DataFrame:
    """Only the minutes that have actually happened at ``at_ms``.

    Production never sees a future bar, so a fixture that supplies one is testing
    a case that cannot occur — and `fold_from_minute` deliberately trusts the
    feed over the local clock.
    """
    return m[m["open_time"] <= at_ms].reset_index(drop=True)


# ---------------------------------------------------------------------------
# fold_from_minute
# ---------------------------------------------------------------------------


def test_fold_produces_one_forming_bar_when_nothing_has_closed():
    """Mid-bar: exactly one bar, flagged forming, aggregating every minute in it."""
    m = minute_frame(0, 20)                       # 00:00 .. 00:19
    bars, forming = fold_from_minute(m, "5m", last_stored_ms=10 * MIN, at_ms=17 * MIN)
    assert [b["open_time"] for b in bars] == [15 * MIN]
    assert forming == 15 * MIN
    assert bars[0]["open"] == m.loc[15, "open"]
    assert bars[0]["high"] == m.loc[15:19, "high"].max()
    assert bars[0]["low"] == m.loc[15:19, "low"].min()
    assert bars[0]["close"] == m.loc[19, "close"]
    assert bars[0]["volume"] == pytest.approx(m.loc[15:19, "volume"].sum())


def test_fold_uses_exchange_data_beyond_a_slow_local_clock():
    """If this machine's clock lags, published minutes must still be folded in."""
    m = minute_frame(0, 25)                       # exchange is at 00:24
    bars, forming = fold_from_minute(m, "5m", last_stored_ms=5 * MIN,
                                     at_ms=12 * MIN)   # clock thinks 00:12
    assert [b["open_time"] for b in bars] == [10 * MIN, 15 * MIN, 20 * MIN]
    assert forming == 20 * MIN


def test_fold_fills_every_bar_the_db_is_missing():
    """The bug this exists to prevent: a closed bar skipped, leaving a hole.

    The slow loop syncs every few minutes, so real bars close between syncs.
    Appending only the in-progress bar onto a stale tail would make the indicator
    pass treat two non-adjacent bars as neighbours.
    """
    m = upto(minute_frame(0, 40), 32 * MIN)
    bars, forming = fold_from_minute(m, "5m", last_stored_ms=5 * MIN, at_ms=32 * MIN)
    got = [b["open_time"] for b in bars]
    assert got == [10 * MIN, 15 * MIN, 20 * MIN, 25 * MIN, 30 * MIN]
    assert np.all(np.diff(got) == INTERVAL_MS["5m"]), "folded bars must be contiguous"
    assert forming == 30 * MIN


def test_fold_refuses_when_the_minute_window_starts_too_late():
    """Better no bar than a bar invented from partial data."""
    m = minute_frame(20 * MIN, 20)                # starts at 00:20
    bars, forming = fold_from_minute(m, "5m", last_stored_ms=0, at_ms=35 * MIN)
    assert bars == [] and forming is None


def test_fold_returns_nothing_when_the_db_is_current():
    m = minute_frame(0, 20)
    bars, forming = fold_from_minute(m, "5m", last_stored_ms=15 * MIN, at_ms=17 * MIN)
    assert bars == [] and forming is None


def test_the_newest_bucket_is_always_the_forming_one():
    """Whatever the last folded bar is, it is the one still in progress."""
    m = minute_frame(0, 20)
    bars, forming = fold_from_minute(upto(m, 14 * MIN), "5m",
                                     last_stored_ms=5 * MIN, at_ms=14 * MIN)
    assert [b["open_time"] for b in bars] == [10 * MIN]
    assert forming == 10 * MIN                 # still inside 10:00-14:59
    bars, forming = fold_from_minute(upto(m, 15 * MIN), "5m",
                                     last_stored_ms=5 * MIN, at_ms=15 * MIN)
    assert [b["open_time"] for b in bars] == [10 * MIN, 15 * MIN]
    assert forming == 15 * MIN


def test_fold_stops_at_a_hole_in_the_minute_feed():
    m = upto(minute_frame(0, 40), 32 * MIN)
    m = m[(m["open_time"] < 20 * MIN) | (m["open_time"] >= 25 * MIN)].reset_index(drop=True)
    bars, _ = fold_from_minute(m, "5m", last_stored_ms=5 * MIN, at_ms=32 * MIN)
    got = [b["open_time"] for b in bars]
    assert got == [10 * MIN, 15 * MIN], "must not jump the missing 20:00 bucket"


def test_fold_matches_a_real_higher_timeframe_bar():
    """Folding 1m must reproduce what the exchange itself reports for that bar.

    Skipped when nothing is synced; this is the check that the shortcut is not
    merely self-consistent.
    """
    with Database() as db:
        repo = CandleRepository(db)
        one = repo.load("XAUUSDT", "1m")
        five = repo.load("XAUUSDT", "5m")
        if one.empty or five.empty:
            pytest.skip("no synced XAUUSDT data")
        target = int(five["open_time"].iloc[-50])
        window = one[(one["open_time"] >= target - 20 * MIN)
                     & (one["open_time"] < target + INTERVAL_MS["5m"])]
        bars, _ = fold_from_minute(window, "5m", target - INTERVAL_MS["5m"],
                                   target + INTERVAL_MS["5m"] - 1)
        assert bars, "expected one folded bar"
        folded = bars[-1]
        row = five[five["open_time"] == target].iloc[0]
        for field in ("open", "high", "low", "close"):
            assert folded[field] == pytest.approx(float(row[field]), abs=1e-6), field


# ---------------------------------------------------------------------------
# tail-window exactness
# ---------------------------------------------------------------------------


def test_tail_window_is_bit_identical_to_full_history():
    """The fast loop's whole premise.

    Every column it publishes must converge, so recomputing over the last
    TAIL_BARS gives the same numbers as recomputing over everything. If a column
    that accumulates (the swing chains, for one — see OPEN_QUESTIONS Q4) were
    ever added to LIVE_COLUMNS, this fails.
    """
    cfg = IndicatorConfig()
    with Database() as db:
        repo = CandleRepository(db)
        checked = 0
        for tf in ("5m", "15m", "30m", "1h", "4h"):
            full = repo.load("XAUUSDT", tf)
            if len(full) < TAIL_BARS + 200:
                continue
            checked += 1
            fi = compute_indicators(full, tf, cfg)
            tail = full.tail(TAIL_BARS).reset_index(drop=True)
            ti = compute_indicators(tail, tf, cfg)
            for col in LIVE_COLUMNS:
                a = fi[col].to_numpy()[-100:]
                b = ti[col].to_numpy()[-100:]
                same = (a == b) | (np.isnan(a) & np.isnan(b))
                assert same.all(), (
                    f"{tf}/{col}: tail window diverges from full history at "
                    f"{int((~same).sum())} of the last 100 bars"
                )
    if checked == 0:
        pytest.skip("no synced series long enough")


# ---------------------------------------------------------------------------
# cross-symbol normalisation
# ---------------------------------------------------------------------------


def test_round_step_snaps_to_lot_and_respects_the_minimum():
    assert _round_step(0.03812, 0.001, 0.001) == 0.038
    assert _round_step(0.0001, 0.001, 0.001) == 0.001      # never below min
    assert _round_step(1.234, None, None) == 1.234


def test_retarget_holds_risk_constant_in_atr_terms():
    """$25 on gold and $25 on BTC are not the same trade unless size is scaled."""
    with Database() as db:
        repo = CandleRepository(db)
        if repo.load("XAUUSDT", "1h").empty or repo.load("BTCUSDT", "1h").empty:
            pytest.skip("needs XAUUSDT and BTCUSDT synced")
        cfg = StrategyConfig.from_yaml(
            "C:/inetpub/Claude/ITSupport/backtest/configs/ut_1h_long_v1.yaml")
        start, end = common_period(db, ("XAUUSDT", "BTCUSDT"), "1h")
        new, note = retarget(cfg, db, "BTCUSDT", start_ms=start, end_ms=end)

        assert new.symbol == "BTCUSDT"
        assert new.name.endswith("@BTCUSDT")
        # Stop distance measured in ATR must survive the move to within the lot
        # step, which is the only thing allowed to perturb it.
        risk_ref = cfg.exit.stop_loss.value / (cfg.sizing.qty * note.atr_reference)
        risk_new = new.exit.stop_loss.value / (new.sizing.qty * note.atr_symbol)
        assert risk_new == pytest.approx(risk_ref, rel=0.03)
        # tickSize must come from the instrument, not be inherited from gold.
        assert new.costs.tick_size == 0.1


def test_retarget_is_a_noop_on_the_reference_symbol():
    with Database() as db:
        if CandleRepository(db).load("XAUUSDT", "1h").empty:
            pytest.skip("needs XAUUSDT synced")
        cfg = StrategyConfig.from_yaml(
            "C:/inetpub/Claude/ITSupport/backtest/configs/ut_1h_long_v1.yaml")
        new, note = retarget(cfg, db, "XAUUSDT")
        assert new.sizing.qty == cfg.sizing.qty
        assert new.name == cfg.name
        assert note.notes == []


# ---------------------------------------------------------------------------
# key-level snapshots
# ---------------------------------------------------------------------------


def test_zone_snapshot_matches_a_single_point_build():
    """The cached series must equal what a one-off build gives at the same instant.

    `tools/plot_chart.build_zones` is the reference implementation of the app's
    card; the series is only useful if it agrees with it bar for bar.
    """
    from backtest.indicators import zones
    from backtest.tools.plot_chart import build_zones

    cfg = IndicatorConfig()
    with Database() as db:
        repo = CandleRepository(db)
        h1 = repo.load("XAUUSDT", "1h")
        if len(h1) < 600:
            pytest.skip("needs XAUUSDT 1h synced")
        for offset in (1, 40, 300):
            at = int(h1["open_time"].iloc[-offset])
            snaps = zones.snapshot_series(db, "XAUUSDT", cfg, end_ms=at)
            assert snaps, f"no snapshot at offset {offset}"
            got = snaps[-1]
            assert got.ts == at

            ref, ref_atr = build_zones(repo, "XAUUSDT", cfg, end_ms=at)
            # Snapshots are stored at 4 dp; that is the precision the assertion
            # can meaningfully make, not a tolerance for disagreement.
            assert got.reference_atr == pytest.approx(ref_atr, abs=5e-5)
            assert len(got.resistance) == len(ref.resistance)
            assert len(got.support) == len(ref.support)
            for mine, theirs in zip(got.resistance, ref.resistance):
                assert mine[0] == pytest.approx(theirs.low, abs=1e-4)
                assert mine[1] == pytest.approx(theirs.high, abs=1e-4)
            for mine, theirs in zip(got.support, ref.support):
                assert mine[0] == pytest.approx(theirs.low, abs=1e-4)
                assert mine[1] == pytest.approx(theirs.high, abs=1e-4)


def test_zone_snapshot_uses_only_bars_that_had_closed():
    """A snapshot must not move when later bars arrive — the look-ahead test.

    Zones are rebuilt from a sliding window, which is exactly the shape of code
    that accidentally reads the future.
    """
    from backtest.indicators import zones

    cfg = IndicatorConfig()
    with Database() as db:
        h1 = CandleRepository(db).load("XAUUSDT", "1h")
        if len(h1) < 800:
            pytest.skip("needs XAUUSDT 1h synced")
        cut = int(h1["open_time"].iloc[-200])
        truncated = zones.snapshot_series(db, "XAUUSDT", cfg, end_ms=cut)
        full = {s.ts: s for s in zones.snapshot_series(db, "XAUUSDT", cfg)}
        assert truncated, "expected snapshots up to the cut"
        for s in truncated[-50:]:
            later = full[s.ts]
            assert s.price == later.price
            assert s.resistance == later.resistance, f"zones moved at {s.ts}"
            assert s.support == later.support, f"zones moved at {s.ts}"


def test_zones_never_come_from_the_ut_level():
    """`SupportResistanceEngine.kt:46-47` is explicit: the UT level is a trailing
    stop, not a level price has respected. If it ever leaked into the sources,
    the card would be quietly wrong in the way the app's own comment warns about.
    """
    from backtest.indicators import zones

    with Database() as db:
        snaps = zones.snapshot_series(db, "XAUUSDT", IndicatorConfig())
        if not snaps:
            pytest.skip("needs XAUUSDT synced")
        seen = set()
        for s in snaps[-400:]:
            for z in (*s.resistance, *s.support):
                seen.update(z[3])
        assert seen, "no zone sources at all"
        assert not any("UT" in src.upper() for src in seen), sorted(seen)
        # every source must be a swing or a session level
        for src in seen:
            assert ("swing" in src) or ("day" in src) or ("Session" in src), src


def test_zone_cache_is_reused_not_recomputed():
    from backtest.indicators import zones

    cfg = IndicatorConfig()
    with Database() as db:
        if CandleRepository(db).load("XAUUSDT", "1h").empty:
            pytest.skip("needs XAUUSDT synced")
        first = zones.snapshot_series(db, "XAUUSDT", cfg)
        if not first:
            pytest.skip("no snapshots")
        rows = db.conn.execute(
            "SELECT COUNT(*) c FROM zone_snapshots WHERE symbol='XAUUSDT' AND config_key=?",
            (zones.config_key(cfg),),
        ).fetchone()["c"]
        assert rows >= len(first)
        again = zones.snapshot_series(db, "XAUUSDT", cfg)
        assert [s.ts for s in again] == [s.ts for s in first]
        assert again[-1].resistance == first[-1].resistance


def test_zone_config_key_changes_with_the_window():
    from backtest.indicators import zones

    a = zones.config_key(IndicatorConfig())
    b = zones.config_key(IndicatorConfig(app_window_bars=400))
    assert a != b, "a different buffer must not reuse cached zones"
