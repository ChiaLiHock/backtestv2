"""Read trade history straight out of a running MetaTrader 5 terminal.

Replaces the export-and-import loop: the terminal is already logged in and
already holds the history, so there is nothing to download by hand.

**This never asks for, stores, or uses your account password.**
``MetaTrader5.initialize()`` with no arguments attaches to the terminal that is
*already running and already logged in* on this machine. The package also has a
``initialize(login=..., password=..., server=...)`` form that logs in
programmatically — it is deliberately not used, and no code here accepts a
credential of any kind. If the terminal is closed, open it yourself and run the
command again.

**It is read-only by construction.** The only terminal calls made anywhere in
this module are ``initialize``, ``terminal_info``, ``account_info``,
``symbol_select``, ``symbol_info_tick``, ``history_deals_get``, ``positions_get``
and ``shutdown``. The package can place and modify orders; nothing here does, and
``tests/test_mt5_live.py`` fails if an order-sending call ever appears in this
file.

**The clock problem is solved properly here.** MT5 hands back timestamps on the
broker server's clock while typing them as though they were UTC, so they need the
same offset correction as the .xlsx path. The difference is that a live terminal
can be *asked*: the last tick of a symbol whose market is open right now is, by
definition, roughly the server's current time. Comparing that to real UTC gives
the offset directly. On this account it reports UTC+3 from the tick clock, which
is the same answer the .xlsx price-matching heuristic reaches independently — two
unrelated methods agreeing is worth more than either alone.

Weekends are the trap: gold and FX stop quoting, so their last tick is Friday's
and would imply a nonsense offset. The freshest tick across the candidate symbols
is therefore the one used, because a closed market's tick can only ever be older.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .mt5 import SYMBOL_MAP, BrokerTrade

# Probed for the server clock. Crypto quotes through the weekend, which is when
# the metals and FX books are shut and their last tick is days stale.
CLOCK_SYMBOLS: tuple[str, ...] = ("BTCUSD", "ETHUSD", "GOLD", "EURUSD", "GOLD24-7")

# MT5 deal enums (`ENUM_DEAL_ENTRY`, `ENUM_DEAL_TYPE`).
_ENTRY_IN, _ENTRY_OUT, _ENTRY_INOUT, _ENTRY_OUT_BY = 0, 1, 2, 3
_DEAL_BUY, _DEAL_SELL = 0, 1


class Mt5Unavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Terminal:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    terminal: str
    build: int
    connected: bool
    trade_allowed: bool
    offset_hours: int
    clock_symbol: str
    clock_age_s: float

    def describe(self) -> str:
        return (f"{self.terminal} build {self.build} · account {self.login} "
                f"@ {self.server} · {self.currency} {self.balance:,.2f} "
                f"(equity {self.equity:,.2f}) · server clock UTC{self.offset_hours:+d} "
                f"from {self.clock_symbol} ({self.clock_age_s:.0f}s old)")


def _api():
    try:
        import MetaTrader5 as api  # noqa: N813
    except ImportError:
        raise Mt5Unavailable(
            "the MetaTrader5 package is not installed:\n"
            "  backtest\\.venv\\Scripts\\python.exe -m pip install MetaTrader5\n"
            "It is Windows-only and needs a 64-bit Python to match the terminal."
        ) from None
    return api


def connect() -> tuple[Any, Terminal]:
    """Attach to the running terminal and measure its clock. No credentials.

    Raises :class:`Mt5Unavailable` with something actionable rather than a bare
    ``False`` — the failure is almost always "the terminal is not open".
    """
    api = _api()
    if not api.initialize():                     # <- no login/password/server
        code, msg = api.last_error()
        raise Mt5Unavailable(
            f"could not attach to MetaTrader 5 ({code}: {msg}).\n"
            "Open the MT5 terminal, log in as usual, and leave it running. "
            "This never logs in for you and never handles your password."
        )

    ti, ai = api.terminal_info(), api.account_info()
    if ti is None or ai is None:
        api.shutdown()
        raise Mt5Unavailable("terminal attached but reported no account — is it logged in?")

    offset, sym, age = _server_offset(api)
    return api, Terminal(
        login=int(ai.login), server=str(ai.server), currency=str(ai.currency),
        balance=float(ai.balance), equity=float(ai.equity),
        terminal=str(ti.name), build=int(ti.build),
        connected=bool(ti.connected), trade_allowed=bool(ti.trade_allowed),
        offset_hours=offset, clock_symbol=sym, clock_age_s=age,
    )


def _server_offset(api) -> tuple[int, str, float]:
    """Whole-hour offset of the broker clock from UTC, from the freshest tick.

    A shut market's last tick is stale by hours or days and would imply a wild
    offset, so the freshest quote across the candidates wins — staleness can only
    move a tick backwards, never forwards.
    """
    now = time.time()
    best: tuple[float, str] | None = None
    for sym in CLOCK_SYMBOLS:
        try:
            if not api.symbol_select(sym, True):
                continue
            tick = api.symbol_info_tick(sym)
        except Exception:
            continue
        if not tick or not tick.time:
            continue
        if best is None or tick.time > best[0]:
            best = (float(tick.time), sym)
    if best is None:
        raise Mt5Unavailable(
            "no symbol is quoting, so the server clock cannot be measured. "
            "Import the .xlsx instead, or pass --tz-offset."
        )
    server_now, sym = best
    offset = int(round((server_now - now) / 3600.0))
    # Residual after removing the whole hours: how stale that quote is.
    age = abs(server_now - (now + offset * 3600.0))
    if age > 900:
        raise Mt5Unavailable(
            f"the freshest quote ({sym}) is {age/60:.0f} minutes old, so the "
            "server clock cannot be pinned down. Every market may be closed; try "
            "again when one is open, or import the .xlsx."
        )
    return offset, sym, age


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def fetch_positions(
    api, terminal: Terminal, since_ms: int | None = None
) -> list[BrokerTrade]:
    """Closed positions rebuilt from deals, plus anything still open.

    MT5 stores *deals*, not positions: an entry deal and one or more exit deals
    share a ``position_id``. Money is summed across all of them, because
    commission and swap are attached to individual deals and the position's true
    P/L is not on any single one.
    """
    # Timezone-aware, not a fixed offset: the server runs EET/EEST and a single
    # measured integer is an hour wrong from late October to late March. Same
    # defect the bar feed had — see data/broker_clock.py for the two proofs.
    from .broker_clock import server_naive_to_utc_ms, utc_ms_to_server_naive

    def to_ms(server_epoch: float) -> int:
        return int(server_naive_to_utc_ms([int(server_epoch)])[0])

    def to_server_dt(ms: int) -> datetime:
        return utc_ms_to_server_naive(int(ms))

    start_ms = since_ms if since_ms is not None else int(
        (time.time() - 5 * 365 * 86400) * 1000)
    frm = to_server_dt(start_ms)
    to = to_server_dt(int(time.time() * 1000)) + timedelta(days=2)

    deals = api.history_deals_get(frm, to)
    if deals is None:
        code, msg = api.last_error()
        raise Mt5Unavailable(f"history_deals_get failed ({code}: {msg})")

    groups: dict[int, list[Any]] = {}
    for d in deals:
        # Balance, credit and commission entries carry no symbol and no position.
        if not d.symbol or not d.position_id:
            continue
        if d.symbol not in SYMBOL_MAP:
            continue
        groups.setdefault(int(d.position_id), []).append(d)

    out: list[BrokerTrade] = []
    for pos_id, ds in groups.items():
        ds.sort(key=lambda d: (d.time_msc or d.time * 1000, d.ticket))
        ins = [d for d in ds if d.entry == _ENTRY_IN]
        outs = [d for d in ds if d.entry in (_ENTRY_OUT, _ENTRY_OUT_BY)]
        if not ins:
            continue
        first, last = ins[0], (outs[-1] if outs else None)
        money = sum((d.profit or 0.0) + (d.commission or 0.0)
                    + (d.swap or 0.0) + (getattr(d, "fee", 0.0) or 0.0) for d in ds)
        out.append(BrokerTrade(
            account=str(terminal.login),
            position_id=str(pos_id),
            broker_symbol=first.symbol,
            symbol=SYMBOL_MAP[first.symbol],
            side="buy" if first.type == _DEAL_BUY else "sell",
            volume=float(sum(d.volume for d in ins)),
            open_time=to_ms(first.time),
            open_price=float(first.price),
            close_time=to_ms(last.time) if last else None,
            close_price=float(last.price) if last else None,
            sl=None, tp=None,     # deals do not carry them; positions_get does
            commission=float(sum(d.commission or 0.0 for d in ds)),
            swap=float(sum(d.swap or 0.0 for d in ds)),
            profit=round(money - sum((d.commission or 0.0) + (d.swap or 0.0) for d in ds), 6),
        ))

    # Live positions carry the current SL/TP; the deal history does not.
    try:
        live = api.positions_get()
    except Exception:
        live = None
    sltp = {str(p.ticket): (p.sl or None, p.tp or None) for p in (live or [])}
    if sltp:
        out = [replace(t, sl=sltp[t.position_id][0], tp=sltp[t.position_id][1])
               if t.position_id in sltp else t
               for t in out]

    out.sort(key=lambda t: t.open_time)
    return out


def newest_stored(db: Database, account: str | None = None) -> int | None:
    sql = "SELECT MAX(open_time) m FROM broker_trades"
    args: tuple = ()
    if account:
        sql += " WHERE account = ?"
        args = (account,)
    row = db.conn.execute(sql, args).fetchone()
    return int(row["m"]) if row and row["m"] is not None else None


def sync(db: Database, full: bool = False, lookback_days: int = 7) -> tuple[Terminal, int]:
    """Pull history into ``broker_trades``. Incremental unless ``full``.

    The incremental window reaches back a few days rather than to the newest
    stored open time, because a position opened last week and closed today would
    otherwise never be updated with its exit.
    """
    from .mt5 import store

    api, terminal = connect()
    try:
        since = None
        if not full:
            newest = newest_stored(db, str(terminal.login))
            if newest is not None:
                since = newest - lookback_days * 86_400_000
        trades = fetch_positions(api, terminal, since_ms=since)
        n = store(db, trades, terminal.offset_hours) if trades else 0
    finally:
        api.shutdown()
    return terminal, n
