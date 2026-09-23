"""SQLite repository layer.

Every SQL statement in the project lives here or in a sibling repository class, so
swapping to PostgreSQL/TimescaleDB later touches this module only. Nothing outside
`backtest.data` imports `sqlite3`.

All timestamps crossing this boundary are UTC epoch milliseconds (int).
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "market.db"

# Canonical interval vocabulary. The Bybit wire codes are a separate mapping
# (see bybit_client.INTERVAL_TO_BYBIT) so the DB never stores a vendor code.
INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

CANDLE_COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "turnover")


class Database:
    """Thin connection owner. Not thread-safe by design; make one per worker."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA foreign_keys = ON")
        # 64 MB page cache; sync of a multi-year 1m series is write-heavy.
        cur.execute("PRAGMA cache_size = -65536")
        cur.close()

    def migrate(self) -> None:
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        """Explicit transaction. isolation_level=None means we drive BEGIN ourselves."""
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
        except Exception:
            cur.execute("ROLLBACK")
            raise
        else:
            cur.execute("COMMIT")
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """A hole in a candle series: bars are missing in [start_ms, end_ms]."""

    start_ms: int
    end_ms: int
    missing_bars: int


class CandleRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, symbol: str, interval: str, rows: Sequence[Sequence[Any]]) -> int:
        """Idempotent insert keyed on (symbol, interval, open_time).

        `rows` are (open_time, open, high, low, close, volume, turnover) tuples.
        Returns the number of rows offered (not the number changed — SQLite does
        not report that usefully for an upsert of identical values).
        """
        if not rows:
            return 0
        payload = [(symbol, interval, *r) for r in rows]
        with self.db.tx() as cur:
            cur.executemany(
                """
                INSERT INTO candles
                    (symbol, interval, open_time, open, high, low, close, volume, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, open_time) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    turnover = excluded.turnover
                """,
                payload,
            )
        return len(payload)

    def load(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> pd.DataFrame:
        """Ascending-by-time OHLCV frame indexed 0..n-1 with an `open_time` column.

        `end_ms` is INCLUSIVE of the bar whose open_time equals it.
        """
        sql = [
            "SELECT open_time, open, high, low, close, volume, turnover",
            "FROM candles WHERE symbol = ? AND interval = ?",
        ]
        params: list[Any] = [symbol, interval]
        if start_ms is not None:
            sql.append("AND open_time >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            sql.append("AND open_time <= ?")
            params.append(int(end_ms))
        sql.append("ORDER BY open_time ASC")

        df = pd.read_sql_query(" ".join(sql), self.db.conn, params=params)
        if df.empty:
            return pd.DataFrame(columns=list(CANDLE_COLUMNS))
        df["open_time"] = df["open_time"].astype("int64")
        for col in ("open", "high", "low", "close", "volume", "turnover"):
            df[col] = df[col].astype("float64")
        return df.reset_index(drop=True)

    def load_tail(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        """The newest ``n`` bars, ascending. Same shape as ``load``.

        ``load(...).tail(n)`` reads every row into pandas and throws almost all
        of them away. That is fine on a 4,000-bar Bybit series and is not fine on
        `MT5:GOLD`, where the 1m and 30m tables hold 100,000 rows each and the
        live loop touches five timeframes per symbol every five seconds.

        `ORDER BY open_time DESC LIMIT n` walks the (symbol, interval, open_time)
        index backwards and stops, so the cost is the rows returned rather than
        the rows stored. The result is re-sorted ascending because every consumer
        assumes ascending time.
        """
        if n <= 0:
            return pd.DataFrame(columns=list(CANDLE_COLUMNS))
        df = pd.read_sql_query(
            "SELECT open_time, open, high, low, close, volume, turnover "
            "FROM candles WHERE symbol = ? AND interval = ? "
            "ORDER BY open_time DESC LIMIT ?",
            self.db.conn, params=(symbol, interval, int(n)),
        )
        if df.empty:
            return pd.DataFrame(columns=list(CANDLE_COLUMNS))
        df = df.iloc[::-1].reset_index(drop=True)
        df["open_time"] = df["open_time"].astype("int64")
        for col in ("open", "high", "low", "close", "volume", "turnover"):
            df[col] = df[col].astype("float64")
        return df

    def coverage(self, symbol: str, interval: str) -> tuple[int | None, int | None, int]:
        """(first_open_time, last_open_time, count)."""
        row = self.db.conn.execute(
            "SELECT MIN(open_time) a, MAX(open_time) b, COUNT(*) c "
            "FROM candles WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        return (row["a"], row["b"], row["c"] or 0)

    def find_gaps(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Gap]:
        """Bars missing from the stored series, judged against the interval's own spacing.

        This detects holes BETWEEN stored bars. It cannot know whether the range
        extends beyond what was ever fetched — that is `coverage`'s job.

        Note: XAUUSDT is a perpetual and trades 24/7, so on this instrument every
        interval boundary is expected to carry a bar. A spot instrument would need
        a session calendar here instead.
        """
        step = INTERVAL_MS[interval]
        df = self.load(symbol, interval, start_ms, end_ms)
        if len(df) < 2:
            return []
        times = df["open_time"].to_numpy()
        deltas = times[1:] - times[:-1]
        gaps: list[Gap] = []
        for i, delta in enumerate(deltas):
            if delta > step:
                missing = int(delta // step) - 1
                gaps.append(
                    Gap(
                        start_ms=int(times[i]) + step,
                        end_ms=int(times[i + 1]) - step,
                        missing_bars=missing,
                    )
                )
        return gaps

    def fingerprint(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> str:
        """Stable hash of the exact data a run consumed.

        Recorded on every run so a later re-run over re-synced data can be told
        apart from a true reproduction.
        """
        row = self.db.conn.execute(
            "SELECT COUNT(*) c, MIN(open_time) a, MAX(open_time) b, "
            "       COALESCE(SUM(close), 0) s "
            "FROM candles WHERE symbol = ? AND interval = ? "
            "  AND open_time >= ? AND open_time <= ?",
            (symbol, interval, int(start_ms), int(end_ms)),
        ).fetchone()
        blob = f"{symbol}|{interval}|{row['c']}|{row['a']}|{row['b']}|{row['s']:.6f}"
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Instruments / funding / open interest / sync state
# ---------------------------------------------------------------------------


class MetaRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_instrument(
        self,
        symbol: str,
        tick_size: float | None,
        qty_step: float | None,
        min_qty: float | None,
        raw_json: str,
        updated_at: int,
    ) -> None:
        with self.db.tx() as cur:
            cur.execute(
                """
                INSERT INTO instruments (symbol, tick_size, qty_step, min_qty, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    tick_size = excluded.tick_size,
                    qty_step = excluded.qty_step,
                    min_qty = excluded.min_qty,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (symbol, tick_size, qty_step, min_qty, raw_json, updated_at),
            )

    def get_instrument(self, symbol: str) -> sqlite3.Row | None:
        return self.db.conn.execute(
            "SELECT * FROM instruments WHERE symbol = ?", (symbol,)
        ).fetchone()

    def upsert_funding(self, symbol: str, rows: Iterable[tuple[int, float]]) -> int:
        payload = [(symbol, int(t), float(r)) for t, r in rows]
        if not payload:
            return 0
        with self.db.tx() as cur:
            cur.executemany(
                "INSERT INTO funding (symbol, funding_time, rate) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol, funding_time) DO UPDATE SET rate = excluded.rate",
                payload,
            )
        return len(payload)

    def load_funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT funding_time, rate FROM funding "
            "WHERE symbol = ? AND funding_time >= ? AND funding_time <= ? "
            "ORDER BY funding_time ASC",
            self.db.conn,
            params=(symbol, int(start_ms), int(end_ms)),
        )

    def upsert_open_interest(
        self, symbol: str, interval: str, rows: Iterable[tuple[int, float]]
    ) -> int:
        payload = [(symbol, interval, int(t), float(v)) for t, v in rows]
        if not payload:
            return 0
        with self.db.tx() as cur:
            cur.executemany(
                "INSERT INTO open_interest (symbol, interval, ts, oi) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol, interval, ts) DO UPDATE SET oi = excluded.oi",
                payload,
            )
        return len(payload)

    def load_open_interest(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT ts, oi FROM open_interest "
            "WHERE symbol = ? AND interval = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            self.db.conn,
            params=(symbol, interval, int(start_ms), int(end_ms)),
        )

    def newest_open_interest(self, symbol: str, interval: str) -> int | None:
        """Newest stored OI timestamp, or None if nothing is stored yet.

        The live sync resumes from here instead of a fixed lookback. A fixed
        window silently turns downtime into a PERMANENT hole: the watcher asks
        only for the last 50 hours, so if it was off for three days the missing
        middle is never requested again and no error is ever raised. Resuming
        from the newest row makes the request self-sizing -- a few points after
        a normal cycle, a few hundred after an outage.
        """
        row = self.db.conn.execute(
            "SELECT MAX(ts) AS ts FROM open_interest WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def open_interest_coverage(
        self, interval: str | None = None
    ) -> list[tuple[str, str, int, int, int]]:
        """(symbol, interval, rows, oldest_ts, newest_ts) per stored series."""
        sql = ("SELECT symbol, interval, COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi "
               "FROM open_interest ")
        params: tuple = ()
        if interval is not None:
            sql += "WHERE interval = ? "
            params = (interval,)
        sql += "GROUP BY symbol, interval ORDER BY symbol, interval"
        return [
            (r["symbol"], r["interval"], int(r["n"]), int(r["lo"]), int(r["hi"]))
            for r in self.db.conn.execute(sql, params).fetchall()
        ]

    def get_sync_state(self, symbol: str, interval: str) -> int | None:
        row = self.db.conn.execute(
            "SELECT last_open_time FROM sync_state WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        ).fetchone()
        return row["last_open_time"] if row else None

    def set_sync_state(
        self, symbol: str, interval: str, last_open_time: int, updated_at: int
    ) -> None:
        with self.db.tx() as cur:
            cur.execute(
                """
                INSERT INTO sync_state (symbol, interval, last_open_time, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                    last_open_time = MAX(sync_state.last_open_time, excluded.last_open_time),
                    updated_at = excluded.updated_at
                """,
                (symbol, interval, int(last_open_time), int(updated_at)),
            )
