"""Running one strategy against more than one instrument, comparably.

Pointing a config at a different symbol is one line. Making the *results*
comparable is not, and getting it wrong produces a confident, completely wrong
answer — which is worse than no answer.

Two things have to be normalised or the comparison is meaningless:

**1. Risk size.** ``ut_1h_long_v1`` stops out at ``$25`` on ``qty 1.0``. On gold
(price ~4,600, median 1H ATR-14 ~14.6) that is 1.7 ATR — a normal stop. On
BTCUSDT (price ~100,000, ATR ~700) the same ``$25`` on ``qty 1.0`` is 0.036 ATR,
i.e. roughly the bid-ask spread. Every trade would stop out instantly and BTC
would look catastrophic for a reason that has nothing to do with the strategy.
So ``qty`` is rescaled by the ATR ratio::

    qty_new = qty_ref * atr_ref / atr_new

which holds the **stop distance in ATR** constant across instruments. The dollar
risk per trade stays the same too, so net P/L is directly comparable.

**2. The window.** BTCUSDT has 6 years of history, XAUUSDT has 166 days
(listed 2026-03-09). Comparing a 6-year sample against a 166-day one measures the
market regime, not the strategy. :func:`common_period` intersects the stored
coverage so all symbols are judged on identical calendar time.

Everything either function changes is returned in a note and printed, because a
silent rescale is exactly the kind of thing that makes a backtest lie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..data.db import CandleRepository, Database, MetaRepository
from ..indicators.trend import atr
from .config import StrategyConfig

# The comparison set. Anything Bybit lists under `linear` works. The crypto
# perps were taken out at the owner's request (2026-09-23): the question this
# set exists to answer is now gold against its own proxy rather than gold
# against BTC/ETH.
COMPARISON_SYMBOLS: tuple[str, ...] = ("XAUUSDT", "PAXGUSDT")

SYMBOL_LABEL: dict[str, str] = {
    "XAUUSDT": "Gold",
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "PAXGUSDT": "PAXG",
}


@dataclass(frozen=True)
class Retarget:
    """What changed when a config was pointed at another symbol."""

    symbol: str
    reference_symbol: str
    atr_reference: float
    atr_symbol: float
    qty_before: float
    qty_after: float
    tick_before: float
    tick_after: float
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.symbol == self.reference_symbol:
            return f"{self.symbol}: unchanged (reference instrument)"
        return (
            f"{self.symbol}: median ATR-14 {self.atr_symbol:,.4f} vs "
            f"{self.reference_symbol} {self.atr_reference:,.4f} -> "
            f"qty {self.qty_before:g} -> {self.qty_after:g}, "
            f"tick {self.tick_before:g} -> {self.tick_after:g}"
        )


def median_atr(
    db: Database,
    symbol: str,
    timeframe: str,
    period: int = 14,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> float:
    """Median ATR-14 in price units over the stored window.

    Median rather than mean: ATR is right-skewed and a handful of shock bars
    would otherwise set the scale for the whole comparison.
    """
    df = CandleRepository(db).load(symbol, timeframe, start_ms, end_ms)
    if df.empty:
        raise SystemExit(
            f"no {timeframe} candles stored for {symbol} — sync it first:\n"
            f"  python -m backtest.cli sync --symbol {symbol} --tf {timeframe} --from 2026-03-09"
        )
    a = atr(df["high"].to_numpy(), df["low"].to_numpy(),
            df["close"].to_numpy(), period)
    a = a[~np.isnan(a)]
    if a.size == 0:
        raise SystemExit(f"{symbol} {timeframe}: not enough bars to measure ATR-{period}")
    return float(np.median(a))


def common_period(
    db: Database, symbols: list[str] | tuple[str, ...], timeframe: str
) -> tuple[int, int]:
    """(start_ms, end_ms) covered by *every* symbol at this timeframe.

    Without this, a symbol with deeper history is judged on a different market.
    """
    repo = CandleRepository(db)
    firsts, lasts = [], []
    for sym in symbols:
        a, b, n = repo.coverage(sym, timeframe)
        if a is None or n == 0:
            raise SystemExit(
                f"no {timeframe} candles stored for {sym} — sync it before comparing"
            )
        firsts.append(a)
        lasts.append(b)
    return max(firsts), min(lasts)


def _round_step(value: float, step: float | None, minimum: float | None) -> float:
    """Snap a quantity to the instrument's lot size, never below its minimum."""
    if step and step > 0:
        value = round(value / step) * step
        # round() on a float step reintroduces representation noise (0.001 * 18
        # is not exactly 0.018); pin it to the step's own decimal places.
        decimals = max(0, -int(np.floor(np.log10(step))) + 2)
        value = round(value, decimals)
    if minimum is not None and value < minimum:
        value = minimum
    return float(value)


def retarget(
    cfg: StrategyConfig,
    db: Database,
    symbol: str,
    *,
    qty: float | None = None,
    normalise: bool = True,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[StrategyConfig, Retarget]:
    """Point ``cfg`` at ``symbol``, rescaling size and tick so results compare.

    ``qty`` overrides the computed size. ``normalise=False`` keeps the config's
    own qty, which is only ever right if you have already sized it yourself —
    the resulting run is **not** comparable with the reference symbol's.
    """
    ref = cfg.symbol
    meta = MetaRepository(db)
    row = meta.get_instrument(symbol)

    tick_before = cfg.costs.tick_size
    tick_after = tick_before
    notes: list[str] = []
    if row is not None and row["tick_size"]:
        tick_after = float(row["tick_size"])
    else:
        notes.append(
            f"{symbol} has no cached instrument row; keeping tickSize {tick_before:g}. "
            "Run `cli resolve --symbol {symbol}` or sync it to pick up the real one."
        )

    qty_before = cfg.sizing.qty
    atr_ref = atr_new = float("nan")
    qty_after = qty_before

    if symbol == ref:
        pass
    elif qty is not None:
        qty_after = float(qty)
        notes.append(f"qty forced to {qty_after:g} by --qty; ATR normalisation skipped")
    elif not normalise:
        notes.append(
            f"--no-normalise: qty stays {qty_before:g}. Net P/L is NOT comparable "
            f"with {ref} — the two runs risk different amounts per trade."
        )
    else:
        atr_ref = median_atr(db, ref, cfg.timeframe, start_ms=start_ms, end_ms=end_ms)
        atr_new = median_atr(db, symbol, cfg.timeframe, start_ms=start_ms, end_ms=end_ms)
        raw = qty_before * atr_ref / atr_new
        step = float(row["qty_step"]) if row is not None and row["qty_step"] else None
        minq = float(row["min_qty"]) if row is not None and row["min_qty"] else None
        qty_after = _round_step(raw, step, minq)
        if step and abs(qty_after - raw) / max(raw, 1e-12) > 0.02:
            notes.append(
                f"qty snapped {raw:.6g} -> {qty_after:g} by the {step:g} lot step "
                f"({abs(qty_after - raw) / raw * 100:.1f}% off target risk)"
            )

    updated = cfg.model_copy(update={
        "name": cfg.name if symbol == ref else f"{cfg.name}@{symbol}",
        "symbol": symbol,
        "sizing": cfg.sizing.model_copy(update={"qty": qty_after}),
        "costs": cfg.costs.model_copy(update={"tick_size": tick_after}),
    })
    return updated, Retarget(
        symbol=symbol,
        reference_symbol=ref,
        atr_reference=atr_ref,
        atr_symbol=atr_new,
        qty_before=qty_before,
        qty_after=qty_after,
        tick_before=tick_before,
        tick_after=tick_after,
        notes=notes,
    )


def comparison_context(
    db: Database, cfg: StrategyConfig, symbols: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    """Facts a reader needs before believing a cross-symbol table.

    Embedded in the report so the caveats travel with the numbers instead of
    living in a doc nobody opens.
    """
    repo = CandleRepository(db)
    start_ms, end_ms = common_period(db, symbols, cfg.timeframe)
    rows = []
    for sym in symbols:
        df = repo.load(sym, cfg.timeframe, start_ms, end_ms)
        px = float(df["close"].median()) if not df.empty else float("nan")
        a = median_atr(db, sym, cfg.timeframe, start_ms=start_ms, end_ms=end_ms)
        rows.append({
            "symbol": sym,
            "label": SYMBOL_LABEL.get(sym, sym),
            "median_price": round(px, 2),
            "median_atr": round(a, 4),
            "atr_percent": round(a / px * 100, 3) if px == px and px else None,
            "bars": int(len(df)),
        })
    return {
        "timeframe": cfg.timeframe,
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "symbols": rows,
    }


def stored_yaml(original: str, cfg: StrategyConfig, retargeted: bool) -> str:
    """The config text to store on a run row.

    The original YAML is kept verbatim for the reference symbol. For a retarget
    the overrides are written INTO the yaml, so the run row records what actually
    executed rather than what was typed — a run you cannot reproduce from its own
    stored config is worse than no record, and the report reads ``sizing.qty``
    back out of it to price a measured move.
    """
    if not retargeted:
        return original

    import yaml as _yaml

    raw = _yaml.safe_load(original)
    raw["name"] = cfg.name
    raw["symbol"] = cfg.symbol
    raw.setdefault("sizing", {})["qty"] = cfg.sizing.qty
    raw.setdefault("costs", {})["tick_size"] = cfg.costs.tick_size
    raw["period"] = {"start": str(cfg.period.start), "end": str(cfg.period.end)}
    header = (
        "# Retargeted from the reference symbol: qty and tickSize were rescaled\n"
        "# so this run is comparable, and the period was pinned to the window\n"
        "# every compared symbol covers. The rule is in backtest/engine/symbols.py.\n"
    )
    return header + _yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
