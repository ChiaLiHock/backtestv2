"""The live MetaTrader 5 connection.

Two properties matter more than any behaviour here, and both are asserted
statically so they hold even on a machine with no terminal installed:

* it never handles a credential, and
* it never places, modifies or closes anything.

The rest need a running terminal and skip without one.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from backtest.data import mt5_live
from backtest.data.db import Database

SOURCE = Path(mt5_live.__file__)
REPORT = Path(r"C:\Users\User\XM\2026\August\MT5ReportHistory-335445171.xlsx")

# Everything in the MetaTrader5 package that can act on the account.
FORBIDDEN_CALLS = {"order_send", "order_check"}


def _called_attrs(tree: ast.AST) -> set[str]:
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def test_module_never_places_an_order():
    """A read-only tool has to be read-only in the source, not just in intent."""
    used = _called_attrs(ast.parse(SOURCE.read_text(encoding="utf-8")))
    assert not (used & FORBIDDEN_CALLS), sorted(used & FORBIDDEN_CALLS)


def test_initialize_is_called_without_credentials():
    """`initialize()` must take no arguments.

    The MetaTrader5 package also accepts `initialize(login=, password=, server=)`,
    which would mean this program handling a live trading password. It attaches to
    the already-logged-in terminal instead, and that has to stay true.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    inits = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "initialize"]
    assert inits, "expected an initialize() call"
    for call in inits:
        assert not call.args and not call.keywords, ast.dump(call)


def test_no_password_identifier_anywhere_in_the_module():
    """Nothing may name a password, let alone hold one.

    `login` is deliberately NOT checked: the module reads the account number back
    out of the terminal in order to report it, which is the opposite of supplying
    a credential.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    names |= {kw.arg or "" for n in ast.walk(tree)
              if isinstance(n, ast.Call) for kw in n.keywords}
    bad = {x for x in names if x and ("passw" in x.lower() or "secret" in x.lower())}
    assert not bad, sorted(bad)


# --- everything below needs a terminal ------------------------------------


def _connect():
    try:
        return mt5_live.connect()
    except mt5_live.Mt5Unavailable as exc:
        pytest.skip(f"no MT5 terminal: {str(exc).splitlines()[0]}")


def test_server_clock_is_measured_from_a_live_quote():
    api, term = _connect()
    try:
        assert term.clock_age_s < 900, term.describe()
        # A stale-tick bug produces something wild like -32; a real broker offset
        # is a whole hour in this band.
        assert -12 <= term.offset_hours <= 14, term.describe()
        assert term.login > 0 and term.server
    finally:
        api.shutdown()


def test_live_clock_agrees_with_the_xlsx_price_matching():
    """Two unrelated methods, one answer.

    The .xlsx path infers the offset from how well fill prices match candles; the
    live path reads it off a quote. Disagreement would mean one of them is wrong
    and the trades are on the wrong bars.
    """
    from backtest.data import mt5

    if not REPORT.exists():
        pytest.skip("no MT5 report on this machine")
    api, term = _connect()
    api.shutdown()
    _, raw = mt5.read_report(REPORT)
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    with Database() as db:
        fit = mt5.detect_offset(db, known)
    if fit is None:
        pytest.skip("no candles to match against")
    assert fit.offset_hours == term.offset_hours, (
        f"price matching says UTC{fit.offset_hours:+d}, "
        f"the terminal clock says UTC{term.offset_hours:+d}"
    )


def test_deals_rebuild_into_positions():
    api, term = _connect()
    try:
        trades = mt5_live.fetch_positions(
            api, term, since_ms=int((time.time() - 60 * 86400) * 1000))
    finally:
        api.shutdown()
    if not trades:
        pytest.skip("no recent positions on this account")
    ids = [t.position_id for t in trades]
    assert len(ids) == len(set(ids)), "a position_id appeared twice"
    for t in trades:
        assert t.symbol in ("XAUUSDT", "BTCUSDT", "ETHUSDT")
        assert t.side in ("buy", "sell")
        assert t.volume > 0
        assert t.open_time > 1_600_000_000_000
        if t.close_time is not None:
            assert t.close_time >= t.open_time
        else:
            assert t.close_price is None
    assert [t.open_time for t in trades] == sorted(t.open_time for t in trades)


def test_live_and_xlsx_agree_on_the_positions_they_share():
    """The overlap between the two import paths must be identical, not similar —
    they are reading the same fills by different routes."""
    from backtest.data import mt5

    if not REPORT.exists():
        pytest.skip("no MT5 report on this machine")
    account, raw = mt5.read_report(REPORT)
    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    from_xlsx = {t.position_id: t for t in mt5.to_trades(account, known, 3)}

    api, term = _connect()
    try:
        live = mt5_live.fetch_positions(
            api, term, since_ms=int((time.time() - 60 * 86400) * 1000))
    finally:
        api.shutdown()
    # Only positions closed in BOTH. The export is a snapshot, so one it recorded
    # as open may well have closed since — the live view being ahead is the point
    # of it, not a disagreement.
    common = [t for t in live
              if t.position_id in from_xlsx
              and t.close_time is not None
              and from_xlsx[t.position_id].close_time is not None]
    assert len(common) >= 20, f"only {len(common)} comparable positions"
    for t in common:
        x = from_xlsx[t.position_id]
        assert t.open_time == x.open_time, t.position_id
        assert t.close_time == x.close_time, t.position_id
        assert t.side == x.side and t.volume == pytest.approx(x.volume)
        assert t.open_price == pytest.approx(x.open_price, abs=1e-6), t.position_id
        assert t.close_price == pytest.approx(x.close_price, abs=1e-6), t.position_id
