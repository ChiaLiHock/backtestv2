"""What actually happened to each emitted signal.

A signal in `reports/signals.jsonl` records a decision. It does not record an
outcome, because at the moment it is written there isn't one. This module walks
each one forward against real price and answers the only question that makes a
signal log worth keeping: **did it reach +25 or −25 first, and what did that
cost?**

Without this the trades panel shows every signal as `open · net 0.00` forever,
the win rate is 0/N, and there is no way to tell a good week from a bad one.

## The three rules that keep this honest

**1. The fill is the NEXT bar's open, not the signal bar's close.** The signal
records `entry_ref` (the close it was decided on) because that is what the rule
saw. Nobody can trade it — the bar had to close first. So the entry priced here
is the open of the following anchor bar, and the bracket is re-derived from that
fill, which is exactly what an executor would send. Using `entry_ref` would
flatter every result by the size of the opening gap.

**2. First touch is resolved on the finest series available.** A 15-minute bar
that spans both barriers cannot say which came first; a 1-minute walk usually
can. Where even the 1m bar straddles both, the trade is booked as a **loss** and
flagged `ambiguous` — the same conservative tie-break `validate_rule.walk_forward`
uses, because the alternative flatters the record at exactly the moments it
should not.

**3. A trade whose 24 hours have not elapsed is OPEN, not a timeout.**
`walk_forward` returns "timeout" when it runs out of bars, which is right for a
completed panel and wrong for a position opened forty minutes ago. That
distinction is checked explicitly here; without it every recent signal would be
booked at whatever price happened to be last, and today would always look
finished.

## Cost

`cost = entry × cost_bps / 1e4`, the same formula the measurement uses, applied
once per round trip. At 0.85 bps and gold near 4,670 that is **$0.40**, so a +25
win nets **+24.60** and a −25 loss nets **−25.40**. Break-even on a symmetric
bracket is therefore **50.8%**, not 50% — the gap is small and it is the whole
margin, so it is never dropped.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators
from ..tools.validate_rule import walk_forward
from .live_signal import RuleConfig

log = logging.getLogger(__name__)

# Series the first touch is resolved on. Finer is better; 1m is what the sync
# stores for every symbol and is what the harness uses as its `path_tf`.
PATH_TF = "1m"

# Round-trip cost in basis points of the entry price. `OPERATING_PLAN.md` uses
# 0.85 for MT5 GOLD, measured from the broker's own per-bar spread over 20,000
# M15 bars. Bybit's taker round trip is far higher (11 bps) — the signals are
# for the MT5 account, so the MT5 number is the honest one.
COST_BPS = 0.85


def _excursions(high: np.ndarray, low: np.ndarray, entry: float,
                long: bool) -> tuple[float, float]:
    """(MAE, MFE) in price, over the path actually walked."""
    if high.size == 0:
        return 0.0, 0.0
    if long:
        return float(entry - low.min()), float(high.max() - entry)
    return float(high.max() - entry), float(entry - low.min())


def resolve(
    db: Database,
    signals: list[dict],
    symbol: str,
    cfg: RuleConfig | None = None,
    indicators: IndicatorConfig | None = None,
    now_ms: int | None = None,
) -> list[dict]:
    """Every `would_enter` signal for ``symbol``, with its outcome filled in.

    Returns dicts in the shape the report's trades panel already draws, so the
    win/loss filter, the chart markers and the arrow-key stepping all work
    against live signals exactly as they did against backtested trades.
    """
    cfg = cfg or RuleConfig()
    icfg = indicators or IndicatorConfig()
    now_ms = now_ms or int(time.time() * 1000)
    repo = CandleRepository(db)

    rows = [r for r in signals
            if r.get("kind") != "link"
            and r.get("would_enter")
            and r.get("symbol") == symbol]
    if not rows:
        return []

    step = INTERVAL_MS[cfg.anchor]
    horizon_ms = cfg.time_stop_bars * step

    anchor = repo.load(symbol, cfg.anchor)
    if anchor.empty:
        return []
    a_t = anchor["open_time"].to_numpy(dtype="int64")
    a_o = anchor["open"].to_numpy(dtype="float64")
    # ATR at the SIGNAL bar, because an ATR bracket is frozen at the signal and
    # a fixed bracket ignores it entirely. Computed once for the whole frame.
    a_atr = compute_indicators(anchor, cfg.anchor, icfg)["atr_14"].to_numpy()

    path = repo.load(symbol, PATH_TF)
    if path.empty:
        path = anchor
        path_tf = cfg.anchor
    else:
        path_tf = PATH_TF
    p_t = path["open_time"].to_numpy(dtype="int64")
    p_h = path["high"].to_numpy(dtype="float64")
    p_l = path["low"].to_numpy(dtype="float64")
    p_c = path["close"].to_numpy(dtype="float64")

    long = cfg.side == "long"
    out: list[dict] = []

    for rec in rows:
        bar_ms = int(rec["bar_open_ms"])
        vetoed = bool((rec.get("extras") or {}).get("vetoed"))

        # The fill bar: the anchor bar AFTER the signal bar.
        k = int(np.searchsorted(a_t, bar_ms, "left"))
        fill_i = k + 1
        if fill_i >= a_t.size:
            out.append(_pending(rec, "waiting for the fill bar to open"))
            continue
        entry = float(a_o[fill_i])
        entry_ms = int(a_t[fill_i])
        atr = float(a_atr[k]) if k < a_atr.size and a_atr[k] == a_atr[k] else 0.0
        sl, tp = cfg.bracket(entry, atr)

        reason, exit_px, exit_ms, ambiguous = walk_forward(
            p_t, p_h, p_l, p_c, entry_ms, entry, tp, sl, long, horizon_ms)

        # `walk_forward` books a "timeout" when it runs out of bars. For a trade
        # whose 24 hours have not elapsed that is not a timeout — it is a
        # position that is still running, and pricing it now would invent an
        # exit. Checked rather than assumed.
        still_running = (reason == "timeout" and now_ms < entry_ms + horizon_ms)
        if reason == "nodata" or still_running:
            out.append(_pending(rec, "position still open", entry=entry,
                                entry_ms=entry_ms, sl=sl, tp=tp, vetoed=vetoed))
            continue

        lo = int(np.searchsorted(p_t, entry_ms, "left"))
        hi = int(np.searchsorted(p_t, exit_ms, "right"))
        mae, mfe = _excursions(p_h[lo:hi], p_l[lo:hi], entry, long)

        gross = (exit_px - entry) if long else (entry - exit_px)
        cost = entry * COST_BPS / 1e4
        net = gross - cost
        bars = max(1, int((exit_ms - entry_ms) // step))

        out.append({
            "id": f"sig-{bar_ms}",
            "side": rec.get("side", "long"),
            # Grouped so "show me only the vetoed ones" is one click, and so a
            # vetoed signal's counterfactual never lands in the headline P/L.
            "signal": "vetoed by risk gate" if vetoed else "rule + gate",
            "entry_time": entry_ms,
            "entry_price": round(entry, 2),
            "exit_time": exit_ms,
            "exit_price": round(float(exit_px), 2),
            "reason": reason,
            "net": round(net, 2),
            "gross": round(gross, 2),
            "fees": round(cost, 2),
            "mae": round(mae, 2),
            "mfe": round(mfe, 2),
            "bars": bars,
            "ambiguous": int(bool(ambiguous)),
            "sl": round(sl, 2), "tp": round(tp, 2),
            "signal_bar": bar_ms,
            "signal_ref": rec.get("entry_ref"),
            "vetoed": vetoed,
            "resolved": True,
            "path_tf": path_tf,
        })

    out.sort(key=lambda t: t["entry_time"])
    return out


def _pending(rec: dict, why: str, entry: float | None = None,
             entry_ms: int | None = None, sl: float | None = None,
             tp: float | None = None, vetoed: bool = False) -> dict:
    """A signal with no outcome yet. Net is 0 because it is UNKNOWN, not flat."""
    bar_ms = int(rec["bar_open_ms"])
    return {
        "id": f"sig-{bar_ms}",
        "side": rec.get("side", "long"),
        "signal": "vetoed by risk gate" if vetoed else "rule + gate",
        "entry_time": entry_ms or bar_ms,
        "entry_price": round(entry, 2) if entry else rec.get("entry_ref"),
        "exit_time": None, "exit_price": None,
        "reason": why,
        "net": 0.0, "gross": 0.0, "fees": 0.0, "mae": 0.0, "mfe": 0.0,
        "bars": 0, "ambiguous": 0,
        "sl": round(sl, 2) if sl else rec.get("sl"),
        "tp": round(tp, 2) if tp else rec.get("tp"),
        "signal_bar": bar_ms,
        "signal_ref": rec.get("entry_ref"),
        "vetoed": vetoed,
        "resolved": False,
        "path_tf": None,
    }


def summarise(trades: list[dict]) -> dict[str, Any]:
    """Win rate and P/L over the RESOLVED signals only.

    Open positions are counted and reported separately rather than folded in at
    zero. A win rate computed over "closed plus still-running" is not a win rate
    — it drifts toward 50% purely as a function of how recently you looked.
    """
    live = [t for t in trades if not t.get("vetoed")]
    done = [t for t in live if t.get("resolved")]
    open_ = [t for t in live if not t.get("resolved")]
    vetoed = [t for t in trades if t.get("vetoed")]

    if not done:
        return {
            "resolved": 0,
            "still open": len(open_),
            "vetoed by gate": len(vetoed),
            "note": "no signal has reached its target, stop or 24h limit yet",
        }

    wins = [t for t in done if t["net"] > 0]
    net = sum(t["net"] for t in done)
    gross = sum(t["gross"] for t in done)
    fees = sum(t["fees"] for t in done)
    win_pct = len(wins) / len(done) * 100
    lo, hi = _wilson(len(wins), len(done))
    reasons: dict[str, int] = {}
    for t in done:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    avg_win = (sum(t["net"] for t in wins) / len(wins)) if wins else 0.0
    losses = [t for t in done if t["net"] <= 0]
    avg_loss = (sum(t["net"] for t in losses) / len(losses)) if losses else 0.0
    pf = (sum(t["net"] for t in wins) / abs(sum(t["net"] for t in losses))
          if losses and sum(t["net"] for t in losses) else float("inf"))

    return {
        "resolved": f"{len(done)}",
        "win rate": f"{win_pct:.1f}% ({len(wins)}/{len(done)})",
        "95% CI": f"{lo:.1f}–{hi:.1f}%",
        # A symmetric bracket does NOT break even at 50% — the cost is the
        # entire margin, so it is stated next to the number it decides.
        "break-even": "50.8% (cost 0.85 bps)",
        "net P/L": f"{net:+,.2f}",
        "gross": f"{gross:+,.2f}",
        "fees": f"{fees:,.2f}",
        "avg win / loss": f"{avg_win:+.2f} / {avg_loss:+.2f}",
        "profit factor": f"{pf:.2f}" if pf != float("inf") else "—",
        "exits": " · ".join(f"{k}={v}" for k, v in sorted(reasons.items())),
        "still open": len(open_),
        "vetoed by gate": len(vetoed),
    }


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval — the same one the harness reports, for comparability."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - m) * 100, min(1.0, c + m) * 100)
