"""Import a MetaTrader 5 "Trade History Report" .xlsx into the database.

The point is to put your *actual* fills on the same chart as the backtest, so
"what was true when I clicked" is a question you can answer by pointing at a bar
rather than by remembering.

Two things have to be got right or the overlay is worse than useless:

**The timestamps are not UTC.** MT5 reports carry the *broker server's* wall
clock with no zone marker anywhere in the file. XM Global runs EET/EEST, i.e.
UTC+2 in winter and UTC+3 in summer, but nothing in the export says so and other
brokers differ. Guessing wrong puts every fill on the wrong bar, and the result
still looks plausible — which is the dangerous kind of wrong. So the offset is
**measured**: :func:`detect_offset` tries every whole-hour offset and picks the
one that minimises the distance between each fill price and the 1-minute candle
it would land on. On this account the answer is unambiguous (median error 3.4 at
+3h versus 9.0 at either neighbour), and it is reported rather than assumed.

**The instruments are not the same contract.** The broker's `GOLD` is spot
XAU/USD; Bybit's `XAUUSDT` is a perpetual. They track closely but not exactly —
measured here, spot sits about 3.4 points (0.076%) below the perp. Fills are
stored and drawn at the price you actually got, never adjusted to fit the chart;
:func:`basis_report` measures the gap so a marker sitting slightly off a candle
has an explanation instead of looking like a bug.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .db import CandleRepository, Database

# Broker instrument -> the Bybit contract this project stores. Anything absent is
# skipped rather than guessed at: mapping a symbol onto the wrong candles is the
# same class of error as getting the timezone wrong.
SYMBOL_MAP: dict[str, str] = {
    "GOLD": "XAUUSDT",
    "GOLD24-7": "XAUUSDT",     # XM's weekend gold book, same underlying
    "XAUUSD": "XAUUSDT",
    "BTCUSD": "BTCUSDT",
    "BTCUSDT": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
    "ETHUSDT": "ETHUSDT",
}

# Contract size per lot, so a fill can be expressed in the same units the engine
# uses. XM gold is 100 oz/lot, so 0.01 lot = 1 oz = the backtest's default qty 1.
CONTRACT_SIZE: dict[str, float] = {
    "GOLD": 100.0, "GOLD24-7": 100.0, "XAUUSD": 100.0,
    "BTCUSD": 1.0, "BTCUSDT": 1.0,
    "ETHUSD": 1.0, "ETHUSDT": 1.0,
}

_TIME_FMT = "%Y.%m.%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class BrokerTrade:
    account: str
    position_id: str
    broker_symbol: str
    symbol: str
    side: str
    volume: float
    open_time: int
    open_price: float
    close_time: int | None
    close_price: float | None
    sl: float | None
    tp: float | None
    commission: float
    swap: float
    profit: float | None

    @property
    def units(self) -> float:
        return self.volume * CONTRACT_SIZE.get(self.broker_symbol, 1.0)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace(",", ""))
    except ValueError:
        return None


def _parse_naive(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.strptime(str(v).strip(), _TIME_FMT)
    except ValueError:
        return None


def _sections(rows: list[tuple]) -> dict[str, tuple[int, int]]:
    """Row spans of the report's sections, by their single-cell heading."""
    marks: list[tuple[int, str]] = []
    for i, r in enumerate(rows):
        cells = [("" if x is None else str(x).strip()) for x in r]
        filled = [c for c in cells if c]
        if len(filled) == 1 and cells[0] and not cells[0][0].isdigit():
            marks.append((i, cells[0]))
    out: dict[str, tuple[int, int]] = {}
    for k, (i, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(rows)
        out[name] = (i, end)
    return out


def read_report(path: str | Path) -> tuple[str, list[dict]]:
    """(account, raw rows) from the Positions and Open Positions sections.

    Times are left as naive datetimes here — they are broker-local and converting
    them is a separate, measured decision.
    """
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - dependency is declared
        raise SystemExit(
            "openpyxl is needed to read MT5 reports:\n"
            "  backtest\\.venv\\Scripts\\python.exe -m pip install openpyxl"
        ) from None

    wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    rows = list(wb[wb.sheetnames[0]].iter_rows(values_only=True))
    wb.close()

    account = ""
    for r in rows[:12]:
        cells = [("" if x is None else str(x).strip()) for x in r]
        if cells and cells[0].lower().startswith("account"):
            account = next((c for c in cells[1:] if c), "").split(" ")[0]
            break

    spans = _sections(rows)
    out: list[dict] = []

    # Closed positions: Time | Position | Symbol | Type | Volume | Price | S/L |
    #                   T/P | Time | Price | Commission | Swap | Profit
    if "Positions" in spans:
        lo, hi = spans["Positions"]
        for r in rows[lo + 2:hi]:
            opened = _parse_naive(r[0])
            if opened is None or not r[2]:
                continue
            out.append({
                "position_id": str(r[1]).strip(),
                "broker_symbol": str(r[2]).strip(),
                "side": str(r[3]).strip().lower(),
                "volume": _num(r[4]) or 0.0,
                "open_naive": opened,
                "open_price": _num(r[5]),
                "sl": _num(r[6]), "tp": _num(r[7]),
                "close_naive": _parse_naive(r[8]),
                "close_price": _num(r[9]),
                "commission": _num(r[10]) or 0.0,
                "swap": _num(r[11]) or 0.0,
                "profit": _num(r[12]),
            })

    # Still-open positions: Time | Position | Symbol | Type | Volume | Price |
    #                       S/L | T/P | Market Price | Swap | Profit | Comment
    if "Open Positions" in spans:
        lo, hi = spans["Open Positions"]
        for r in rows[lo + 2:hi]:
            opened = _parse_naive(r[0])
            if opened is None or not r[2]:
                continue
            out.append({
                "position_id": str(r[1]).strip(),
                "broker_symbol": str(r[2]).strip(),
                "side": str(r[3]).strip().lower(),
                "volume": _num(r[4]) or 0.0,
                "open_naive": opened,
                "open_price": _num(r[5]),
                "sl": _num(r[6]), "tp": _num(r[7]),
                "close_naive": None,
                "close_price": None,
                "commission": 0.0,
                "swap": _num(r[9]) or 0.0,
                "profit": _num(r[10]),
            })
    return account, out


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OffsetFit:
    offset_hours: int
    median_error: float
    runner_up_error: float
    matched: int

    @property
    def confident(self) -> bool:
        """The winner has to be clearly better, not merely first.

        A 2x separation is a large margin for this test: a wrong whole-hour
        offset on gold typically lands 10-30 points away, a right one lands
        within the basis.
        """
        return self.matched >= 20 and self.runner_up_error > self.median_error * 2.0

    def describe(self) -> str:
        return (f"UTC{self.offset_hours:+d} (median price error {self.median_error:.2f}, "
                f"next best {self.runner_up_error:.2f}, {self.matched} fills matched)")


def detect_offset(
    db: Database, raw: list[dict], candidates: Iterable[int] = range(-12, 13)
) -> OffsetFit | None:
    """Which whole-hour offset puts these fills on the right candles?

    Scored by the median absolute distance between a fill price and the close of
    the 1-minute candle it would land in. A constant basis between broker and
    exchange shifts every candidate equally, so it cannot bias the choice.
    """
    repo = CandleRepository(db)
    by_symbol: dict[str, list[tuple[datetime, float]]] = {}
    for r in raw:
        sym = SYMBOL_MAP.get(r["broker_symbol"])
        if sym is None or r["open_price"] is None:
            continue
        by_symbol.setdefault(sym, []).append((r["open_naive"], float(r["open_price"])))
    if not by_symbol:
        return None

    minute: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym in by_symbol:
        d = repo.load(sym, "1m")
        if not d.empty:
            minute[sym] = (d["open_time"].to_numpy(dtype="int64"),
                           d["close"].to_numpy())
    if not minute:
        return None

    scored: list[tuple[float, int, int]] = []
    for off in candidates:
        errs: list[float] = []
        for sym, fills in by_symbol.items():
            if sym not in minute:
                continue
            t, c = minute[sym]
            for when, px in fills:
                ms = int((when.replace(tzinfo=timezone.utc).timestamp() - off * 3600) * 1000)
                i = int(np.searchsorted(t, ms, "right")) - 1
                if i < 0 or i >= t.size or ms - t[i] > 120_000:
                    continue
                errs.append(abs(px - c[i]))
        if len(errs) >= 20:
            scored.append((float(np.median(errs)), off, len(errs)))
    if not scored:
        return None
    scored.sort()
    best, runner = scored[0], (scored[1] if len(scored) > 1 else scored[0])
    return OffsetFit(offset_hours=best[1], median_error=best[0],
                     runner_up_error=runner[0], matched=best[2])


def basis_report(db: Database, trades: list[BrokerTrade]) -> dict[str, dict]:
    """Median broker-minus-exchange price gap, per symbol.

    Fills are never adjusted by this. It exists so a marker that sits a little
    off the candles has a measured explanation.
    """
    repo = CandleRepository(db)
    out: dict[str, dict] = {}
    for sym in sorted({t.symbol for t in trades}):
        d = repo.load(sym, "1m")
        if d.empty:
            continue
        t = d["open_time"].to_numpy(dtype="int64")
        c = d["close"].to_numpy()
        diffs, prices = [], []
        for tr in trades:
            if tr.symbol != sym:
                continue
            i = int(np.searchsorted(t, tr.open_time, "right")) - 1
            if i < 0 or tr.open_time - t[i] > 120_000:
                continue
            diffs.append(tr.open_price - c[i])
            prices.append(tr.open_price)
        if not diffs:
            continue
        a = np.asarray(diffs)
        out[sym] = {
            "n": int(a.size),
            "median": round(float(np.median(a)), 4),
            "median_pct": round(float(np.median(a / np.asarray(prices)) * 100), 4),
            "p25": round(float(np.percentile(a, 25)), 4),
            "p75": round(float(np.percentile(a, 75)), 4),
        }
    return out


# ---------------------------------------------------------------------------
# Conversion + storage
# ---------------------------------------------------------------------------


def to_trades(account: str, raw: list[dict], offset_hours: int) -> list[BrokerTrade]:
    shift = offset_hours * 3600
    out: list[BrokerTrade] = []
    for r in raw:
        sym = SYMBOL_MAP.get(r["broker_symbol"])
        if sym is None or r["open_price"] is None:
            continue

        def ms(dtv: datetime | None) -> int | None:
            if dtv is None:
                return None
            return int((dtv.replace(tzinfo=timezone.utc).timestamp() - shift) * 1000)

        out.append(BrokerTrade(
            account=account or "unknown",
            position_id=r["position_id"],
            broker_symbol=r["broker_symbol"],
            symbol=sym,
            side=r["side"],
            volume=float(r["volume"]),
            open_time=ms(r["open_naive"]),          # type: ignore[arg-type]
            open_price=float(r["open_price"]),
            close_time=ms(r["close_naive"]),
            close_price=r["close_price"],
            sl=r["sl"], tp=r["tp"],
            commission=float(r["commission"]),
            swap=float(r["swap"]),
            profit=r["profit"],
        ))
    out.sort(key=lambda t: t.open_time)
    return out


def store(db: Database, trades: list[BrokerTrade], offset_hours: int) -> int:
    now = int(time.time() * 1000)
    with db.tx() as cur:
        cur.executemany(
            "INSERT OR REPLACE INTO broker_trades (account, position_id, symbol, "
            "broker_symbol, side, volume, open_time, open_price, close_time, "
            "close_price, sl, tp, commission, swap, profit, tz_offset_s, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(t.account, t.position_id, t.symbol, t.broker_symbol, t.side, t.volume,
              t.open_time, t.open_price, t.close_time, t.close_price, t.sl, t.tp,
              t.commission, t.swap, t.profit, offset_hours * 3600, now)
             for t in trades],
        )
    return len(trades)


def load(db: Database, symbol: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM broker_trades WHERE symbol = ? ORDER BY open_time",
        (symbol,),
    ).fetchall()
    return [dict(r) for r in rows]
