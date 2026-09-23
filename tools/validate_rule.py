"""Re-measure the ENTRY_RULES trigger on any panel. `AUTOMATION.md` gate §5.1.

    python -m backtest.tools.validate_rule --symbol MT5:GOLD --anchor 15m
    python -m backtest.tools.validate_rule --symbol XAUUSDT --anchor 15m --grid

This is the decision point of the automation build: the rule's 56.6%/68.2% were
measured on **Bybit** candles, and moving the feed to the broker invalidates none
of the reasoning but all of the calibration. If the edge does not survive the
feed change, everything downstream is moot.

The protocol is `ENTRY_RULES.md` §6, reproduced exactly, because a validation
that quietly uses an easier method than the original is not a validation:

1. **Entry at the next bar's open**, never the signal bar's close.
2. **Forward path walked at a finer resolution** than the anchor, so which of
   TP/SL is touched first is a fact rather than a guess. Ties resolve as losses.
3. **Non-overlapping sequential simulation** — a candidate is skipped while a
   position is open. Bar-level screens count the same trend five times over and
   their confidence intervals lie; §6 records that the first version of the study
   reported ±2.1 on a number whose honest interval was ±8.
4. **Proportional costs**, not a fixed dollar figure — gold traded 1,780–4,540
   across the panels and a flat number is wrong at both ends.
5. **Long and short measured separately**, then pooled. A rule whose lift lives
   entirely on one side is a bet on the direction of the sample.

The null is 49.8%: the dip/reclaim pattern with no multi-timeframe agreement,
measured over n=2107. Beating a coin flip is not the bar; beating *that* is.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..engine import features as F
from ..engine.signals import SignalContext, evaluate_signal
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators

# The nine legs of ENTRY_RULES §4.1 / §4.2, in order.
LEGS_LONG = [
    ("4h UT bullish", "htf_4h_ut_bullish"),
    ("1h UT bullish", "htf_1h_ut_bullish"),
    ("30m UT bullish", "htf_30m_ut_bullish"),
    ("own UT bullish", "ut_bias_bullish"),
    ("5m UT bullish", "htf_5m_ut_bullish"),
    ("4h EMA stack", "htf_4h_ema_stack_bullish"),
    ("dip below EMA14", "mtf_dip_long"),
    ("reclaim", "mtf_reclaim_long"),
]
LEGS_SHORT = [
    ("4h UT bearish", "htf_4h_ut_bearish"),
    ("1h UT bearish", "htf_1h_ut_bearish"),
    ("30m UT bearish", "htf_30m_ut_bearish"),
    ("own UT bearish", "ut_bias_bearish"),
    ("5m UT bearish", "htf_5m_ut_bearish"),
    ("4h EMA stack", "htf_4h_ema_stack_bearish"),
    ("dip above EMA14", "mtf_dip_short"),
    ("reclaim", "mtf_reclaim_short"),
]
# Legs 8 and 9 are both inside mtf_reclaim_*, so eight entries cover nine legs.

NULL_WR = 49.8      # dip/reclaim alone, n=2107 (ENTRY_RULES §4.3)


@dataclass
class Trade:
    side: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    reason: str
    atr: float
    r: float            # result in R (+tp_mult / -sl_mult), before cost
    net: float          # per unit, after proportional cost
    ambiguous: bool


@dataclass
class Panel:
    symbol: str
    anchor: str
    path_tf: str
    start: int
    end: int
    tp_mult: float
    sl_mult: float
    cost_bps: float
    trades: list[Trade] = field(default_factory=list)
    fired: int = 0
    skipped_in_position: int = 0
    warnings: list[str] = field(default_factory=list)


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - m) * 100, min(1.0, c + m) * 100)


def _frames(db: Database, symbol: str, tfs: list[str], cfg: IndicatorConfig,
            tail_bars: int | None = None):
    """Raw + computed frames per timeframe.

    ``tail_bars`` keeps only the newest N bars of EACH timeframe before computing.
    Measurement passes must never set it — they need the whole panel — but the
    live path evaluates one bar and cannot afford 400k. See ``build_context``.
    """
    repo = CandleRepository(db)
    raw, ind = {}, {}
    for tf in tfs:
        d = repo.load(symbol, tf)
        if d.empty:
            continue
        if tail_bars:
            d = d.tail(int(tail_bars)).reset_index(drop=True)
        raw[tf] = d
        ind[tf] = compute_indicators(d, tf, cfg)
    return raw, ind


# Building a context computes indicators across five timeframes — ~400k bars on
# the MT5 panel, about 16 seconds. The TP/SL grid calls measure() nine times with
# identical inputs, so without this the sweep recomputed the same frames nine
# times over. Keyed on everything that can change the frames.
#
# The key was once `id(db)`, which is wrong in two ways that only show up in a
# long-lived process. CPython reuses an address once an object is freed, so a
# process opening a Database per cycle can be handed a PREVIOUS cycle's frames —
# silently, with stale bars, and forever. And the converse: a live loop that
# holds ONE Database open across a bar close would keep hitting a cache built
# before that bar existed, so `evaluate_latest` would keep answering about the
# bar before last. Keying on the DB path plus the newest anchor bar fixes both:
# same data is a hit, new data is a miss, and a sweep (where the data cannot
# move) still hits every time.
_CTX_CACHE: dict[tuple, tuple] = {}

# The cache holds five computed timeframes per entry; on the MT5 panel that is
# hundreds of MB, so it cannot grow without bound in a process that runs for days.
_CTX_CACHE_MAX = 8


def _newest_bar(db: Database, symbol: str, anchor: str) -> int:
    """Newest stored open_time for (symbol, anchor). One indexed row."""
    row = db.conn.execute(
        "SELECT MAX(open_time) FROM candles WHERE symbol = ? AND interval = ?",
        (symbol, anchor)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def build_context(
    db: Database, symbol: str, anchor: str, cfg: IndicatorConfig, dip_lookback: int,
    tail_bars: int | None = None,
) -> tuple[SignalContext, pd.DataFrame]:
    """Anchor frame plus every higher timeframe aligned as-of-CLOSED.

    ``tail_bars=None`` (the default, and what every measurement uses) reads all of
    history. The LIVE path passes a tail because it only ever reads the newest
    bar: on the MT5 panel a full build is ~400k bars and ~24 s, which a loop that
    ticks every 5 s cannot pay every time an anchor bar closes. It is sound only
    because these columns converge — `tests/test_live.py` pins a 1,500-bar tail as
    bit-identical to full history on the values actually read. It is NOT true of
    the swing-structure columns, so anything reading those must leave this None.
    """
    key = (str(getattr(db, "path", "")), symbol, anchor, dip_lookback, tail_bars,
           _newest_bar(db, symbol, anchor),
           cfg.config_key() if hasattr(cfg, "config_key") else repr(cfg))
    hit = _CTX_CACHE.get(key)
    if hit is not None:
        return hit

    need = sorted({anchor, "5m", "15m", "30m", "1h", "4h"},
                  key=lambda t: INTERVAL_MS[t])
    raw, ind = _frames(db, symbol, need, cfg, tail_bars)
    if anchor not in raw:
        raise SystemExit(f"no {anchor} candles for {symbol}")

    df = raw[anchor]
    ot = df["open_time"].to_numpy(dtype="int64")
    frames = {tf: (raw[tf]["open_time"].to_numpy(dtype="int64"), ind[tf]) for tf in raw}
    idx = F.build_htf_alignment(ot, anchor, frames)
    htf = {tf: F.aligned_frame(ind[tf], idx[tf]) for tf in raw if tf != anchor}

    # Coverage of every frame a leg reads. A timeframe that starts LATER than the
    # anchor silently kills every signal before that date — an HTF column that is
    # blank cannot equal BIAS_BULLISH — and the run then reports a shorter panel
    # with no error. This cost a full measurement pass; it is now stated.
    coverage = {tf: int(raw[tf]["open_time"].iloc[0]) for tf in raw}

    ctx = SignalContext(
        ind=ind[anchor],
        close=df["close"].to_numpy(), high=df["high"].to_numpy(),
        low=df["low"].to_numpy(), open_time=ot,
        thresholds={"ut_near_atr": 0.6, "zone_near_atr": 0.5,
                    "dynamic_near_atr": 0.5, "zone_touch_atr": 0.25,
                    "dip_lookback": dip_lookback},
        htf=htf,
    )
    if len(_CTX_CACHE) >= _CTX_CACHE_MAX:
        _CTX_CACHE.pop(next(iter(_CTX_CACHE)))     # oldest insertion first
    _CTX_CACHE[key] = (ctx, df, coverage)
    return ctx, df, coverage


def leg_masks(ctx: SignalContext, side: str, use_5m: bool) -> dict[str, np.ndarray]:
    legs = LEGS_LONG if side == "long" else LEGS_SHORT
    out = {}
    for label, name in legs:
        if not use_5m and name.startswith("htf_5m"):
            continue
        out[label] = np.asarray(evaluate_signal(name, ctx), dtype=bool)
    return out


def walk_forward(
    path_t: np.ndarray, path_h: np.ndarray, path_l: np.ndarray, path_c: np.ndarray,
    start_ms: int, entry: float, tp: float, sl: float, long: bool, horizon_ms: int,
) -> tuple[str, float, int, bool]:
    """First touch of TP or SL on the finer series. Ties are losses.

    Returns (reason, exit_price, exit_time, ambiguous). ``ambiguous`` marks a
    sub-bar that straddled both levels — the resolution limit of the path feed,
    recorded rather than hidden because it is the one place this measurement
    could flatter itself.
    """
    i = int(np.searchsorted(path_t, start_ms, "left"))
    end = start_ms + horizon_ms
    while i < path_t.size and path_t[i] <= end:
        hi, lo = path_h[i], path_l[i]
        hit_tp = (hi >= tp) if long else (lo <= tp)
        hit_sl = (lo <= sl) if long else (hi >= sl)
        if hit_tp and hit_sl:
            return ("sl", sl, int(path_t[i]), True)      # tie -> loss
        if hit_sl:
            return ("sl", sl, int(path_t[i]), False)
        if hit_tp:
            return ("tp", tp, int(path_t[i]), False)
        i += 1
    if i > 0 and i - 1 < path_t.size:
        j = min(i, path_t.size) - 1
        # Exit at the CLOSE of the last bar in the horizon. Using the extreme
        # against the position would charge a loss that never happened.
        return ("timeout", float(path_c[j]), int(path_t[j]), False)
    return ("nodata", entry, start_ms, False)


def measure(
    db: Database, symbol: str, anchor: str, *,
    tp_mult: float = 2.5, sl_mult: float = 2.5, cost_bps: float = 11.0,
    use_5m: bool = True, dip_lookback: int = 3, path_tf: str | None = None,
    horizon_bars: int = 96, start_ms: int | None = None, cfg: IndicatorConfig | None = None,
    session: str = "gold_session", windows: bool = False, max_concurrent: int = 1,
) -> dict[str, Panel]:
    """Measure both sides. Returns {'long': Panel, 'short': Panel}."""
    cfg = cfg or IndicatorConfig()
    ctx, df, coverage = build_context(db, symbol, anchor, cfg, dip_lookback)
    ot = ctx.open_time
    opens = df["open"].to_numpy()
    atr = ctx.ind["atr_14"].to_numpy()
    usable = ctx.ind["usable"].to_numpy().astype(bool) if "usable" in ctx.ind else np.ones(ot.size, bool)

    # Path feed: the finest series available that is finer than the anchor.
    repo = CandleRepository(db)
    if path_tf is None:
        for cand in ("1m", "5m", "15m", "30m"):
            if INTERVAL_MS[cand] >= INTERVAL_MS[anchor]:
                break
            d = repo.load(symbol, cand)
            if not d.empty:
                path_tf = cand
                break
    pdf = repo.load(symbol, path_tf) if path_tf else df
    if pdf.empty:
        pdf, path_tf = df, anchor
    path_t = pdf["open_time"].to_numpy(dtype="int64")
    path_h, path_l = pdf["high"].to_numpy(), pdf["low"].to_numpy()
    path_c = pdf["close"].to_numpy()

    in_session = np.array([F.is_gold_session(int(t)) for t in ot], dtype=bool)
    myt_hour = ((ot // 3_600_000) + 8) % 24
    in_window = ((myt_hour >= 6) & (myt_hour < 9)) | ((myt_hour >= 19) & (myt_hour < 22))

    warnings: list[str] = []
    horizon_ms = horizon_bars * INTERVAL_MS[anchor]
    lo_bound = start_ms if start_ms is not None else max(int(ot[0]), int(path_t[0]))
    # Only bars whose forward path is actually covered can be measured.
    hi_bound = int(path_t[-1])

    # Legs cannot be true before their own timeframe has data.
    needed = {"4h", "1h", "30m", anchor} | ({"5m"} if use_5m else set())
    binding = max((coverage.get(tf, lo_bound) for tf in needed if tf in coverage),
                  default=lo_bound)
    if binding > lo_bound:
        late = sorted((tf, coverage[tf]) for tf in needed
                      if tf in coverage and coverage[tf] > lo_bound)
        warnings.append(
            "panel truncated to " + _fmt_ms(binding) + " by "
            + ", ".join(f"{tf} (starts {_fmt_ms(t)})" for tf, t in late)
        )
        lo_bound = binding

    out: dict[str, Panel] = {}
    for side in ("long", "short"):
        masks = leg_masks(ctx, side, use_5m)
        fire = np.ones(ot.size, dtype=bool)
        for m in masks.values():
            fire &= m
        fire &= usable
        fire &= (ot >= lo_bound) & (ot <= hi_bound)
        # ENTRY_RULES §4: "Session: gold_session, unchanged." Omitting it lets
        # weekend bars in, which on a 24/7 perpetual are thin off-hours flow and
        # are not the market the rule was measured on.
        if session == "gold_session":
            fire &= in_session
        # §11: new positions only inside 06:00-09:00 and 19:00-22:00 MYT.
        if windows:
            fire &= in_window

        panel = Panel(symbol=symbol, anchor=anchor, path_tf=path_tf or anchor,
                      start=lo_bound, end=hi_bound, tp_mult=tp_mult,
                      sl_mult=sl_mult, cost_bps=cost_bps)
        panel.warnings = warnings
        panel.fired = int(fire.sum())

        # Exit times of positions still open. `max_concurrent` slots in the SAME
        # symbol and direction — they are not diversification, and the
        # correlation shows up as longer losing runs (worst streak 9 -> 15 -> 20
        # as slots go 1 -> 2 -> 3). See OPERATING_PLAN §10.2.
        open_until: list[int] = []
        for i in np.flatnonzero(fire):
            if i + 1 >= ot.size:
                continue
            a = atr[i]
            if not np.isfinite(a) or a <= 0:
                continue
            open_until = [x for x in open_until if x > ot[i]]
            if len(open_until) >= max_concurrent:
                panel.skipped_in_position += 1
                continue
            entry_t = int(ot[i + 1])
            entry = float(opens[i + 1])
            if not np.isfinite(entry):
                continue
            if side == "long":
                tp, sl = entry + tp_mult * a, entry - sl_mult * a
            else:
                tp, sl = entry - tp_mult * a, entry + sl_mult * a
            reason, px, xt, amb = walk_forward(
                path_t, path_h, path_l, path_c, entry_t, entry, tp, sl,
                side == "long", horizon_ms)
            if reason == "nodata":
                continue
            gross = (px - entry) if side == "long" else (entry - px)
            cost = entry * cost_bps / 1e4
            panel.trades.append(Trade(
                side=side, entry_time=entry_t, entry_price=entry,
                exit_time=xt, exit_price=px, reason=reason, atr=float(a),
                r=gross / a, net=gross - cost, ambiguous=amb,
            ))
            open_until.append(int(xt))
        out[side] = panel
    return out


def summarise(panels: dict[str, Panel]) -> dict:
    allt = [t for p in panels.values() for t in p.trades]
    def block(ts: list[Trade]) -> dict:
        if not ts:
            return {"n": 0}
        wins = sum(1 for t in ts if t.net > 0)
        net = sum(t.net for t in ts)
        gains = sum(t.net for t in ts if t.net > 0)
        losses = -sum(t.net for t in ts if t.net <= 0)
        lo, hi = _wilson(wins, len(ts))
        return {
            "n": len(ts),
            "win_pct": round(wins / len(ts) * 100, 1),
            "ci": [round(lo, 1), round(hi, 1)],
            "net_per_unit": round(net, 2),
            "expectancy": round(net / len(ts), 4),
            "profit_factor": round(gains / losses, 3) if losses else float("inf"),
            "ambiguous": sum(1 for t in ts if t.ambiguous),
            "timeouts": sum(1 for t in ts if t.reason == "timeout"),
        }
    return {
        "pooled": block(allt),
        "long": block(panels["long"].trades),
        "short": block(panels["short"].trades),
        "fired": {s: p.fired for s, p in panels.items()},
        "skipped_in_position": {s: p.skipped_in_position for s, p in panels.items()},
    }


def breakeven_wr(tp_mult: float, sl_mult: float, atr: float, cost: float) -> float:
    """ENTRY_RULES §2: (SL*ATR + cost) / ((TP+SL)*ATR)."""
    return (sl_mult * atr + cost) / ((tp_mult + sl_mult) * atr) * 100


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    p = argparse.ArgumentParser(prog="python -m backtest.tools.validate_rule")
    p.add_argument("--symbol", default="MT5:GOLD")
    p.add_argument("--anchor", default="15m")
    p.add_argument("--tp", type=float, default=2.5)
    p.add_argument("--sl", type=float, default=2.5)
    p.add_argument("--cost-bps", type=float, default=None,
                   help="round-trip cost in bps of notional; defaults to 0.85 "
                        "for MT5 symbols (measured) and 11.0 for Bybit")
    p.add_argument("--no-5m", action="store_true", help="drop leg 5 (ENTRY_RULES §4.3)")
    p.add_argument("--dip-lookback", type=int, default=3)
    p.add_argument("--path-tf", default=None)
    p.add_argument("--from", dest="start", default=None)
    p.add_argument("--no-session", action="store_true",
                   help="drop the gold_session filter (ENTRY_RULES §4 keeps it)")
    p.add_argument("--windows", action="store_true",
                   help="restrict entries to 06-09 and 19-22 MYT (ENTRY_RULES §11)")
    p.add_argument("--max-concurrent", type=int, default=1,
                   help="simultaneous positions in the same symbol and direction "
                        "(OPERATING_PLAN §10.5 ships 2)")
    p.add_argument("--grid", action="store_true", help="sweep the TP/SL bracket")
    p.add_argument("--by-year", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)

    cost = a.cost_bps if a.cost_bps is not None else (
        0.85 if a.symbol.startswith("MT5:") else 11.0)
    start_ms = None
    if a.start:
        start_ms = int(datetime.strptime(a.start, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)

    with (Database(a.db) if a.db else Database()) as db:
        run = lambda tp, sl: measure(
            db, a.symbol, a.anchor, tp_mult=tp, sl_mult=sl, cost_bps=cost,
            use_5m=not a.no_5m, dip_lookback=a.dip_lookback,
            path_tf=a.path_tf, start_ms=start_ms,
            session="none" if a.no_session else "gold_session",
            windows=a.windows, max_concurrent=a.max_concurrent)

        panels = run(a.tp, a.sl)
        s = summarise(panels)
        ref = panels["long"]
        med_atr = float(np.median([t.atr for t in
                                   panels["long"].trades + panels["short"].trades])) \
            if s["pooled"]["n"] else float("nan")
        med_px = float(np.median([t.entry_price for t in
                                  panels["long"].trades + panels["short"].trades])) \
            if s["pooled"]["n"] else float("nan")
        be = breakeven_wr(a.tp, a.sl, med_atr, med_px * cost / 1e4) \
            if s["pooled"]["n"] else float("nan")

        if a.json:
            print(json.dumps({"summary": s, "breakeven_wr": be,
                              "median_atr": med_atr, "cost_bps": cost}, indent=1))
            return 0

        for w in ref.warnings:
            print(f"[!]     {w}")
        print(f"panel   {a.symbol} anchor {a.anchor}, path walked at {ref.path_tf}")
        print(f"        {_fmt_ms(ref.start)} -> {_fmt_ms(ref.end)}"
              f"   bracket TP{a.tp}/SL{a.sl} ATR"
              f"   cost {cost} bps"
              f"{'   (5m leg OFF)' if a.no_5m else ''}"
              f"{'   windows 06-09/19-22 MYT' if a.windows else ''}"
              f"{'   NO session filter' if a.no_session else ''}"
              f"{'' if a.max_concurrent == 1 else f'   {a.max_concurrent} slots'}")
        print(f"        median ATR {med_atr:.2f} on median price {med_px:,.2f}"
              f"  ->  break-even WR {be:.1f}%   null {NULL_WR}%")
        print()
        hdr = f"{'':7} {'n':>5} {'win%':>7} {'95% CI':>13} {'net/unit':>10} {'exp':>8} {'PF':>6} {'amb':>5}"
        print(hdr); print("-" * len(hdr))
        for k in ("pooled", "long", "short"):
            b = s[k]
            if not b["n"]:
                print(f"{k:7} {0:>5}"); continue
            print(f"{k:7} {b['n']:>5} {b['win_pct']:>6.1f}% "
                  f"{b['ci'][0]:>5.1f}-{b['ci'][1]:<6.1f} {b['net_per_unit']:>10.2f} "
                  f"{b['expectancy']:>8.3f} {b['profit_factor']:>6.2f} {b['ambiguous']:>5}"
                  f"  {b['timeouts']:>4} timeouts")
        print()
        print(f"signals fired: long {s['fired']['long']}, short {s['fired']['short']}"
              f"   skipped while in a position: "
              f"{s['skipped_in_position']['long'] + s['skipped_in_position']['short']}")
        pooled = s["pooled"]
        if pooled["n"]:
            verdict = ("CLEARS" if pooled["win_pct"] > be else "does NOT clear")
            print(f"verdict: pooled {pooled['win_pct']}% {verdict} break-even {be:.1f}%"
                  f"; CI {pooled['ci'][0]}-{pooled['ci'][1]}"
                  f"{' — CI straddles it' if pooled['ci'][0] <= be <= pooled['ci'][1] else ''}")

        if a.by_year:
            print("\nby year:")
            allt = panels["long"].trades + panels["short"].trades
            allt.sort(key=lambda t: t.entry_time)
            years: dict[int, list[Trade]] = {}
            for t in allt:
                y = datetime.fromtimestamp(t.entry_time / 1000, timezone.utc).year
                years.setdefault(y, []).append(t)
            for y, ts in sorted(years.items()):
                wins = sum(1 for t in ts if t.net > 0)
                net = sum(t.net for t in ts)
                print(f"  {y}  n={len(ts):>4}  win {wins/len(ts)*100:>5.1f}%  "
                      f"net {net:>9.2f}")

        if a.grid:
            print("\nbracket grid (pooled win% / net per unit):")
            cols = [2.0, 2.5, 3.0]
            print("        " + "".join(f"{'TP'+str(c):>18}" for c in cols))
            for sl in cols:
                row = f"  SL{sl:<4}"
                for tp in cols:
                    g = summarise(run(tp, sl))["pooled"]
                    row += (f"{g['win_pct']:>9.1f}% {g['net_per_unit']:>7.0f}"
                            if g["n"] else f"{'—':>18}")
                print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
