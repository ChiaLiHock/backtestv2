"""Render a replica of the app's Chart screen so indicator parity can be eyeballed.

Colours, overlay set and window size are taken from the app, not approximated:

* palette  -> `ui/theme/Color.kt:23-46` (TradingColors)
* overlays -> `ui/MarketViewModel.kt:70` — defaults are UT, EMA, ZONES, SIGNALS
              (Bollinger and VWAP are off by default, which is why the app
              screenshot shows no grey bands and no teal line)
* window   -> `ui/chart/CandleChart.kt:413` DEFAULT_VISIBLE_BARS = 90
* draw order -> `CandleChart.kt:214-230`: zones, candles, then BB, EMA, VWAP, UT

Usage:
    python -m backtest.tools.plot_chart --tf 30m --bars 90 --out chart_30m.png
    python -m backtest.tools.plot_chart --tf 30m --end 2026-08-21T23:30:00Z
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from ..data.db import CandleRepository, Database
from ..indicators import structure as st
from ..indicators.base import IndicatorConfig
from ..indicators.registry import compute_indicators
from ..indicators.trend import atr as atr_fn

# --- TradingColors, ui/theme/Color.kt:23-46 --------------------------------
C_BG = "#0E1116"          # Ink900
C_PANEL = "#161B22"       # Ink800
C_UP = "#2EBD85"          # candleUp
C_DOWN = "#F6465D"        # candleDown
C_GRID = "#232A33"        # gridLine
C_AXIS = "#6E7B8A"        # axisText
C_UT = "#D4AF37"          # utLine (GoldAccent)
C_EMA7 = "#58A6FF"
C_EMA14 = "#BC8CFF"
C_EMA28 = "#FF9E64"
C_BB = "#7D8590"
C_VWAP = "#39D3C3"
C_SUPPORT = "#2EBD85"     # supportZone, drawn at 0x33 = 20% alpha
C_RESIST = "#F6465D"      # resistanceZone
ZONE_ALPHA = 0x33 / 255.0
C_TEXT = "#E6EDF3"

DEFAULT_VISIBLE_BARS = 90

# Zones are built market-wide from these, not per timeframe
# (`MarketAnalysisEngine.kt:141-151`).
STRUCTURAL_TFS = ("30m", "1h", "4h")
DAILY_TF = "1h"       # dailyCandles source
REFERENCE_TF = "1h"   # referenceAtr source (the direction anchor, H1)


def _fmt(ms: int, tz: timezone = timezone.utc) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M")


def build_zones(
    repo: CandleRepository, symbol: str, cfg: IndicatorConfig, end_ms: int | None
) -> tuple[st.KeyLevels, float]:
    """Reproduce `MarketAnalysisEngine.buildKeyLevels` at one point in time."""
    # Windowed to the app's 500-bar buffer. Zone construction is genuinely
    # window-dependent, not merely warm-up sensitive — see IndicatorConfig
    # .app_window_bars and docs/OPEN_QUESTIONS.md Q4.
    win = cfg.app_window_bars

    swings: dict[str, list[st.SwingPoint]] = {}
    for tf in STRUCTURAL_TFS:
        df = repo.load(symbol, tf, end_ms=end_ms).tail(win).reset_index(drop=True)
        if df.empty:
            continue
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        a = atr_fn(high, low, df["close"].to_numpy(), cfg.atr_period)
        swings[tf] = st.final_accepted_chain(
            high, low, df["open_time"].to_numpy(dtype="int64"), a,
            st.swing_lookback_for(tf), cfg.swing_min_separation_atr,
        )

    daily = repo.load(symbol, DAILY_TF, end_ms=end_ms).tail(win).reset_index(drop=True)
    ref = repo.load(symbol, REFERENCE_TF, end_ms=end_ms).tail(win).reset_index(drop=True)
    ref_atr = atr_fn(
        ref["high"].to_numpy(), ref["low"].to_numpy(), ref["close"].to_numpy(),
        cfg.atr_period,
    )
    reference_atr = float(ref_atr[-1]) if ref_atr.size else float("nan")
    current_price = float(daily["close"].iloc[-1]) if not daily.empty else float("nan")

    levels = st.build_key_levels(
        current_price=current_price,
        swings_by_timeframe=swings,
        daily_open_time=daily["open_time"].to_numpy(dtype="int64"),
        daily_high=daily["high"].to_numpy(),
        daily_low=daily["low"].to_numpy(),
        daily_close=daily["close"].to_numpy(),
        reference_atr=reference_atr,
    )
    return levels, reference_atr


def plot(
    symbol: str,
    tf: str,
    bars: int,
    end_ms: int | None,
    out_path: Path,
    show_bb: bool,
    show_vwap: bool,
    tz: timezone = timezone.utc,
    tz_label: str = "UTC",
) -> dict[str, float]:
    cfg = IndicatorConfig()
    db = Database()
    repo = CandleRepository(db)

    df = repo.load(symbol, tf, end_ms=end_ms)
    if df.empty:
        raise SystemExit(f"no candles for {symbol} {tf}")

    ind = compute_indicators(df, tf, cfg)
    levels, _ = build_zones(repo, symbol, cfg, end_ms)

    # Window is the LAST `bars` bars, matching the app's initial view.
    n = len(df)
    start = max(0, n - bars)
    w = slice(start, n)

    # Rendered in `tz` because the app's chart axis uses the DEVICE's zone with no
    # locale pinned (`CandleChart.kt`), which for this user is MYT (UTC+8).
    t = np.array([datetime.fromtimestamp(ms / 1000, tz) for ms in df["open_time"][w]])
    o = df["open"].to_numpy()[w]
    h = df["high"].to_numpy()[w]
    lo = df["low"].to_numpy()[w]
    c = df["close"].to_numpy()[w]

    fig, ax = plt.subplots(figsize=(13, 8.5), dpi=140)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)

    # --- 1. S/R zones first, so candles draw on top (CandleChart.kt:323-324)
    for z in levels.resistance:
        ax.add_patch(Rectangle(
            (mdates.date2num(t[0]), z.low),
            mdates.date2num(t[-1]) - mdates.date2num(t[0]), z.high - z.low,
            facecolor=C_RESIST, alpha=ZONE_ALPHA, edgecolor="none", zorder=1,
        ))
    for z in levels.support:
        ax.add_patch(Rectangle(
            (mdates.date2num(t[0]), z.low),
            mdates.date2num(t[-1]) - mdates.date2num(t[0]), z.high - z.low,
            facecolor=C_SUPPORT, alpha=ZONE_ALPHA, edgecolor="none", zorder=1,
        ))

    # --- 2. candles (CandleChart.kt:338-341): body + wick, coloured by isUp
    step = (mdates.date2num(t[1]) - mdates.date2num(t[0])) if len(t) > 1 else 0.01
    body_w = step * 0.62
    for i in range(len(t)):
        col = C_UP if c[i] >= o[i] else C_DOWN   # Candle.isUp is close >= open
        x = mdates.date2num(t[i])
        ax.plot([x, x], [lo[i], h[i]], color=col, linewidth=0.9, zorder=2)
        bottom, height = min(o[i], c[i]), abs(c[i] - o[i])
        ax.add_patch(Rectangle(
            (x - body_w / 2, bottom), body_w, max(height, 1e-9),
            facecolor=col, edgecolor=col, linewidth=0.6, zorder=3,
        ))

    # --- 3. overlays, in the app's draw order (CandleChart.kt:214-230)
    if show_bb:
        ax.plot(t, ind["bb_upper"].to_numpy()[w], color=C_BB, lw=1.0, zorder=4)
        ax.plot(t, ind["bb_middle"].to_numpy()[w], color=C_BB, lw=1.0, alpha=0.6, zorder=4)
        ax.plot(t, ind["bb_lower"].to_numpy()[w], color=C_BB, lw=1.0, zorder=4)
    ax.plot(t, ind["ema_7"].to_numpy()[w], color=C_EMA7, lw=1.5, zorder=5, label="EMA 7")
    ax.plot(t, ind["ema_14"].to_numpy()[w], color=C_EMA14, lw=1.5, zorder=5, label="EMA 14")
    ax.plot(t, ind["ema_28"].to_numpy()[w], color=C_EMA28, lw=1.5, zorder=5, label="EMA 28")
    if show_vwap:
        ax.plot(t, ind["vwap"].to_numpy()[w], color=C_VWAP, lw=1.6, zorder=5, label="VWAP")
    # UT last and thickest — it is the headline level.
    ax.plot(t, ind["ut_level"].to_numpy()[w], color=C_UT, lw=2.0, zorder=6,
            label="UT Dynamic Level")

    # --- 4. signal markers (CandleChart.kt:396-405)
    buy = ind["ut_buy"].to_numpy()[w]
    sell = ind["ut_sell"].to_numpy()[w]
    if buy.any():
        ax.scatter(t[buy], lo[buy] - (h - lo).mean() * 0.8, marker="^", s=70,
                   color=C_UP, zorder=7, label="UT buy")
    if sell.any():
        ax.scatter(t[sell], h[sell] + (h - lo).mean() * 0.8, marker="v", s=70,
                   color=C_DOWN, zorder=7, label="UT sell")

    # --- y range: candles, plus BB/UT when shown. Zones are deliberately NOT
    # included, matching `CandleChart.kt:184-190` — a distant zone must not
    # squash the price action, it just gets clipped at the edge.
    y_hi, y_lo = float(np.nanmax(h)), float(np.nanmin(lo))
    extra = [ind["ut_level"].to_numpy()[w]]
    if show_bb:
        extra += [ind["bb_upper"].to_numpy()[w], ind["bb_lower"].to_numpy()[w]]
    for arr in extra:
        if np.isfinite(arr).any():
            y_hi = max(y_hi, float(np.nanmax(arr)))
            y_lo = min(y_lo, float(np.nanmin(arr)))
    pad = (y_hi - y_lo) * 0.08 or 1.0
    ax.set_ylim(y_lo - pad, y_hi + pad)
    ax.set_xlim(t[0], t[-1])

    # --- chrome
    ax.grid(True, color=C_GRID, lw=0.7, zorder=0)
    ax.tick_params(colors=C_AXIS, labelsize=9)
    for s in ax.spines.values():
        s.set_color(C_GRID)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b\n%H:%M", tz=timezone.utc))

    last = ind.iloc[-1]
    ax.set_title(
        f"{symbol} · {tf} · Python port (CONFIRMED bars)\n"
        f"{_fmt(int(df['open_time'].iloc[start]), tz)} → "
        f"{_fmt(int(df['open_time'].iloc[-1]), tz)} {tz_label}   |   "
        f"UT key={cfg.ut_key_value} atr={cfg.ut_atr_period}",
        color=C_TEXT, fontsize=11, pad=14,
    )
    leg = ax.legend(loc="upper left", framealpha=0.85, facecolor=C_PANEL,
                    edgecolor=C_GRID, fontsize=9)
    for txt in leg.get_texts():
        txt.set_color(C_TEXT)

    # Right-edge price tag, the way the app pins it (CandleChart.kt:265).
    ax.annotate(
        f"{c[-1]:,.2f}", xy=(1.0, c[-1]), xycoords=("axes fraction", "data"),
        xytext=(6, 0), textcoords="offset points", va="center",
        color="#000000", fontsize=9, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.3", facecolor=C_UT, edgecolor="none"),
    )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=C_BG)
    plt.close(fig)

    # LegendCard values (ChartScreen.kt:242-248) for a numeric cross-check.
    return {
        "close": float(c[-1]),
        "UT Dynamic Level": float(last["ut_level"]),
        "EMA 7": float(last["ema_7"]),
        "EMA 14": float(last["ema_14"]),
        "EMA 28": float(last["ema_28"]),
        "Bollinger upper": float(last["bb_upper"]),
        "Bollinger lower": float(last["bb_lower"]),
        "VWAP": float(last["vwap"]),
        "RSI": float(last["rsi_14"]),
        "ADX": float(last["adx_14"]),
        "BB %B": float(last["bb_percent_b"]),
        "Relative volume": float(last["relative_volume"]),
    }


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m backtest.tools.plot_chart")
    p.add_argument("--symbol", default="XAUUSDT")
    p.add_argument("--tf", default="30m")
    p.add_argument("--bars", type=int, default=DEFAULT_VISIBLE_BARS)
    p.add_argument("--end", default=None, help="ISO UTC; omit for the newest bar")
    p.add_argument("--out", default=None)
    p.add_argument("--bollinger", action="store_true", help="off by default, as in the app")
    p.add_argument("--vwap", action="store_true", help="off by default, as in the app")
    p.add_argument("--tz", default="UTC",
                   help="display timezone: UTC, or MYT / +8 for Asia/Kuala_Lumpur")
    args = p.parse_args()

    end_ms = None
    if args.end:
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                end_ms = int(
                    datetime.strptime(args.end, fmt)
                    .replace(tzinfo=timezone.utc)
                    .timestamp() * 1000
                )
                break
            except ValueError:
                continue
        if end_ms is None:
            raise SystemExit(f"cannot parse --end {args.end!r}")

    raw = args.tz.strip().upper()
    if raw in ("UTC", "Z", "+0", "0"):
        tz, tz_label = timezone.utc, "UTC"
    elif raw in ("MYT", "+8", "8", "ASIA/KUALA_LUMPUR"):
        tz, tz_label = timezone(timedelta(hours=8)), "MYT"
    else:
        raise SystemExit(f"unsupported --tz {args.tz!r}; use UTC or MYT")

    out = Path(args.out) if args.out else Path(f"chart_{args.symbol}_{args.tf}.png")
    values = plot(args.symbol, args.tf, args.bars, end_ms, out, args.bollinger,
                  args.vwap, tz, tz_label)

    print(f"wrote {out}")
    print("\nLegend (compare against the app's Chart page legend card):")
    for k, v in values.items():
        print(f"  {k:<20} {v:,.4f}" if v == v else f"  {k:<20} —")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
