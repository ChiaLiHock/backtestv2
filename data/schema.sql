-- backtest/data/schema.sql
-- SQLite schema. All timestamps are UTC epoch MILLISECONDS (INTEGER), never text, never local.
-- Every statement is idempotent so this file can be replayed on an existing database.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Market data
-- ---------------------------------------------------------------------------

-- WITHOUT ROWID: the primary key IS the storage order, which is exactly the
-- access pattern (symbol, interval, time-ascending scan).
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,   -- canonical: 1m 5m 15m 30m 1h 4h 1d
    open_time   INTEGER NOT NULL,   -- bar OPEN, UTC epoch ms
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    turnover    REAL    NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_candles_time ON candles(symbol, interval, open_time);

CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT PRIMARY KEY,
    tick_size   REAL,
    qty_step    REAL,
    min_qty     REAL,
    raw_json    TEXT,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS funding (
    symbol       TEXT    NOT NULL,
    funding_time INTEGER NOT NULL,
    rate         REAL    NOT NULL,
    PRIMARY KEY (symbol, funding_time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS open_interest (
    symbol   TEXT    NOT NULL,
    interval TEXT    NOT NULL,
    ts       INTEGER NOT NULL,
    oi       REAL    NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sync_state (
    symbol         TEXT    NOT NULL,
    interval       TEXT    NOT NULL,
    last_open_time INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- Backtest results  (PHASE 3 writes these; created here so one schema file owns
-- the whole database and a fresh install needs exactly one migration step)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    created_at       INTEGER NOT NULL,
    config_yaml      TEXT    NOT NULL,
    config_hash      TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    timeframe        TEXT    NOT NULL,
    start_ms         INTEGER NOT NULL,
    end_ms           INTEGER NOT NULL,
    code_version     TEXT    NOT NULL,
    data_fingerprint TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_hash ON runs(config_hash);

CREATE TABLE IF NOT EXISTS trades (
    trade_id    TEXT PRIMARY KEY,
    run_id      TEXT    NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,   -- long | short
    signal_id   TEXT,               -- which setup fired
    entry_time  INTEGER NOT NULL,
    entry_price REAL    NOT NULL,
    qty         REAL    NOT NULL,
    exit_time   INTEGER,
    exit_price  REAL,
    exit_reason TEXT,               -- tp | sl | time_stop | end_of_data
    gross_pnl   REAL,
    fees        REAL,
    funding     REAL,
    net_pnl     REAL,
    mae         REAL,               -- max adverse excursion, $
    mfe         REAL,               -- max favourable excursion, $
    bars_held   INTEGER,
    ambiguous   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_run  ON trades(run_id);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(run_id, entry_time);

-- Indicator snapshot as of the entry bar t (CLOSED). Labels go in value_txt,
-- numerics in value_num. This is the table the win-rate mining reads.
CREATE TABLE IF NOT EXISTS trade_features (
    trade_id  TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
    feature   TEXT NOT NULL,
    value_num REAL,
    value_txt TEXT,
    PRIMARY KEY (trade_id, feature)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_features_name ON trade_features(feature);

-- Async job tracking for the PHASE 5 API.
CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    kind       TEXT    NOT NULL,   -- sync | backtest
    status     TEXT    NOT NULL,   -- queued | running | done | error
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    payload    TEXT,
    result     TEXT,
    error      TEXT
);

-- ---------------------------------------------------------------------------
-- Key-level (support/resistance) snapshots
-- ---------------------------------------------------------------------------

-- One row per H1 bar: the KEY LEVELS card as it stood at that bar's close.
-- Zones are window-dependent (the app rebuilds from a trailing 500-bar buffer),
-- so they cannot be derived by indexing into a single full-history pass — each
-- snapshot is a genuine recompute at ~3 ms. A closed bar's snapshot depends only
-- on closed bars, so it never changes and caching it is exact, not an
-- approximation. `config_key` covers the inputs that feed zone construction, so
-- changing the window or the swing separation invalidates rather than serving
-- stale rows. See backtest/indicators/zones.py.
CREATE TABLE IF NOT EXISTS zone_snapshots (
    symbol        TEXT    NOT NULL,
    config_key    TEXT    NOT NULL,
    ts            INTEGER NOT NULL,   -- H1 bar open_time, UTC epoch ms
    price         REAL    NOT NULL,   -- H1 CONFIRMED close: the app's "Current"
    reference_atr REAL,
    zones_json    TEXT    NOT NULL,
    built_at      INTEGER NOT NULL,
    PRIMARY KEY (symbol, config_key, ts)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- Real fills imported from a broker report
-- ---------------------------------------------------------------------------

-- Your actual trades, so they can be drawn against the same candles as the
-- backtest. Times are stored UTC epoch ms like everything else; `tz_offset_s`
-- records the broker-server offset they were converted FROM, because MT5 exports
-- carry a wall clock with no zone marker and that offset is a measurement, not a
-- constant (see backtest/data/mt5.py). Prices are the fills as they happened and
-- are never adjusted toward the exchange's contract.
CREATE TABLE IF NOT EXISTS broker_trades (
    account       TEXT    NOT NULL,
    position_id   TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,   -- mapped exchange symbol, e.g. XAUUSDT
    broker_symbol TEXT    NOT NULL,   -- as written by the broker, e.g. GOLD
    side          TEXT    NOT NULL,   -- buy | sell
    volume        REAL    NOT NULL,   -- lots
    open_time     INTEGER NOT NULL,
    open_price    REAL    NOT NULL,
    close_time    INTEGER,            -- NULL while the position is still open
    close_price   REAL,
    sl            REAL,
    tp            REAL,
    commission    REAL,
    swap          REAL,
    profit        REAL,
    tz_offset_s   INTEGER NOT NULL,
    imported_at   INTEGER NOT NULL,
    PRIMARY KEY (account, position_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_broker_trades_sym
    ON broker_trades(symbol, open_time);
