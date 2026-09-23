"""Run-level performance metrics.

Every number here is a statement about the past under the config's assumptions.
None of them is a forecast, and the sample-size caveats are part of the output
rather than a footnote — a win rate over 40 trades and one over 4,000 trades are
different kinds of object and the report says so.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

# Below this, a win rate is not distinguishable from noise at any useful
# resolution. The master spec asks for a warning under 100 trades; the binomial
# maths agrees — at n=100 the 95% CI on a 55% win rate is roughly +/-10 points.
MIN_TRADES_CONFIDENT = 100
MIN_BUCKET_CONFIDENT = 30


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because it stays inside [0, 1] and
    behaves at small n — which is exactly the regime these runs live in.
    """
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def max_drawdown(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity - peak))


def longest_losing_streak(pnls: Sequence[float]) -> int:
    best = current = 0
    for p in pnls:
        if p < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def summarise(trades: Sequence[Any]) -> dict[str, Any]:
    """Headline metrics for one run. Accepts Trade objects or dict-likes."""
    def get(t: Any, name: str) -> Any:
        return t[name] if isinstance(t, dict) else getattr(t, name)

    n = len(trades)
    if n == 0:
        return {"trades": "0"}

    pnl = np.array([float(get(t, "net_pnl")) for t in trades])
    wins = pnl > 0
    losses = pnl < 0
    n_wins = int(wins.sum())

    gross_win = float(pnl[wins].sum()) if n_wins else 0.0
    gross_loss = float(-pnl[losses].sum()) if losses.any() else 0.0
    equity = np.cumsum(pnl)

    lo, hi = wilson_interval(n_wins, n)
    expectancy = float(pnl.mean())
    sharpe = float(pnl.mean() / pnl.std(ddof=1)) if n > 1 and pnl.std(ddof=1) > 0 else 0.0

    ambiguous = sum(int(get(t, "ambiguous")) for t in trades)
    bars = [int(get(t, "bars_held")) for t in trades]

    reasons: dict[str, int] = {}
    for t in trades:
        r = get(t, "exit_reason") or "open"
        reasons[r] = reasons.get(r, 0) + 1

    out: dict[str, Any] = {
        "trades": f"{n}",
        "win rate": f"{n_wins / n * 100:.1f}%  (95% CI {lo * 100:.1f}-{hi * 100:.1f}%)",
        "wins / losses": f"{n_wins} / {int(losses.sum())}",
        "net P/L": f"{pnl.sum():,.2f}",
        "expectancy / trade": f"{expectancy:,.2f}",
        "avg win": f"{pnl[wins].mean():,.2f}" if n_wins else "-",
        "avg loss": f"{pnl[losses].mean():,.2f}" if losses.any() else "-",
        "profit factor": f"{gross_win / gross_loss:.2f}" if gross_loss > 0 else "inf (no losers)",
        "max drawdown": f"{max_drawdown(equity):,.2f}",
        "Sharpe (per trade)": f"{sharpe:.2f}",
        "longest losing streak": f"{longest_losing_streak(pnl)}",
        "avg bars held": f"{np.mean(bars):.1f}",
        "ambiguous trades": f"{ambiguous}  ({ambiguous / n * 100:.1f}%)",
        "exit reasons": ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())),
    }

    if n < MIN_TRADES_CONFIDENT:
        out["! sample"] = (
            f"only {n} trades — the CI above is wide enough that this cannot "
            f"distinguish a real edge from chance"
        )
    return out
