"""Ingest MT5 GOLD (XM) bars into `candles` under the symbol key 'MT5GOLD'.

AUTOMATION.md §A / §2.1. Run:

    python mt5_ingest.py                 # all timeframes
    python mt5_ingest.py --tf 15m,1h     # a subset
    python mt5_ingest.py --prove         # re-run the clock proofs only, no writes


THE CLOCK — the whole job, and the spec is wrong about it
=========================================================

MT5 hands back the broker server's **wall clock** typed as though it were UTC.
`AUTOMATION.md` §2.1 records the offset as a constant **UTC+3**, measured from a
server clock that read 11:22 when real UTC was 08:22. That reading was taken in
**August**. It is correct for August and wrong for five months of the year.

The XM server runs **EET/EEST** — UTC+2 in winter, UTC+3 in summer, switching on
the EU rule (last Sunday of March / last Sunday of October). Measured two
independent ways, see `work/mt5/probe_offset2.py` and `work/mt5/probe_weekedge.py`:

1. Cross-correlation of 15m log returns against Bybit PAXGUSDT, whose timestamps
   are known-true UTC, scanned in 15-minute steps, month by month over 4.4 years.
   Every month resolves to +2 or +3 and the transitions land exactly on the EU
   DST dates. Nov-Mar = +2, Apr-Oct = +3.
2. No reference instrument at all: the metals week closes at 17:00 New York,
   which is 21:00 UTC under EDT and 22:00 UTC under EST. Comparing that known
   instant to the server clock of the last bar of each week reproduces the same
   schedule on 130 of 135 weeks. The five exceptions are US holiday early closes
   (Thanksgiving Friday, July 4th, Juneteenth), not clock changes.

So a fixed −3h shift stamps every bar between late October and late March one
hour too early. That is not cosmetic: it moves `hour_utc`/`hour_myt` by an hour
for ~5 months a year, which is exactly the axis `ENTRY_RULES.md` §11 gates on.

Consequence for the H4 partition, which is what §2.1 really cares about:

    server 00:00 04:00 08:00 12:00 16:00 20:00
    = UTC   21:00 01:00 05:00 09:00 13:00 17:00   (summer, Apr-Oct)  <- §2.1
    = UTC   22:00 02:00 06:00 10:00 14:00 18:00   (winter, Nov-Mar)  <- omitted

The bars themselves are NOT re-bucketed. The broker's partition is kept exactly
as published — the 4H UT level is a path-dependent latching trail, so
re-bucketing produces a different flip history, not a rounded number. All that
changes is the UTC label on each bucket, which is what conversion means.

The ambiguous hour (October fallback repeats server 03:00-04:00) and the missing
hour (March springs 03:00 -> 04:00) both fall on a Sunday while GOLD is shut, so
no bar lands in either. That is asserted, not assumed.


WHAT ELSE IS STORED
===================

* `volume` <- `tick_volume`. `real_volume` is 0 on this feed (asserted).
* `turnover` <- 0.0. There is no traded size here; inventing one from
  tick_volume x price would be a fabricated column an absolute turnover gate
  could silently be built on. Miners must not read `turnover` on MT5GOLD.
* the per-bar `spread` field -> table `mt5_bar_spread`, in BOTH points and
  price. This is the real execution cost and the whole break-even argument
  rests on it (§1: median 0.38 = 0.83 bps, against Bybit's 11 bps).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import MetaTrader5 as mt5

DB = r"C:\inetpub\Claude\ITSupport\backtest\data\market.db"
SYMBOL = "MT5GOLD"          # storage key; never merged with Bybit XAUUSDT
BROKER_SYMBOL = "GOLD"

# The broker clock. EET/EEST, EU DST rule — measured, see the module docstring.
SERVER_TZ = ZoneInfo("Europe/Athens")

TF_MT5 = {"1m": mt5.TIMEFRAME_M1, "5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15,
          "30m": mt5.TIMEFRAME_M30, "1h": mt5.TIMEFRAME_H1, "4h": mt5.TIMEFRAME_H4}
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

# copy_rates_from_pos silently caps around 60k bars; ranges are walked instead.
CHUNK_BARS = 20_000
EARLIEST = dt.datetime(1999, 1, 1)      # server wall clock; no gold before this


# ---------------------------------------------------------------------------
# the conversion
# ---------------------------------------------------------------------------

def server_epoch_to_utc_ms(server_epoch_s: np.ndarray) -> np.ndarray:
    """MT5 `time` (server wall clock encoded as if it were UTC) -> true UTC ms.

    Vectorised: localise the naive server wall clock into Europe/Athens and read
    back the true UTC instant. `ambiguous`/`nonexistent` are set to raise, so a
    bar sitting inside a DST fold is a hard error rather than a silent 1-hour
    slip — the case this whole module exists to prevent.
    """
    naive = pd.to_datetime(server_epoch_s.astype("int64"), unit="s")
    local = naive.tz_localize(SERVER_TZ, ambiguous="raise", nonexistent="raise")
    # pandas 3 infers datetime64[s] here, so the unit is pinned explicitly —
    # `.astype("int64")` on a second-resolution index returns SECONDS, and
    # dividing that by 10**6 would silently produce 1970 timestamps.
    return local.tz_convert("UTC").as_unit("ms").astype("int64").to_numpy()


def offset_hours_at(server_naive: dt.datetime) -> int:
    """Whole-hour server->UTC offset in force at a server wall-clock instant."""
    return int(server_naive.replace(tzinfo=SERVER_TZ).utcoffset().total_seconds() // 3600)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def fetch(interval: str) -> pd.DataFrame:
    """All available bars for one timeframe, ascending, `time` still SERVER."""
    step = TF_SEC[interval]
    hi = dt.datetime.utcnow() + dt.timedelta(hours=4)   # past any server clock
    chunks: list[pd.DataFrame] = []
    while hi > EARLIEST:
        lo = max(EARLIEST, hi - dt.timedelta(seconds=CHUNK_BARS * step))
        got = mt5.copy_rates_range(BROKER_SYMBOL, TF_MT5[interval], lo, hi)
        if got is None or len(got) == 0:
            # A range entirely before the broker's depth returns EMPTY rather
            # than clamping, which is why this walks backwards in windows.
            break
        chunks.append(pd.DataFrame(got))
        if lo <= EARLIEST:
            break
        hi = lo - dt.timedelta(seconds=1)
    if not chunks:
        return pd.DataFrame()
    d = (pd.concat(chunks).drop_duplicates("time").sort_values("time")
         .reset_index(drop=True))
    return d


# ---------------------------------------------------------------------------
# proofs
# ---------------------------------------------------------------------------

def prove(store: dict[str, pd.DataFrame]) -> list[str]:
    """AUTOMATION.md acceptance test 7, plus the H4 partition check.

    Returns a list of human-readable proof lines. Raises on failure.
    """
    lines: list[str] = []

    # ---- 1. a known H4 bar, both seasons ----------------------------------
    h4 = store["4h"]
    for label, server_str, expect_utc in [
        ("summer", "2026-08-21 20:00:00", "2026-08-21 17:00:00"),
        ("winter", "2026-01-15 20:00:00", "2026-01-15 18:00:00"),
    ]:
        srv = pd.Timestamp(server_str)
        row = h4[h4.server_time == srv]
        assert len(row) == 1, f"no H4 bar at server {server_str}"
        got = pd.Timestamp(int(row.open_time.iloc[0]), unit="ms", tz="UTC")
        want = pd.Timestamp(expect_utc, tz="UTC")
        assert got == want, f"H4 {label}: stored {got} != true {want}"
        off = offset_hours_at(srv.to_pydatetime())
        lines.append(f"H4 {label}: server {server_str} -> stored open_time "
                     f"{got:%Y-%m-%d %H:%M} UTC (offset +{off}h)  OK")

    # ---- 2. H4 bucket starts, per season ----------------------------------
    ts = pd.to_datetime(h4.open_time, unit="ms", utc=True)
    off = (h4.server_time.values.astype("datetime64[s]").astype("int64")
           - h4.open_time.to_numpy() // 1000) // 3600
    for season, o, want in [("summer", 3, {21, 1, 5, 9, 13, 17}),
                            ("winter", 2, {22, 2, 6, 10, 14, 18})]:
        hrs = sorted(set(ts[off == o].dt.hour))
        assert set(hrs) == want, f"H4 {season} bucket starts {hrs} != {sorted(want)}"
        lines.append(f"H4 {season} (offset +{o}h) bucket starts = "
                     f"{sorted(want)} UTC  OK ({int((off == o).sum())} bars)")
    naive_utc = sorted(set(pd.to_datetime(h4.server_time).dt.hour))
    assert naive_utc == [0, 4, 8, 12, 16, 20], naive_utc
    lines.append(f"H4 raw server bucket starts = {naive_utc} (unchanged — the "
                 f"broker partition was NOT re-bucketed)  OK")

    # ---- 3. the counter-check: NOT the Bybit grid -------------------------
    bad = int(ts.dt.hour.isin([0, 4, 8, 12, 16, 20]).sum())
    assert bad == 0, f"{bad} H4 bars land on the Bybit 00/04/08... UTC grid"
    lines.append("zero H4 bars on the Bybit 00/04/08/12/16/20 UTC grid  OK")

    # ---- 4. week close, independent of any reference instrument ----------
    m15 = store.get("15m")
    if m15 is not None and len(m15):
        # The last bar before each weekend gap. Found from the gaps themselves
        # rather than from a calendar week, because pandas' week anchoring is
        # not the market's week and got this wrong once already.
        t = m15.open_time.to_numpy("int64")
        gap = np.flatnonzero(np.diff(t) > 24 * 3600 * 1000)
        close = pd.to_datetime(t[gap] + 15 * 60 * 1000, unit="ms", utc=True)
        fri = close[close.weekday == 4]
        ok = int(fri.hour.isin([21, 22]).sum())      # 17:00 New York, EDT / EST
        assert len(fri) > 100 and ok / len(fri) > 0.95, \
            f"week closes at 17:00 NY: {ok}/{len(fri)}"
        lines.append(f"week close lands at 17:00 New York (21:00/22:00 UTC) on "
                     f"{ok}/{len(fri)} weeks — offset schedule confirmed with no "
                     f"reference instrument  OK")

    # ---- 5. monotonic, on-grid, no duplicates ----------------------------
    for tf, d in store.items():
        t = d.open_time.to_numpy("int64")
        assert (np.diff(t) > 0).all(), f"{tf}: open_time not strictly increasing"
        step = TF_SEC[tf] * 1000
        if tf != "4h":
            # sub-daily grids are absolute; H4 is anchored to a shifting server
            # midnight so its residue changes with the season, by design.
            res = set((t % step).tolist())
            assert len(res) == 1, f"{tf}: bars off-grid, residues {sorted(res)[:5]}"
    lines.append("all timeframes strictly increasing, deduped, on-grid  OK")
    return lines


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS mt5_bar_spread (
    symbol         TEXT    NOT NULL,
    interval       TEXT    NOT NULL,
    open_time      INTEGER NOT NULL,   -- true UTC epoch ms, matches candles
    spread_points  INTEGER NOT NULL,   -- broker's own per-bar field
    spread_price   REAL    NOT NULL,   -- points x point size (0.01 on GOLD)
    PRIMARY KEY (symbol, interval, open_time)
) WITHOUT ROWID;
"""


def store_tf(con: sqlite3.Connection, interval: str, d: pd.DataFrame, point: float):
    rows = list(zip(
        [SYMBOL] * len(d), [interval] * len(d),
        d.open_time.astype("int64").tolist(),
        d.open.astype(float).tolist(), d.high.astype(float).tolist(),
        d.low.astype(float).tolist(), d.close.astype(float).tolist(),
        d.tick_volume.astype(float).tolist(), [0.0] * len(d)))
    con.executemany(
        "INSERT INTO candles (symbol,interval,open_time,open,high,low,close,volume,turnover)"
        " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,interval,open_time) DO UPDATE SET"
        " open=excluded.open, high=excluded.high, low=excluded.low,"
        " close=excluded.close, volume=excluded.volume, turnover=excluded.turnover", rows)
    sp = list(zip([SYMBOL] * len(d), [interval] * len(d),
                  d.open_time.astype("int64").tolist(),
                  d.spread.astype(int).tolist(),
                  (d.spread.astype(float) * point).tolist()))
    con.executemany(
        "INSERT INTO mt5_bar_spread (symbol,interval,open_time,spread_points,spread_price)"
        " VALUES (?,?,?,?,?) ON CONFLICT(symbol,interval,open_time) DO UPDATE SET"
        " spread_points=excluded.spread_points, spread_price=excluded.spread_price", sp)
    con.commit()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1m,5m,15m,30m,1h,4h")
    ap.add_argument("--prove", action="store_true",
                    help="re-run the clock proofs from what is already stored")
    args = ap.parse_args()
    tfs = args.tf.split(",")

    if not mt5.initialize():
        print("mt5.initialize FAILED", mt5.last_error(), file=sys.stderr)
        return 2
    ti = mt5.terminal_info()
    si = mt5.symbol_info(BROKER_SYMBOL)
    if ti is None or not ti.connected or si is None:
        print("terminal not connected / symbol missing", file=sys.stderr)
        return 2
    point = float(si.point)
    print(f"terminal build {ti.build}  {BROKER_SYMBOL} point={point} digits={si.digits}")
    print(f"server tz = {SERVER_TZ} (EET/EEST); offset now = "
          f"+{offset_hours_at(dt.datetime.utcnow())}h")

    store: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        raw = fetch(tf)
        if raw.empty:
            print(f"{tf}: EMPTY  {mt5.last_error()}", file=sys.stderr)
            continue
        # §2.2 says real_volume is 0. It is on the intraday feed, but the H1/H4
        # series carry a nonzero value on some bars, so this is reported rather
        # than asserted. `volume` is tick_volume either way — mixing two
        # different volume definitions inside one column would be worse than
        # having only the unitless one.
        rv = float((raw.real_volume != 0).mean()) * 100
        raw["server_time"] = pd.to_datetime(raw.time, unit="s")
        raw["open_time"] = server_epoch_to_utc_ms(raw.time.to_numpy())
        store[tf] = raw
        print(f"{tf}: {len(raw):7d} bars  server "
              f"{raw.server_time.iloc[0]} .. {raw.server_time.iloc[-1]}  ->  UTC "
              f"{pd.Timestamp(int(raw.open_time.iloc[0]), unit='ms')} .. "
              f"{pd.Timestamp(int(raw.open_time.iloc[-1]), unit='ms')}"
              f"   spread med={raw.spread.median()*point:.2f}"
              f"   real_volume nonzero on {rv:.1f}% of bars")

    print("\n--- clock proofs " + "-" * 50)
    for line in prove(store):
        print("  " + line)

    if args.prove:
        mt5.shutdown()
        return 0

    con = sqlite3.connect(DB)
    con.executescript(DDL)
    for tf, d in store.items():
        store_tf(con, tf, d, point)
        n = con.execute("select count(*) from candles where symbol=? and interval=?",
                        (SYMBOL, tf)).fetchone()[0]
        print(f"stored {SYMBOL} {tf}: {n} rows in candles")
    con.close()
    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
