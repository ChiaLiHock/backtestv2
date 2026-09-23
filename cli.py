"""Command-line entry point.

    python -m backtest.cli sync --symbol XAUUSDT --tf 1m,5m,15m,30m,1h,4h --from 2024-01-01
    python -m backtest.cli coverage --symbol XAUUSDT
    python -m backtest.cli resolve
    python -m backtest.cli indicators --symbol XAUUSDT --tf 30m --tail 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .data.bybit_client import BybitClient, BybitError
from .data.db import INTERVAL_MS, CandleRepository, Database
from .data.sync import MarketDataSync

console = Console()

DEFAULT_SYMBOL = "XAUUSDT"
DEFAULT_TIMEFRAMES = "1m,5m,15m,30m,1h,4h"


def parse_time(value: str) -> int:
    """Accept 'now', an ISO date, or an ISO datetime. Returns epoch ms (UTC)."""
    v = value.strip()
    if v.lower() == "now":
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"cannot parse time {value!r} — use YYYY-MM-DD, an ISO datetime, or 'now'"
    )


def fmt(ms: int | None) -> str:
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z"


def _iso_z(ms: int) -> str:
    """Epoch ms -> the ISO form `engine.config.parse_time` round-trips."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timeframes(raw: str) -> list[str]:
    out = []
    for part in raw.split(","):
        tf = part.strip().lower()
        if not tf:
            continue
        if tf not in INTERVAL_MS:
            raise SystemExit(
                f"unknown timeframe {tf!r}; supported: {', '.join(INTERVAL_MS)}"
            )
        out.append(tf)
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_resolve(args: argparse.Namespace) -> int:
    """Confirm the contract exists before trusting a hardcoded string."""
    with BybitClient() as client:
        try:
            inst = client.resolve_symbol(args.symbol)
        except BybitError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
    table = Table(title=f"Resolved {inst.symbol}", show_header=False)
    table.add_row("symbol", inst.symbol)
    table.add_row("baseCoin", inst.base_coin)
    table.add_row("quoteCoin", inst.quote_coin)
    table.add_row("contractType", inst.contract_type)
    table.add_row("tickSize", str(inst.tick_size))
    table.add_row("qtyStep", str(inst.qty_step))
    table.add_row("minOrderQty", str(inst.min_qty))
    console.print(table)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    timeframes = _timeframes(args.tf)
    start_ms = parse_time(args.start)
    end_ms = parse_time(args.end)
    if end_ms <= start_ms:
        console.print("[red]--to must be after --from[/red]")
        return 1

    with Database(args.db) as db, BybitClient() as client:
        sync = MarketDataSync(client, db)
        try:
            symbol = sync.sync_instrument(args.symbol)
        except BybitError as exc:
            console.print(f"[red]instrument resolution failed: {exc}[/red]")
            return 1

        table = Table(title=f"Sync {symbol}")
        table.add_column("TF")
        table.add_column("Bars", justify="right")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Gaps", justify="right")

        failed = False
        for tf in timeframes:
            try:
                report = sync.sync_candles(
                    symbol, tf, start_ms, end_ms, resume=not args.no_resume
                )
            except BybitError as exc:
                console.print(f"[red]{tf}: {exc}[/red]")
                failed = True
                continue
            gap_text = (
                "[green]0[/green]"
                if report.ok
                else f"[yellow]{len(report.gaps)}[/yellow]"
            )
            table.add_row(
                tf,
                str(report.bars_written),
                fmt(report.first_open_time),
                fmt(report.last_open_time),
                gap_text,
            )
            if report.truncated_to_ms is not None:
                console.print(
                    f"[yellow]{tf}: requested from {fmt(start_ms)} but "
                    f"{symbol} has no data before {fmt(report.truncated_to_ms)} — "
                    f"range truncated.[/yellow]"
                )
        console.print(table)

        if args.funding:
            n = sync.sync_funding(symbol, start_ms, end_ms)
            console.print(f"funding rows: {n}")
        if args.open_interest:
            oi_tf = "1h" if "1h" in timeframes else timeframes[-1]
            n = sync.sync_open_interest(symbol, oi_tf, start_ms, end_ms)
            console.print(f"open-interest rows ({oi_tf}): {n}")

    return 1 if failed else 0


def cmd_oi(args: argparse.Namespace) -> int:
    """Backfill open interest to the source's retention limit, then report coverage.

    The watcher's own sync deliberately only walks FORWARD from the newest
    stored point, so it can never fill in history from before the first time it
    ran. That history is not permanently unavailable -- Bybit still serves
    ~170 days of 15m OI -- but it is only available *until* it ages out, which
    is why this exists as a command you can run once rather than a thing you
    have to have thought of in advance.

    Nothing is deleted and nothing is overwritten with a different value: the
    upsert is keyed on (symbol, interval, ts). Re-running it is therefore safe
    and is the right move after any long outage.
    """
    from .data.db import MetaRepository
    from .engine.risk_feed import OI_INTERVAL, OI_RETENTION_DAYS

    interval = args.interval
    symbols = [x.strip().upper() for x in args.symbol.split(",") if x.strip()]
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = now - args.days * 86_400_000

    with Database(args.db) as db, BybitClient() as client:
        sync = MarketDataSync(client, db)
        meta = MetaRepository(db)

        before = {(sym, iv): (n, lo, hi)
                  for sym, iv, n, lo, hi in meta.open_interest_coverage(interval)}

        table = Table(title=f"Open interest ({interval})")
        for col in ("Symbol", "Stored", "New", "Oldest", "Newest"):
            table.add_column(col, justify="right" if col in ("Stored", "New") else "left")

        failed = False
        for sym in symbols:
            try:
                sync.sync_open_interest(sym, interval, start, now)
            except BybitError as exc:
                console.print(f"[red]{sym}: {exc}[/red]")
                failed = True
                continue
            n0 = before.get((sym, interval), (0, None, None))[0]
            after = {(a, b): (n, lo, hi)
                     for a, b, n, lo, hi in meta.open_interest_coverage(interval)}
            n1, lo, hi = after.get((sym, interval), (0, None, None))
            table.add_row(sym, f"{n1:,}", f"+{n1 - n0:,}", fmt(lo), fmt(hi))

        console.print(table)
        console.print(
            f"[dim]Source retention is about {OI_RETENTION_DAYS} days on "
            f"{OI_INTERVAL}; anything older survives only because it was stored "
            f"before it aged out. This table is never pruned.[/dim]"
        )
    return 1 if failed else 0


def cmd_import_mt5(args: argparse.Namespace) -> int:
    """Import a MetaTrader 5 trade-history export."""
    from pathlib import Path

    from .data import mt5

    path = Path(args.file)
    if not path.exists():
        console.print(f"[red]no such file: {path}[/red]")
        return 1

    account, raw = mt5.read_report(path)
    if not raw:
        console.print("[red]no positions found — is this a Trade History Report?[/red]")
        return 1
    console.print(f"account [cyan]{account or '?'}[/cyan]  {len(raw)} positions in the file")

    known = [r for r in raw if r["broker_symbol"] in mt5.SYMBOL_MAP]
    skipped = sorted({r["broker_symbol"] for r in raw
                      if r["broker_symbol"] not in mt5.SYMBOL_MAP})
    if skipped:
        console.print(f"[dim]skipping unmapped symbols: {', '.join(skipped)}[/dim]")
    if not known:
        console.print("[red]none of these symbols map to a synced contract[/red]")
        return 1

    with Database(args.db) as db:
        if args.tz_offset is None:
            fit = mt5.detect_offset(db, known)
            if fit is None:
                console.print(
                    "[red]could not measure the broker's timezone — sync 1m candles "
                    "for these symbols first, or pass --tz-offset[/red]"
                )
                return 1
            console.print(f"broker clock measured as [cyan]{fit.describe()}[/cyan]")
            if not fit.confident:
                console.print(
                    "[yellow]That fit is NOT clear-cut. Every fill would land on the "
                    "wrong bar if it is wrong, and the result would still look "
                    "plausible. Check it, or pass --tz-offset explicitly.[/yellow]"
                )
            offset = fit.offset_hours
        else:
            offset = args.tz_offset
            console.print(f"broker clock taken as [cyan]UTC{offset:+d}[/cyan] (--tz-offset)")

        trades = mt5.to_trades(account, known, offset)
        n = mt5.store(db, trades, offset)

        table = Table(title=f"Imported {n} positions")
        for c in ("symbol", "broker", "n", "open", "closed", "net P/L", "first", "last"):
            table.add_column(c, justify="right" if c not in ("symbol", "broker") else "left")
        for sym in sorted({t.symbol for t in trades}):
            group = [t for t in trades if t.symbol == sym]
            live = [t for t in group if t.close_time is None]
            pnl = sum((t.profit or 0.0) + t.swap + t.commission for t in group)
            table.add_row(
                sym, ",".join(sorted({t.broker_symbol for t in group})),
                str(len(group)), str(len(live)), str(len(group) - len(live)),
                f"[{'green' if pnl > 0 else 'red'}]{pnl:,.2f}[/]",
                fmt(min(t.open_time for t in group)),
                fmt(max(t.open_time for t in group)),
            )
        console.print(table)

        basis = mt5.basis_report(db, trades)
        if basis:
            bt = Table(title="Broker vs exchange price basis (fills are NOT adjusted)")
            for c in ("symbol", "n", "median", "as %", "IQR"):
                bt.add_column(c, justify="right" if c != "symbol" else "left")
            for sym, b in basis.items():
                bt.add_row(sym, str(b["n"]), f"{b['median']:+.2f}",
                           f"{b['median_pct']:+.3f}%",
                           f"[{b['p25']:+.2f}, {b['p75']:+.2f}]")
            console.print(bt)
            console.print("[dim]Your broker's contract is not the exchange's. Markers are "
                          "drawn at the price you actually got, so they can sit slightly "
                          "off the candles by this amount.[/dim]")
    return 0


def cmd_sync_mt5(args: argparse.Namespace) -> int:
    """Pull trade history straight from the running MetaTrader 5 terminal."""
    from .data import mt5_live

    try:
        with Database(args.db) as db:
            terminal, n = mt5_live.sync(db, full=args.full,
                                        lookback_days=args.lookback_days)
    except mt5_live.Mt5Unavailable as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.print(f"[bold]{terminal.terminal}[/bold] build {terminal.build}")
    console.print(f"  account [cyan]{terminal.login}[/cyan] @ {terminal.server}  "
                  f"{terminal.currency} {terminal.balance:,.2f} "
                  f"(equity {terminal.equity:,.2f})")
    console.print(f"  server clock [cyan]UTC{terminal.offset_hours:+d}[/cyan] measured "
                  f"from {terminal.clock_symbol} ({terminal.clock_age_s:.0f}s old)")
    if not terminal.connected:
        console.print("[yellow]  terminal reports it is NOT connected to the broker — "
                      "history may be incomplete[/yellow]")
    console.print(f"  [green]{n}[/green] positions written")

    with Database(args.db) as db:
        rows = db.conn.execute(
            "SELECT symbol, COUNT(*) n, SUM(COALESCE(profit,0)+COALESCE(swap,0)"
            "+COALESCE(commission,0)) net, SUM(close_time IS NULL) live, "
            "MAX(open_time) last FROM broker_trades WHERE account = ? GROUP BY symbol",
            (str(terminal.login),),
        ).fetchall()
    table = Table(title=f"broker_trades · account {terminal.login}")
    for c in ("symbol", "n", "open", "net P/L", "newest"):
        table.add_column(c, justify="right" if c != "symbol" else "left")
    for r in rows:
        table.add_row(r["symbol"], str(r["n"]), str(r["live"]),
                      f"[{'green' if r['net'] > 0 else 'red'}]{r['net']:,.2f}[/]",
                      fmt(r["last"]))
    console.print(table)
    console.print("[dim]Read-only: this never places, modifies or closes anything, "
                  "and never handles your password — it attaches to the terminal you "
                  "already have open.[/dim]")
    return 0


def cmd_sync_mt5_rates(args: argparse.Namespace) -> int:
    """Pull the broker's own candles for the instrument actually traded."""
    from .data import mt5_live, mt5_rates

    intervals = _timeframes(args.tf)
    start_ms = parse_time(args.start) if args.start else None
    try:
        with Database(args.db) as db:
            terminal, reports = mt5_rates.sync(
                db, broker_symbol=args.symbol, intervals=intervals, start_ms=start_ms)
    except mt5_live.Mt5Unavailable as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.print(f"[bold]{terminal.terminal}[/bold]  server clock "
                  f"[cyan]UTC{terminal.offset_hours:+d}[/cyan] "
                  f"(from {terminal.clock_symbol}, {terminal.clock_age_s:.0f}s old)")
    stored = mt5_rates.stored_symbol(args.symbol)
    table = Table(title=f"{stored} — stored as true UTC, broker's own bar partition")
    for c in ("TF", "Bars", "From", "To", "Median spread", "Breaks", "Unexplained"):
        table.add_column(c, justify="right" if c != "TF" else "left")
    with Database(args.db) as db:
        for r in reports:
            g = mt5_rates.session_gaps(db, stored, r.interval) if r.bars else {}
            table.add_row(
                r.interval, str(r.bars), fmt(r.first_ms), fmt(r.last_ms),
                "—" if r.spread_median is None else f"{r.spread_median:.2f}",
                str(g.get("expected_breaks", "—")),
                ("[green]0[/green]" if not g.get("unexplained_count")
                 else f"[yellow]{g['unexplained_count']}[/yellow]"),
            )
    console.print(table)
    console.print("[dim]`Breaks` are weekend and rollover closes — this is a session "
                  "instrument, so those are correct, not missing data. Only "
                  "`Unexplained` would be a problem.[/dim]")
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    """Evaluate the rule and append to reports/signals.jsonl."""
    from pathlib import Path

    from .engine.live_signal import LiveSignal, RuleConfig

    sig = LiveSignal(RuleConfig(), log_path=Path(args.out) if args.out else None)
    with Database(args.db) as db:
        if args.backfill_days:
            since = int((datetime.now(timezone.utc).timestamp()
                         - args.backfill_days * 86400) * 1000)
            evals = sig.backfill(db, since_ms=since)
        elif args.all:
            evals = sig.backfill(db)
        else:
            evals = [sig.evaluate_latest(db)]

    written = sig.write(evals)
    fired = [e for e in evals if e.fired]
    entries = [e for e in evals if e.would_enter]
    console.print(f"evaluated [cyan]{len(evals)}[/cyan] closed bars, "
                  f"wrote [cyan]{written}[/cyan] new lines to {sig.log_path}")
    console.print(f"config [cyan]{sig.config_hash()}[/cyan]  "
                  f"{sig.cfg.side} TP{sig.cfg.tp_atr}/SL{sig.cfg.sl_atr} ATR, "
                  f"time stop {sig.cfg.time_stop_bars} bars, "
                  f"leg5 {'ON' if sig.cfg.use_5m_leg else 'OFF'}, "
                  f"{sig.cfg.lots} lots")

    if evals:
        span_days = (evals[-1].bar_open_ms - evals[0].bar_open_ms) / 86_400_000
        wk = span_days / 7 if span_days > 0 else float("nan")
        # Two different rates, and only the second is comparable with the
        # measured 2.7/week. `fired` counts every bar where the legs agree,
        # including those while a position is already open; the panel ratio is
        # 5.7:1 (15.1/week raw vs 2.66/week entries).
        console.print(
            f"over {span_days:.1f} days: [bold]{len(fired)}[/bold] bars with all "
            f"legs true ([bold]{len(fired)/wk:.1f}/week[/bold] raw), of which "
            f"[bold]{len(entries)}[/bold] would have opened a trade "
            f"([bold]{len(entries)/wk:.2f}/week[/bold])")
        exp = {1: 2.7, 2: 5.0, 3: 6.9, 4: 8.5}.get(sig.cfg.max_concurrent)
        console.print(f"[dim]panel average at {sig.cfg.max_concurrent} slot(s): "
                      f"{exp}/week entries, 15.2/week raw leg-agreement. "
                      f"Data ends {fmt(evals[-1].bar_open_ms)} — a window anchored "
                      f"to today is shorter than it looks if the feed is stale.[/dim]")
        last = evals[-1]
        console.print(f"latest bar {fmt(last.bar_open_ms)}  "
                      f"fired=[{'green' if last.fired else 'dim'}]{last.fired}[/]"
                      + (f"  [dim]{last.reason_if_skipped}[/dim]"
                         if last.reason_if_skipped else ""))
    return 0


def cmd_zones(args: argparse.Namespace) -> int:
    """Build (or top up) the KEY LEVELS cache for a symbol.

    The live loop reads this cache and never writes it: a snapshot is a genuine
    recompute over a trailing 500-bar buffer at ~3 ms a bar, so building
    `MT5:GOLD`'s 81,915 H1 bars inline would stall the page for minutes. Run this
    once per symbol; afterwards only new bars are computed.
    """
    from .indicators import zones
    from .indicators.base import IndicatorConfig

    cfg = IndicatorConfig()
    with Database(args.db) as db:
        for symbol in _symbol_list(args.symbols, args.symbol):
            with console.status(f"building zone snapshots for {symbol}…"):
                done = {"n": 0, "total": 0}

                def progress(n, total, _d=done):
                    _d["n"], _d["total"] = n, total

                snaps = zones.snapshot_series(db, symbol, cfg, progress=progress)
            if not snaps:
                console.print(f"[yellow]{symbol}[/yellow]: no snapshots — is "
                              f"30m/1h/4h synced?")
                continue
            console.print(
                f"[cyan]{symbol}[/cyan]: {len(snaps):,} snapshots, newest "
                f"{fmt(snaps[-1].ts)}  [dim]({done['total']:,} computed this run)[/dim]")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    with Database(args.db) as db:
        repo = CandleRepository(db)
        table = Table(title=f"Coverage {args.symbol}")
        table.add_column("TF")
        table.add_column("Bars", justify="right")
        table.add_column("From")
        table.add_column("To")
        table.add_column("Gaps", justify="right")
        table.add_column("Missing bars", justify="right")

        for tf in _timeframes(args.tf):
            first, last, count = repo.coverage(args.symbol, tf)
            gaps = repo.find_gaps(args.symbol, tf)
            missing = sum(g.missing_bars for g in gaps)
            table.add_row(
                tf,
                str(count),
                fmt(first),
                fmt(last),
                ("[green]0[/green]" if not gaps else f"[yellow]{len(gaps)}[/yellow]"),
                str(missing),
            )
        console.print(table)

        if args.show_gaps:
            for tf in _timeframes(args.tf):
                for gap in repo.find_gaps(args.symbol, tf)[:20]:
                    console.print(
                        f"  {tf}: {fmt(gap.start_ms)} → {fmt(gap.end_ms)} "
                        f"({gap.missing_bars} bars)"
                    )
    return 0


def cmd_indicators(args: argparse.Namespace) -> int:
    """Compute the indicator frame and print the tail — a quick sanity check."""
    from .indicators.base import IndicatorConfig
    from .indicators.registry import compute_indicators

    with Database(args.db) as db:
        df = CandleRepository(db).load(args.symbol, args.tf)
    if df.empty:
        console.print(f"[red]no candles for {args.symbol} {args.tf} — run sync first[/red]")
        return 1

    cfg = IndicatorConfig()
    out = compute_indicators(df, args.tf, cfg)
    console.print(f"computed {len(out)} bars, {len(out.columns)} columns")

    cols = [
        "open_time", "atr_14", "ema_7", "ema_28", "rsi_14", "adx_14",
        "ut_level", "ut_bias", "bb_percent_b", "structure_state",
        "volatility_band", "directional_score", "confirmed_bias",
    ]
    tail = out[cols].tail(args.tail).copy()
    tail["open_time"] = tail["open_time"].map(fmt)

    table = Table(title=f"{args.symbol} {args.tf} — last {args.tail} bars")
    for c in cols:
        table.add_column(c, overflow="fold")
    for _, row in tail.iterrows():
        table.add_row(
            *[
                row[c] if c == "open_time"
                else ("—" if row[c] != row[c] else f"{row[c]:.4g}")
                for c in cols
            ]
        )
    console.print(table)
    return 0


# ---------------------------------------------------------------------------


def _symbol_list(raw: str | None, fallback: str) -> list[str]:
    if not raw:
        return [fallback]
    out, seen = [], set()
    for part in raw.split(","):
        sym = part.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out or [fallback]


def _print_funnel(result) -> None:
    funnel = Table(title="Signal funnel", show_header=True)
    funnel.add_column("stage")
    funnel.add_column("n", justify="right")
    funnel.add_row("signals fired", str(result.signals_fired))
    funnel.add_row("skipped — outside session", str(result.skipped_session))
    funnel.add_row("skipped — already in position", str(result.skipped_in_position))
    funnel.add_row("skipped — cooldown", str(result.skipped_cooldown))
    funnel.add_row("[bold]trades taken[/bold]", f"[bold]{len(result.trades)}[/bold]")
    console.print(funnel)


def run_for_symbols(
    db: Database,
    cfg,
    config_yaml: str,
    symbols: list[str],
    *,
    allow_gaps: bool = False,
    qty: float | None = None,
    normalise: bool = True,
    quiet: bool = False,
) -> dict[str, object]:
    """Run one strategy against each symbol on the SAME calendar window.

    Returns ``{symbol: BacktestResult}``. See `engine/symbols.py` for why the
    window and the position size both have to be pinned before the numbers mean
    anything next to each other.
    """
    from .engine.backtester import Backtester, persist
    from .engine.symbols import common_period, retarget, stored_yaml

    start_ms, end_ms = (None, None)
    if len(symbols) > 1:
        start_ms, end_ms = common_period(db, symbols, cfg.timeframe)
        if not quiet:
            console.print(
                f"[dim]common window across {', '.join(symbols)}: "
                f"{fmt(start_ms)} -> {fmt(end_ms)}[/dim]"
            )

    out: dict[str, object] = {}
    for sym in symbols:
        scfg, note = retarget(cfg, db, sym, qty=qty, normalise=normalise,
                              start_ms=start_ms, end_ms=end_ms)
        if len(symbols) > 1:
            scfg = scfg.model_copy(update={"period": scfg.period.model_copy(
                update={"start": _iso_z(start_ms), "end": _iso_z(end_ms)})})
        if not quiet:
            console.print(f"[bold]{scfg.name}[/bold]  {scfg.symbol} {scfg.timeframe}  "
                          f"config_hash=[cyan]{scfg.config_hash()}[/cyan]")
            if sym != cfg.symbol:
                console.print(f"  [dim]{note.describe()}[/dim]")
            for n in note.notes:
                console.print(f"  [yellow]{n}[/yellow]")

        result = Backtester(db, scfg).run(allow_gaps=allow_gaps)
        persist(db, result, stored_yaml(config_yaml, scfg, sym != cfg.symbol))
        out[sym] = result
    return out


def cmd_backtest(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .analysis.metrics import summarise
    from .engine.config import StrategyConfig

    path = Path(args.config)
    cfg = StrategyConfig.from_yaml(path)
    if args.start:
        cfg = cfg.model_copy(update={"period": cfg.period.model_copy(update={"start": args.start})})
    if args.end:
        cfg = cfg.model_copy(update={"period": cfg.period.model_copy(update={"end": args.end})})

    symbols = _symbol_list(args.symbols or args.symbol, cfg.symbol)
    config_yaml = path.read_text(encoding="utf-8")

    with Database(args.db) as db:
        results = run_for_symbols(
            db, cfg, config_yaml, symbols,
            allow_gaps=args.allow_gaps, qty=args.qty,
            normalise=not args.no_normalise,
        )

    single = len(symbols) == 1
    for sym, result in results.items():
        trades = result.trades
        console.print(f"run_id [cyan]{result.run_id}[/cyan]  {sym}  "
                      f"{fmt(result.start_ms)} -> {fmt(result.end_ms)}  "
                      f"({result.bars_evaluated} bars evaluated)")
        if single:
            _print_funnel(result)
        if not trades:
            console.print("[yellow]no trades — nothing to summarise[/yellow]")
            continue
        if single:
            table = Table(title="Summary", show_header=False)
            for k, v in summarise(trades).items():
                table.add_row(k, v if isinstance(v, str) else f"{v:,.2f}")
            console.print(table)
        if result.gaps and sum(result.gaps.values()):
            console.print(f"[yellow]data gaps present: {result.gaps}[/yellow]")

    if not single:
        print_comparison(results)
    return 0


def print_comparison(results: dict) -> None:
    """Side-by-side across symbols, with the caveat that makes it readable."""
    from .analysis.metrics import wilson_interval

    table = Table(title="Cross-symbol comparison (same window, ATR-normalised size)")
    for c in ("symbol", "trades", "win %", "95% CI", "net P/L", "gross", "fees", "expectancy"):
        table.add_column(c, justify="right" if c != "symbol" else "left")
    for sym, r in results.items():
        t = r.trades
        if not t:
            table.add_row(sym, "0", "—", "—", "—", "—", "—", "—")
            continue
        wins = sum(1 for x in t if x.net_pnl > 0)
        net = sum(x.net_pnl for x in t)
        gross = sum(x.gross_pnl for x in t)
        fees = sum(x.fees for x in t)
        lo, hi = wilson_interval(wins, len(t))
        table.add_row(
            sym, str(len(t)), f"{wins / len(t) * 100:.1f}%",
            f"{lo * 100:.0f}–{hi * 100:.0f}%",
            f"[{'green' if net > 0 else 'red'}]{net:,.2f}[/]",
            f"{gross:,.2f}", f"{fees:,.2f}", f"{net / len(t):,.2f}",
        )
    console.print(table)
    console.print(
        "[dim]Position size was scaled by the ATR ratio so every symbol risks the "
        "same dollars per trade, and all symbols share one calendar window. "
        "Overlapping confidence intervals mean the ranking is not yet a finding.[/dim]"
    )


def cmd_runs(args: argparse.Namespace) -> int:
    import pandas as pd
    import yaml

    with Database(args.db) as db:
        rows = pd.read_sql_query(
            "SELECT r.run_id, r.created_at, r.config_yaml, r.config_hash, r.symbol, "
            "r.timeframe, r.start_ms, r.end_ms, "
            "(SELECT COUNT(*) FROM trades t WHERE t.run_id = r.run_id) n, "
            "(SELECT COALESCE(SUM(net_pnl),0) FROM trades t WHERE t.run_id = r.run_id) net "
            "FROM runs r ORDER BY r.created_at DESC",
            db.conn,
        )
    if rows.empty:
        console.print("[yellow]no runs yet[/yellow]")
        return 0
    table = Table(title="Runs")
    for c in ("run_id", "name", "symbol", "tf", "trades", "net P/L", "period"):
        table.add_column(c)
    for _, r in rows.iterrows():
        name = yaml.safe_load(r["config_yaml"]).get("name", "?")
        net = float(r["net"])
        table.add_row(
            r["run_id"], name, r["symbol"], r["timeframe"], str(int(r["n"])),
            f"[{'green' if net > 0 else 'red'}]{net:,.2f}[/]",
            f"{fmt(int(r['start_ms']))} -> {fmt(int(r['end_ms']))}",
        )
    console.print(table)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .analysis.report import build_book, build_report, render
    from .engine.config import StrategyConfig
    from .engine.symbols import comparison_context

    tfs = tuple(_timeframes(args.tf))

    with Database(args.db) as db:
        if args.symbols:
            symbols = _symbol_list(args.symbols, DEFAULT_SYMBOL)
            runs: dict[str, str] = {}
            for sym in symbols:
                row = db.conn.execute(
                    "SELECT run_id FROM runs WHERE symbol = ? "
                    "ORDER BY created_at DESC LIMIT 1", (sym,)
                ).fetchone()
                if row is None:
                    console.print(f"[red]no run for {sym} — run it first:[/red]")
                    console.print(
                        f"  python -m backtest.cli backtest <config> "
                        f"--symbols {','.join(symbols)}"
                    )
                    return 1
                runs[sym] = row["run_id"]

            comparison = None
            try:
                yaml_text = db.conn.execute(
                    "SELECT config_yaml FROM runs WHERE run_id = ?", (runs[symbols[0]],)
                ).fetchone()["config_yaml"]
                comparison = comparison_context(
                    db, StrategyConfig.from_text(yaml_text), symbols)
            except Exception as exc:      # a caveat box is nice-to-have
                console.print(f"[yellow]comparison context unavailable: {exc}[/yellow]")

            out = Path(args.out) if args.out else (
                Path(__file__).resolve().parent / "reports"
                / f"report_{'_'.join(symbols)}.html"
            )
            book = build_book(db, runs, active=symbols[0], timeframes=tfs,
                              comparison=comparison)
            path = render(book, out)
        else:
            run_id = args.run_id
            if run_id is None:
                row = db.conn.execute(
                    "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    console.print("[red]no runs — run a backtest first[/red]")
                    return 1
                run_id = row["run_id"]

            out = Path(args.out) if args.out else (
                Path(__file__).resolve().parent / "reports" / f"report_{run_id}.html"
            )
            path = build_report(db, run_id, out, tfs)

    size_mb = path.stat().st_size / 1e6
    console.print(f"wrote [cyan]{path}[/cyan]  ({size_mb:.1f} MB, self-contained)")
    if args.open:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Keep a strategy pointed at the newest data and serve the report."""
    from pathlib import Path

    from .engine.config import StrategyConfig
    from .watch import Watcher

    base = StrategyConfig.from_yaml(Path(args.config))
    symbols = _symbol_list(args.symbols, base.symbol)


    from .engine.risk_engine import GateSettings

    live_mode = not args.backtest
    gate = GateSettings(
        enabled=not args.no_gate,
        max_danger=args.max_danger,
        min_confidence=args.min_confidence,
        require_bias=args.require_bias,
    )
    w = Watcher(Path(args.config), db_path=args.db,
                interval_s=args.interval, port=args.port,
                report_dir=Path(args.report_dir) if args.report_dir else None,
                symbols=symbols, live_interval_s=args.live_interval,
                normalise=not args.no_normalise, mt5_sync=not args.no_mt5,
                live_mode=live_mode, gate=gate, emit=not args.no_emit,
                max_bars=args.max_bars)
    url = f"http://127.0.0.1:{args.port}/"
    console.print(f"[bold]watching[/bold] {args.config}  [{', '.join(symbols)}]")
    if live_mode:
        console.print(f"  [bold]LIVE MODE[/bold] — no backtest. The rule is evaluated "
                      f"on each closed bar and gated by the risk read.")
        console.print(f"  chart + risk every {args.live_interval}s, "
                      f"rule + payload every {args.interval}s -> [cyan]{url}[/cyan]")
        console.print(f"  gate: {'OFF' if args.no_gate else f'danger <= {args.max_danger}, confidence >= {args.min_confidence}'}"
                      + ("  [dim](signals NOT written)[/dim]" if args.no_emit else ""))
    else:
        console.print(f"  chart refreshes every {args.live_interval}s, "
                      f"backtest re-runs every {args.interval}s -> [cyan]{url}[/cyan]")
    console.print("  Ctrl+C to stop")

    opened = {"done": False}

    def report(status: dict) -> None:
        console.print(
            f"[dim]{status['updated_myt']}[/dim]  rebuilt in {status['cycle_seconds']}s"
            + (f"  [dim]MT5 {status['mt5']}[/dim]" if status.get("mt5") else "")
        )
        rule = status.get("rule") or {}
        if rule and not rule.get("error"):
            if rule.get("sent"):
                console.print(
                    f"  [bold green]>>> SIGNAL {rule['symbol']} "
                    f"{str(rule['side']).upper()} @ {rule.get('entry_ref')}  "
                    f"SL {rule.get('sl')}  TP {rule.get('tp')}  "
                    f"{rule.get('lots')} lots[/bold green]")
            elif rule.get("vetoed"):
                g = rule.get("gate") or {}
                console.print(f"  [yellow]rule fired, VETOED by the risk gate: "
                              f"{', '.join(g.get('vetoes') or [])}[/yellow]")
            elif rule.get("blocking"):
                console.print(f"  [dim]rule {rule.get('anchor')} "
                              f"{rule.get('bar_myt','?')} — blocked by "
                              f"{', '.join(rule['blocking'])}[/dim]")

        for sym, info in (status.get("symbols") or {}).items():
            live = info.get("live") or {}
            entry = live.get("entry_would_fire", {})
            risk = info.get("risk") or {}
            if status.get("live_mode") and not risk.get("error"):
                lo, sh = risk.get("long") or {}, risk.get("short") or {}
                console.print(
                    f"  [bold]{sym}[/bold]  bar {live.get('last_closed_bar_myt','?')}"
                    f"  px {live.get('last_price','?')}"
                    f"  danger L{lo.get('danger','?')}/S{sh.get('danger','?')}"
                    f"  conf L{lo.get('confidence','?')}/S{sh.get('confidence','?')}"
                    f"  bias {risk.get('bias','?')}")
                continue
            net = info["net_pnl"]
            console.print(
                f"  [bold]{sym}[/bold]  bar {live.get('last_closed_bar_myt','?')}"
                f"  px {live.get('last_price','?')}"
                f"  trades {info['trades']}  net "
                f"[{'green' if net > 0 else 'red'}]{net:,.2f}[/]"
            )
            if entry.get("fired"):
                console.print("    [bold green]>>> ENTRY CONDITIONS MET on the last closed bar[/bold green]")
            elif entry.get("conditions"):
                blocked = [k for k, v in entry["conditions"].items() if not v]
                if blocked:
                    console.print(f"    [dim]blocked by: {', '.join(blocked)}[/dim]")
        if not opened["done"] and args.open:
            import webbrowser
            webbrowser.open(url)
            # The cockpit is a SECOND window, not a replacement: the chart says
            # what happened, the cockpit says how much room there is. `open_new`
            # rather than `open` so it does not land as a tab behind the chart
            # on browsers that reuse the window.
            webbrowser.open_new(url.rstrip("/") + "/cockpit.html")
            opened["done"] = True

    try:
        w.run(on_cycle=report)
    except KeyboardInterrupt:
        w.stop()
        console.print("stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backtest.cli")
    p.add_argument("--db", default=None, help="path to market.db")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("resolve", help="resolve the gold symbol against instruments-info")
    r.add_argument("--symbol", default=DEFAULT_SYMBOL)
    r.set_defaults(func=cmd_resolve)

    s = sub.add_parser("sync", help="fetch and store candles")
    s.add_argument("--symbol", default=DEFAULT_SYMBOL)
    s.add_argument("--tf", default=DEFAULT_TIMEFRAMES)
    s.add_argument("--from", dest="start", required=True, type=str)
    s.add_argument("--to", dest="end", default="now", type=str)
    s.add_argument("--no-resume", action="store_true")
    s.add_argument("--funding", action="store_true")
    s.add_argument("--open-interest", action="store_true")
    s.set_defaults(func=cmd_sync)

    oi = sub.add_parser("oi", help="backfill and report stored open interest")
    oi.add_argument("--symbol", default="XAUUSDT,BTCUSDT,ETHUSDT",
                    help="comma-separated")
    oi.add_argument("--interval", default="15m")
    oi.add_argument("--days", type=int, default=170,
                    help="how far back to ask; the source serves about 170")
    oi.set_defaults(func=cmd_oi)

    im = sub.add_parser("import-mt5", help="import a MetaTrader 5 trade history .xlsx")
    im.add_argument("file")
    im.add_argument("--tz-offset", type=int, default=None,
                    help="broker server offset in whole hours; measured from the "
                         "fills themselves when omitted")
    im.set_defaults(func=cmd_import_mt5)

    sm = sub.add_parser("sync-mt5",
                        help="pull trade history from the running MT5 terminal")
    sm.add_argument("--full", action="store_true",
                    help="re-read all history instead of just recent days")
    sm.add_argument("--lookback-days", type=int, default=7,
                    help="how far back to re-read on an incremental sync, so a "
                         "position closed today but opened last week is updated")
    sm.set_defaults(func=cmd_sync_mt5)

    sr = sub.add_parser("sync-mt5-rates",
                        help="pull candles from the MT5 terminal (stored as MT5:<SYMBOL>)")
    sr.add_argument("--symbol", default="GOLD", help="broker symbol, e.g. GOLD")
    sr.add_argument("--tf", default=DEFAULT_TIMEFRAMES)
    sr.add_argument("--from", dest="start", default=None,
                    help="omit to take everything the terminal will give")
    sr.set_defaults(func=cmd_sync_mt5_rates)

    sg = sub.add_parser("signals",
                        help="evaluate the rule and append to reports/signals.jsonl")
    sg.add_argument("--backfill-days", type=int, default=None,
                    help="evaluate every closed bar in the last N days")
    sg.add_argument("--all", action="store_true", help="evaluate the whole panel")
    sg.add_argument("--out", default=None)
    sg.set_defaults(func=cmd_signals)

    zn = sub.add_parser("zones",
                        help="build the KEY LEVELS snapshot cache for a symbol")
    zn.add_argument("--symbol", default=DEFAULT_SYMBOL)
    zn.add_argument("--symbols", default=None,
                    help="comma-separated, e.g. MT5:GOLD,XAUUSDT")
    zn.set_defaults(func=cmd_zones)

    c = sub.add_parser("coverage", help="report stored ranges and gaps")
    c.add_argument("--symbol", default=DEFAULT_SYMBOL)
    c.add_argument("--tf", default=DEFAULT_TIMEFRAMES)
    c.add_argument("--show-gaps", action="store_true")
    c.set_defaults(func=cmd_coverage)

    b = sub.add_parser("backtest", help="run a strategy config")
    b.add_argument("config")
    b.add_argument("--from", dest="start", default=None)
    b.add_argument("--to", dest="end", default=None)
    b.add_argument("--allow-gaps", action="store_true")
    b.add_argument("--symbol", default=None,
                   help="override the config's symbol (size is ATR-normalised)")
    b.add_argument("--symbols", default=None,
                   help="comma-separated; runs each on one shared window and "
                        "prints a comparison, e.g. XAUUSDT,BTCUSDT,ETHUSDT")
    b.add_argument("--qty", type=float, default=None,
                   help="force position size instead of ATR-normalising it")
    b.add_argument("--no-normalise", action="store_true",
                   help="keep the config's qty on every symbol — results will "
                        "NOT be comparable")
    b.set_defaults(func=cmd_backtest)

    rn = sub.add_parser("runs", help="list stored runs")
    rn.set_defaults(func=cmd_runs)

    rp = sub.add_parser("report", help="build the interactive HTML report for a run")
    rp.add_argument("run_id", nargs="?", default=None, help="defaults to the newest run")
    rp.add_argument("--tf", default="5m,15m,30m,1h,4h")
    rp.add_argument("--out", default=None)
    rp.add_argument("--symbols", default=None,
                    help="comma-separated; builds ONE page holding the newest "
                         "run of each symbol, with a switcher")
    rp.add_argument("--open", action="store_true", help="open it in the browser")
    rp.set_defaults(func=cmd_report)

    wt = sub.add_parser("watch", help="sync + re-run + serve the report on a timer")
    wt.add_argument("config", nargs="?",
                    default="backtest/configs/ut_1h_long_v1.yaml")
    wt.add_argument("--interval", type=int, default=300,
                    help="seconds between full backtest rebuilds")
    wt.add_argument("--live-interval", type=int, default=5,
                    help="seconds between live chart updates (default 5)")
    wt.add_argument("--symbols", default=None,
                    help="comma-separated; adds a symbol switcher to the page, "
                         "e.g. XAUUSDT,BTCUSDT,ETHUSDT")
    wt.add_argument("--no-normalise", action="store_true",
                    help="keep the config's qty on every symbol — results will "
                         "NOT be comparable")
    wt.add_argument("--no-mt5", action="store_true",
                    help="do not pull trade history from the MT5 terminal each cycle")
    wt.add_argument("--port", type=int, default=8787)
    wt.add_argument("--report-dir", default=None,
                    help="where to write index.html, the payloads and the JSON "
                         "feeds (default backtest/reports). Give a second watcher "
                         "its own directory — two sharing one overwrite each "
                         "other's page and the result looks like a code bug")
    wt.add_argument("--open", action="store_true", help="open the browser once")
    wt.add_argument("--backtest", action="store_true",
                    help="re-run the strategy every cycle (the old behaviour). "
                         "Without this the watcher runs in LIVE mode: no "
                         "backtest, the rule evaluated on each closed bar")
    wt.add_argument("--max-bars", type=int, default=1500,
                    help="bars per timeframe embedded in the live payload "
                         "(default 1500; indicators still warm up over more)")
    wt.add_argument("--no-gate", action="store_true",
                    help="log the risk read but never let it veto a signal")
    wt.add_argument("--max-danger", type=int, default=70,
                    help="veto a signal above this Danger%% (default 70)")
    wt.add_argument("--min-confidence", type=int, default=25,
                    help="veto a signal below this Confidence%% (default 25)")
    wt.add_argument("--require-bias", action="store_true",
                    help="also require the structural bias to agree with the side")
    wt.add_argument("--no-emit", action="store_true",
                    help="evaluate and display, but do not append to signals.jsonl")
    wt.set_defaults(func=cmd_watch)

    i = sub.add_parser("indicators", help="compute indicators and print the tail")
    i.add_argument("--symbol", default=DEFAULT_SYMBOL)
    i.add_argument("--tf", default="30m")
    i.add_argument("--tail", type=int, default=10)
    i.set_defaults(func=cmd_indicators)

    return p


def main(argv: list[str] | None = None) -> int:
    # The Windows console defaults to cp1252, which cannot encode the glyphs
    # rich uses for box drawing. Force UTF-8 rather than degrading the output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    if args.db is None:
        from .data.db import DEFAULT_DB_PATH

        args.db = DEFAULT_DB_PATH
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
