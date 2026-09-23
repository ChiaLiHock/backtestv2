"""Importing real broker fills.

The failure mode this guards against is not a crash — it is an import that
succeeds, puts every fill an hour or three from where it happened, and still
looks entirely plausible on the chart. So the timezone is measured rather than
assumed, and these tests check the measurement rather than the parsing.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from backtest.data import mt5
from backtest.data.db import CandleRepository, Database

REPORT = r"C:\Users\User\XM\2026\August\MT5ReportHistory-335445171.xlsx"


def _report():
    from pathlib import Path
    if not Path(REPORT).exists():
        pytest.skip("no MT5 report on this machine")
    return mt5.read_report(REPORT)


def test_only_mapped_symbols_are_imported():
    """An unknown broker symbol must be skipped, never guessed onto a contract."""
    _, raw = _report()
    assert raw, "expected positions"
    seen = {r["broker_symbol"] for r in raw}
    assert "AAVEUSD" in seen, "fixture should contain an unmapped symbol"
    trades = mt5.to_trades("acct", raw, 3)
    assert {t.broker_symbol for t in trades} <= set(mt5.SYMBOL_MAP)
    assert all(t.symbol in ("XAUUSDT", "BTCUSDT", "ETHUSDT") for t in trades)


def test_detected_offset_is_the_broker_server_zone():
    """XM Global runs EET/EEST; August is UTC+3. Measured, not assumed.

    The point of the assertion is the *margin*: a whole-hour error on gold lands
    tens of points away, so a correct fit is not a close call.
    """
    _, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    with Database() as db:
        if CandleRepository(db).load("XAUUSDT", "1m").empty:
            pytest.skip("needs XAUUSDT 1m synced")
        fit = mt5.detect_offset(db, known)
    assert fit is not None
    assert fit.offset_hours == 3, fit.describe()
    assert fit.confident, fit.describe()
    assert fit.runner_up_error > fit.median_error * 2


def test_fills_land_on_the_bar_they_were_taken_on():
    """The end-to-end check that the conversion is right.

    Measured on the **1h** bar, not the 1m one: a gold minute candle is often
    thinner than the 3.4-point basis between the broker's spot contract and the
    exchange's perpetual, so "inside the 1m range" would be failing for a reason
    that has nothing to do with time. On the hour it is unambiguous — at the
    wrong offset this collapses from ~87% to noise.
    """
    account, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    trades = [t for t in mt5.to_trades(account, known, 3) if t.symbol == "XAUUSDT"]
    with Database() as db:
        h1 = CandleRepository(db).load("XAUUSDT", "1h")
        if h1.empty:
            pytest.skip("needs XAUUSDT 1h synced")
    t = h1["open_time"].to_numpy(dtype="int64")
    hi, lo = h1["high"].to_numpy(), h1["low"].to_numpy()

    inside = n = 0
    worst = 0.0
    for tr in trades:
        i = int(np.searchsorted(t, tr.open_time, "right")) - 1
        if i < 0 or tr.open_time - t[i] > 3_600_000:
            continue
        n += 1
        p = tr.open_price
        if lo[i] <= p <= hi[i]:
            inside += 1
        worst = max(worst, lo[i] - p if p < lo[i] else (p - hi[i] if p > hi[i] else 0.0))
    assert n > 100, f"only {n} fills matched a bar"
    assert inside / n > 0.7, f"only {inside/n:.0%} of fills are inside their 1h bar"
    assert worst < 15.0, f"a fill is {worst:.2f} outside its bar"


def test_the_leftover_gap_is_the_contract_basis_not_a_time_error():
    """What is left after aligning the clock must be a *constant* price offset.

    A residual that is centred on zero but noisy would mean the timestamps are
    right and something else is wrong; a residual with a consistent sign and a
    tight spread is two different contracts, which is expected and is why fills
    are never price-adjusted.
    """
    account, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    with Database() as db:
        if CandleRepository(db).load("XAUUSDT", "1m").empty:
            pytest.skip("needs XAUUSDT 1m synced")
        trades = mt5.to_trades(account, known, 3)
        basis = mt5.basis_report(db, trades)
    gold = basis.get("XAUUSDT")
    assert gold and gold["n"] > 100
    # Spot gold trades under the perpetual here, consistently and by well under
    # a tenth of a percent.
    assert -0.3 < gold["median_pct"] < 0.0, gold
    spread = gold["p75"] - gold["p25"]
    assert spread < 6.0, f"basis is not stable enough to be a contract offset: {gold}"


def test_a_wrong_offset_is_clearly_worse():
    """Guards the guard: if the scoring could not tell +2 from +3, the detector
    would be decorative."""
    _, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    with Database() as db:
        if CandleRepository(db).load("XAUUSDT", "1m").empty:
            pytest.skip("needs XAUUSDT 1m synced")
        right = mt5.detect_offset(db, known, candidates=[3])
        wrong = mt5.detect_offset(db, known, candidates=[2])
        other = mt5.detect_offset(db, known, candidates=[4])
    assert right.median_error < wrong.median_error / 2
    assert right.median_error < other.median_error / 2


def test_gold_lot_maps_onto_the_backtest_unit():
    """0.01 GOLD lot is 1 oz, which is the engine's default qty of 1.0 — the
    bridge that makes a real P/L comparable with a backtested one."""
    assert mt5.CONTRACT_SIZE["GOLD"] == 100.0
    account, raw = _report()
    gold = [r for r in raw if r["broker_symbol"] == "GOLD" and r["close_price"]]
    trades = mt5.to_trades(account, gold, 3)
    checked = 0
    for tr in trades[:60]:
        if tr.profit is None or tr.swap:
            continue
        move = (tr.close_price - tr.open_price) * (1 if tr.side == "buy" else -1)
        assert move * tr.units == pytest.approx(tr.profit, abs=0.6), tr.position_id
        checked += 1
    assert checked > 20


def test_timestamps_are_stored_as_utc_epoch_ms():
    account, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    trades = mt5.to_trades(account, known, 3)
    for tr in trades[:50]:
        assert isinstance(tr.open_time, int)
        assert tr.open_time > 1_600_000_000_000       # ms, not seconds
        if tr.close_time is not None:
            assert tr.close_time >= tr.open_time
    # a naive 08:00 broker time at +3 must land on 05:00 UTC
    sample = [r for r in known if r["open_naive"] is not None][0]
    shifted = mt5.to_trades(account, [sample], 3)[0]
    naive = sample["open_naive"]
    from datetime import timezone
    assert datetime.fromtimestamp(shifted.open_time / 1000, timezone.utc).hour \
        == (naive.hour - 3) % 24


def test_open_positions_are_imported_without_a_close():
    account, raw = _report()
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    trades = mt5.to_trades(account, known, 3)
    live = [t for t in trades if t.close_time is None]
    assert live, "the fixture has open ETHUSD positions"
    assert all(t.close_price is None for t in live)
