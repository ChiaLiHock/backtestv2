"""Live risk read — a Python port of `RiskExecutiveEngine.kt`.

PURE and side-effect-free. It performs no I/O and calls no API. It consumes
already-computed indicator rows plus a raw candle series and returns a
`RiskSnapshot`: an independent **Danger%** and **Confidence%** for BOTH
directions, a structural **Bias**, a "Show Why" reason breakdown, and a
lightweight simulated-history probability.

Danger is the source file's weighted danger-vector matrix — **Trend & EMA 40% /
Order-Book 30% / Momentum 30%** — a deterministic simulation of risk, and
deliberately *not* a heavy historical backtest. A full `Backtester.run()` over
166 days takes ~7 s per symbol; this runs in single-digit milliseconds, which is
what makes it usable on a 5-second live loop.

---

## What is a faithful port, and what is not

Every weight, reference constant and blend in this file is transcribed from
`RiskExecutiveEngine.kt` unchanged. Three of its **inputs** do not exist in this
project, and pretending otherwise would be the worst possible outcome — a number
on a page that looks measured and is invented. So:

**1. There is no order book here, at all.** The Kotlin's 30% ORDER_BOOK factor
reads `LiquidityWall` (a resting bid/ask with a notional size). This project
holds Bybit *klines* and MT5 *rates* — no depth, ever. The substitute is this
repo's own **KEY LEVELS zones** (`indicators/zones.py`): swing / previous-day /
session levels with a `strength` score. Zone distance stands in for wall
distance, and `strength` stands in for notional.

That is a port of the *idea*, not of the data. A resting wall is liquidity that
exists right now; a zone is a price people defended in the past. They are not the
same claim, and `RiskSnapshot.orderbook_is_proxy` is True so the UI can say so.

**2. There is no open interest and no long/short ratio.** `BybitClient` can fetch
OI (`open_interest()`) but nothing syncs it, and the `open_interest` table is
empty; MT5 has no OI at all. Rather than substitute a guess, the OI term is
**skipped** when `oi_deviation_pct is None`, and `data_completeness` scores lower
for its absence — which is exactly what the Kotlin already does for a missing
input. A missing input should lower confidence, not silently read as zero danger.

**3. Wick bias is derived here, not ported.** The Kotlin reads
`TfCandleSignal.signal.wickBias` from an analyzer this repo does not have. It is
computed in `wick_bias()` below from candle geometry, and the rule is stated
there rather than hidden.

## The bracket is in ATR or in dollars, never in percent

The Kotlin takes `takeProfitPct` / `stopLossPct`. Nothing here trades in percent.
`RiskSettings` therefore carries the live bracket in its own units — ATR
multiples (`OPERATING_PLAN.md` §8's TP 5.0 / SL 2.5) or a fixed distance in price
(`tp_usd` / `sl_usd`) — and converts to percent at evaluation time, so the percent
maths below is unchanged while the barriers being simulated are the ones actually
sent to the broker.

The Kotlin's TP_MIN/TP_MAX clamps still apply to an ATR bracket, where a spike in
ATR really can produce an absurd target. They are **not** applied to a fixed
bracket: that number was typed by a person and is absurd or not on its own terms,
and silently widening a $10 stop to satisfy a 0.5%-of-price floor would have the
sim measure a trade nobody is taking.

## What this is for

It is a **risk read, not a trigger**. Its weights are hand-set in the source file
and have never been measured on gold: `docs/FEATURE_MINE.md` records six mined
filters rejected on this data, and nothing here has been through that. The
measured edge is the 9-leg rule in `engine/live_signal.py` (+3.3 points against a
matched random-entry null, `OPERATING_PLAN.md` §8.1). So this engine **gates**
that rule — it can veto, it cannot fire. See `gate()` at the end of this file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

# --- Danger-vector weights (the 40/30/30 matrix). RiskExecutiveEngine.kt:29-31
W_TREND = 0.40
W_ORDERBOOK = 0.30
W_MOMENTUM = 0.30

# --- Momentum tuning. :34-37
MOVE_LOOKBACK = 12       # candles ~= last hour on 5m, for recent-move intensity
MOVE_REF_PCT = 4.0       # a 4% move over that window ~= "intense" (-> 1.0)
ATR_VOL_REF_PCT = 2.5    # ATR of 2.5% of price ~= high whipsaw danger
OI_REF_PCT = 40.0        # OI deviation that saturates the "aggression" term

# --- Historical double-barrier sim. :40-45
SIM_HORIZON = 48         # forward candles (~4 h on 5m, one strategy cycle)
SIM_EMA_TOL_PCT = 0.5    # EMA28-distance match tolerance (spec)
SIM_MIN_MATCHES = 8      # below this the sim is too thin to weight
SIM_BLEND = 0.20         # how much the sim's SL-first prob nudges Danger
SIM_WARMUP = 50          # indices before this lack stable indicators

RSI_PERIOD = 14
ATR_PERIOD = 14

# Wick-bias classification (NOT ported — see the module docstring). A candle is a
# rejection when one wick is at least this multiple of the body AND at least this
# fraction of the whole range. Both tests are needed: the body ratio alone fires
# on every doji, and the range ratio alone fires on any long candle with a tail.
WICK_BODY_RATIO = 1.5
WICK_RANGE_FRAC = 0.45

BEARISH_REJECTION = "BEARISH_REJECTION"
BULLISH_REJECTION = "BULLISH_REJECTION"
WICK_NEUTRAL = "NEUTRAL"

BULLISH, BEARISH, NEUTRAL = "BULLISH", "BEARISH", "NEUTRAL"
LONG, SHORT = "LONG", "SHORT"
DANGER, SUPPORT = "DANGER", "SUPPORT"
TREND_EMA, ORDER_BOOK, MOMENTUM = "TREND_EMA", "ORDER_BOOK", "MOMENTUM"

# Per-timeframe weight for the multi-timeframe wick vote. Slower timeframes carry
# more, matching `RiskSettings.weights.forLabel` in the Kotlin and the app's own
# MultiTimeframeEngine ordering.
DEFAULT_TF_WEIGHTS: dict[str, float] = {
    "5m": 0.5, "15m": 0.8, "30m": 1.0, "1h": 1.4, "4h": 1.8,
}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskSettings:
    """The bracket being risk-assessed, plus the sim's search window.

    `tp_atr` / `sl_atr` are ATR multiples because that is what the shipped system
    uses; they become the Kotlin's percent barriers in `evaluate()` once the live
    ATR is known.
    """

    tp_atr: float = 5.0            # OPERATING_PLAN §8
    sl_atr: float = 2.5
    # A FIXED distance in price units, when the live bracket is a fixed one.
    # Set these and the ATR multiples are ignored — the sim and the order-book
    # factor then measure the barriers actually being sent, not a proxy for them.
    tp_usd: float | None = None
    sl_usd: float | None = None
    lookback_bars: int = 5000      # how far back the sim looks for analogues
    lookback_label: str = "5,000 bars"
    # The Kotlin fixes this at 48 (4 h of 5m). The shipped time stop is 96 x 15m
    # = 24 h, so a caller measuring THIS system passes 288 and gets a sim whose
    # horizon matches the trade it is describing.
    sim_horizon: int = SIM_HORIZON
    horizon_label: str = "4h"
    tf_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TF_WEIGHTS))

    TP_MIN, TP_MAX = 0.5, 20.0
    SL_MIN, SL_MAX = 0.25, 10.0


@dataclass(frozen=True)
class TfCandleSignal:
    """One timeframe's contribution. Mirrors the Kotlin's `TfCandleSignal`."""

    label: str                      # "5m", "1h", ...
    wick_bias: str = WICK_NEUTRAL
    relative_volume: float = 1.0
    ut_bias: int = 0                # -1 / 0 / +1, from indicators/trend.ut_bias
    confirmed_bias: int = 0         # -1 / 0 / +1, from directional_score
    usable: bool = True             # >= 60 closed bars (TimeframeAnalyzer.kt:66)


@dataclass(frozen=True)
class Zone:
    """A KEY LEVELS support/resistance band, standing in for a LiquidityWall.

    `strength` replaces the wall's `notionalUsd`. See the module docstring: this
    is a proxy and is labelled as one everywhere it surfaces.
    """

    low: float
    high: float
    strength: float
    sources: tuple[str, ...] = ()

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def distance_from(self, spot: float) -> float:
        """0 when spot is inside the band, else the gap to the nearer edge."""
        if self.low <= spot <= self.high:
            return 0.0
        return self.low - spot if spot < self.low else spot - self.high


@dataclass(frozen=True)
class RiskInputs:
    """Everything `evaluate()` reads. Assembled by the caller from the DB."""

    spot: float
    open_: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema_7: float | None = None
    ema_14: float | None = None
    ema_28: float | None = None
    ema_50: float | None = None
    ema_200: float | None = None
    ema_28_series: np.ndarray | None = None
    rsi_series: np.ndarray | None = None
    atr: float | None = None
    resistance: tuple[Zone, ...] = ()
    support: tuple[Zone, ...] = ()
    tf_signals: tuple[TfCandleSignal, ...] = ()
    oi_deviation_pct: float | None = None    # absent in this project — see docstring
    long_ratio: float | None = None
    short_ratio: float | None = None
    zones_are_stale_by_ms: int | None = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskReason:
    direction: str
    polarity: str      # DANGER | SUPPORT
    factor: str        # TREND_EMA | ORDER_BOOK | MOMENTUM
    text: str

    def as_dict(self) -> dict:
        return {"direction": self.direction, "polarity": self.polarity,
                "factor": self.factor, "text": self.text}


@dataclass(frozen=True)
class HistoricalSim:
    matches: int
    long_tp_first: int
    long_sl_first: int
    short_tp_first: int
    short_sl_first: int
    lookback_label: str
    horizon_label: str
    summary: str

    def long_sl_first_prob(self, min_matches: int = SIM_MIN_MATCHES) -> float | None:
        n = self.long_tp_first + self.long_sl_first
        if self.matches < min_matches or n == 0:
            return None
        return self.long_sl_first / n

    def short_sl_first_prob(self, min_matches: int = SIM_MIN_MATCHES) -> float | None:
        n = self.short_tp_first + self.short_sl_first
        if self.matches < min_matches or n == 0:
            return None
        return self.short_sl_first / n

    def as_dict(self) -> dict:
        return {
            "matches": self.matches,
            "long_tp_first": self.long_tp_first, "long_sl_first": self.long_sl_first,
            "short_tp_first": self.short_tp_first, "short_sl_first": self.short_sl_first,
            "lookback_label": self.lookback_label, "horizon_label": self.horizon_label,
            "summary": self.summary,
            "long_sl_first_prob": _r(self.long_sl_first_prob(), 3),
            "short_sl_first_prob": _r(self.short_sl_first_prob(), 3),
        }


@dataclass(frozen=True)
class RiskDirectionScore:
    direction: str
    danger: int          # 0..100
    confidence: int      # 0..100
    trend_pts: int
    orderbook_pts: int
    momentum_pts: int
    reasons: tuple[RiskReason, ...]

    def as_dict(self) -> dict:
        return {
            "direction": self.direction, "danger": self.danger,
            "confidence": self.confidence, "trend_pts": self.trend_pts,
            "orderbook_pts": self.orderbook_pts, "momentum_pts": self.momentum_pts,
            "reasons": [r.as_dict() for r in self.reasons],
        }


@dataclass(frozen=True)
class RiskSnapshot:
    long: RiskDirectionScore
    short: RiskDirectionScore
    bias: str
    historical_sim: HistoricalSim | None
    data_confidence_note: str
    tp_pct: float
    sl_pct: float
    orderbook_is_proxy: bool = True
    missing_inputs: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "long": self.long.as_dict(),
            "short": self.short.as_dict(),
            "bias": self.bias,
            "historical_sim": self.historical_sim.as_dict() if self.historical_sim else None,
            "data_confidence_note": self.data_confidence_note,
            "tp_pct": _r(self.tp_pct, 3), "sl_pct": _r(self.sl_pct, 3),
            "orderbook_is_proxy": self.orderbook_is_proxy,
            "missing_inputs": list(self.missing_inputs),
        }


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def evaluate(inp: RiskInputs, settings: RiskSettings | None = None) -> RiskSnapshot:
    """Danger / Confidence / Bias / reasons / sim. Pure."""
    s = settings or RiskSettings()
    spot = float(inp.spot) if inp.spot and inp.spot > 0 else (
        float(inp.close[-1]) if inp.close.size else 0.0)
    atr = float(inp.atr) if inp.atr and math.isfinite(inp.atr) else 0.0

    # The live bracket -> the percent barriers the Kotlin maths expects, then
    # clamped to its own bounds so an absurd ATR cannot produce an absurd target.
    # A fixed-dollar bracket is converted against spot; an ATR bracket against
    # the ATR. Either way what is simulated below is what the executor would send.
    fixed = s.tp_usd is not None or s.sl_usd is not None
    up = s.tp_usd if s.tp_usd is not None else s.tp_atr * atr
    dn = s.sl_usd if s.sl_usd is not None else s.sl_atr * atr
    if spot > 0:
        tp_raw, sl_raw = up / spot * 100.0, dn / spot * 100.0
        # A fixed bracket is only floored away from zero/NaN; an ATR one keeps the
        # Kotlin's bounds, which exist because ATR can spike. See the docstring.
        tp = tp_raw if fixed and tp_raw > 0 else _clamp(tp_raw, s.TP_MIN, s.TP_MAX)
        sl = sl_raw if fixed and sl_raw > 0 else _clamp(sl_raw, s.SL_MIN, s.SL_MAX)
    else:
        tp, sl = s.TP_MIN, s.SL_MIN
    atr_pct = (atr / spot * 100.0) if spot > 0 else 0.0

    rsi = _last_finite(inp.rsi_series) if inp.rsi_series is not None else None
    rsi = 50.0 if rsi is None else rsi

    # ---- Factor 1: Trend & EMA rejection (40%) --------------------------- :91-104
    short_emas = [e for e in (inp.ema_7, inp.ema_14, inp.ema_28) if _ok(e)]
    all_emas = [e for e in (inp.ema_7, inp.ema_14, inp.ema_28,
                            inp.ema_50, inp.ema_200) if _ok(e)]
    below_short = _frac(short_emas, lambda e: spot < e)
    above_short = _frac(short_emas, lambda e: spot > e)
    below_all = _frac(all_emas, lambda e: spot < e)

    wick_bear = _weighted_wick(inp.tf_signals, s.tf_weights, BEARISH_REJECTION)
    wick_bull = _weighted_wick(inp.tf_signals, s.tf_weights, BULLISH_REJECTION)

    long_trend = _clamp01(0.55 * below_short + 0.45 * wick_bear)
    short_trend = _clamp01(0.55 * above_short + 0.45 * wick_bull)

    # ---- Factor 2: zones standing in for the order book (30%) ----------- :106-110
    zones = tuple(inp.resistance) + tuple(inp.support)
    max_strength = max((z.strength for z in zones), default=0.0) or 1.0
    long_ob = _zone_danger(inp, spot, tp, sl, max_strength, for_long=True)
    short_ob = _zone_danger(inp, spot, tp, sl, max_strength, for_long=False)

    # ---- Factor 3: volatility & momentum exhaustion (30%) --------------- :112-135
    recent_return_pct = _recent_return_pct(inp.close, MOVE_LOOKBACK)
    down_move = _clamp01(-recent_return_pct / MOVE_REF_PCT)
    up_move = _clamp01(recent_return_pct / MOVE_REF_PCT)
    exhaustion_down = _clamp01((30.0 - rsi) / 20.0)   # oversold degree
    exhaustion_up = _clamp01((rsi - 70.0) / 20.0)     # overbought degree

    # No OI in this project. `None` means "skip this term and reweight", not
    # "zero danger" — see the module docstring.
    has_oi = inp.oi_deviation_pct is not None
    oi_expand = _clamp01((inp.oi_deviation_pct or 0.0) / OI_REF_PCT) if has_oi else None

    rel_vol = next((t.relative_volume for t in inp.tf_signals if t.label == "5m"), None)
    if rel_vol is None:
        rel_vol = next((t.relative_volume for t in inp.tf_signals), 1.0)
    vol_amp = _clamp01((rel_vol - 1.0) / 0.8)          # 0 at avg vol, 1 at climax
    vol_danger = _clamp01(atr_pct / ATR_VOL_REF_PCT)   # whipsaw, shared both sides

    long_mom = _momentum_danger(
        chase=up_move * exhaustion_up,                 # buying an exhausted rally
        knife=down_move * (1.0 - exhaustion_down),     # a fresh falling knife
        oi=(down_move * oi_expand) if oi_expand is not None else None,
        vol_amp=vol_amp, vol_danger=vol_danger,
    )
    short_mom = _momentum_danger(
        chase=down_move * exhaustion_down,             # selling an exhausted dump
        knife=up_move * (1.0 - exhaustion_up),
        oi=(up_move * oi_expand) if oi_expand is not None else None,
        vol_amp=vol_amp, vol_danger=vol_danger,
    )

    # ---- Historical double-barrier simulation --------------------------- :137-138
    sim = simulate(inp, tp, sl, s)

    # ---- Blend -> Danger (0..100), nudged by the sim -------------------- :140-145
    long_base = W_TREND * long_trend + W_ORDERBOOK * long_ob + W_MOMENTUM * long_mom
    short_base = W_TREND * short_trend + W_ORDERBOOK * short_ob + W_MOMENTUM * short_mom
    long_danger = _blend_danger(long_base, sim.long_sl_first_prob() if sim else None)
    short_danger = _blend_danger(short_base, sim.short_sl_first_prob() if sim else None)

    # ---- Confidence ----------------------------------------------------- :147-156
    data_complete, missing = _data_completeness(inp)
    agreement = max(wick_bear, wick_bull)
    volume_conf = _clamp01(rel_vol / 1.8)
    zone_depth = _clamp01(len(zones) / 6.0)
    base_conf = (0.30 * data_complete + 0.30 * agreement
                 + 0.20 * volume_conf + 0.20 * zone_depth)
    if sim is not None and sim.matches >= SIM_MIN_MATCHES:
        base_conf = min(1.0, base_conf + 0.05)
    long_conf = _directional_confidence(base_conf, long_trend, long_ob, long_mom)
    short_conf = _directional_confidence(base_conf, short_trend, short_ob, short_mom)

    # ---- Structural bias, independent of Danger ------------------------- :158-171
    # With too little history to seed even the fastest EMA there is no structural
    # read: stay NEUTRAL rather than let an EMPTY ema set read as "price above all
    # EMAs", which would be a false bullish.
    ema_score = 0.0 if not all_emas else (0.5 - below_all) * 2.0
    mom_score = _clamp(recent_return_pct / MOVE_REF_PCT, -1.0, 1.0)
    bias_score = 0.7 * ema_score + 0.3 * mom_score
    if not all_emas:
        bias = NEUTRAL
    elif bias_score < -0.20:
        bias = BEARISH
    elif bias_score > 0.20:
        bias = BULLISH
    else:
        bias = NEUTRAL

    ctx = _ReasonCtx(
        spot=spot, ema_14=inp.ema_14, rsi=rsi,
        below_short=below_short, above_short=above_short,
        wick_bear=wick_bear, wick_bull=wick_bull,
        long_ob=long_ob, short_ob=short_ob,
        down_move=down_move, up_move=up_move,
        exhaustion_down=exhaustion_down, exhaustion_up=exhaustion_up,
        oi_expand=oi_expand, vol_amp=vol_amp, vol_danger=vol_danger,
    )

    return RiskSnapshot(
        long=RiskDirectionScore(
            direction=LONG, danger=long_danger, confidence=long_conf,
            trend_pts=round(W_TREND * long_trend * 100),
            orderbook_pts=round(W_ORDERBOOK * long_ob * 100),
            momentum_pts=round(W_MOMENTUM * long_mom * 100),
            reasons=_long_reasons(ctx, tp, sl),
        ),
        short=RiskDirectionScore(
            direction=SHORT, danger=short_danger, confidence=short_conf,
            trend_pts=round(W_TREND * short_trend * 100),
            orderbook_pts=round(W_ORDERBOOK * short_ob * 100),
            momentum_pts=round(W_MOMENTUM * short_mom * 100),
            reasons=_short_reasons(ctx, tp, sl),
        ),
        bias=bias,
        historical_sim=sim,
        data_confidence_note=_confidence_note(data_complete, agreement,
                                              int(inp.close.size)),
        tp_pct=tp, sl_pct=sl,
        orderbook_is_proxy=True,
        missing_inputs=missing,
    )


# ---------------------------------------------------------------------------
# Factor helpers
# ---------------------------------------------------------------------------


def _zone_danger(inp: RiskInputs, spot: float, tp: float, sl: float,
                 max_strength: float, for_long: bool) -> float:
    """`orderBookDanger` (:180-205) with zones in place of walls.

    For a LONG: upside blocked by RESISTANCE within +TP%, and no SUPPORT within
    -SL%. For a SHORT the roles mirror. `strength` replaces `notionalUsd`.
    """
    if spot <= 0.0:
        return 0.0
    if for_long:
        block_pool = [z for z in inp.resistance if z.high > spot]
        support_pool = [z for z in inp.support if z.low < spot]
    else:
        block_pool = [z for z in inp.support if z.low < spot]
        support_pool = [z for z in inp.resistance if z.high > spot]

    block = min(block_pool, key=lambda z: z.distance_from(spot), default=None)
    supp = min(support_pool, key=lambda z: z.distance_from(spot), default=None)

    blocked = 0.0
    if block is not None:
        d = abs(block.distance_from(spot)) / spot * 100.0
        if d <= tp:
            blocked = _clamp01(1.0 - d / tp) * min(1.0, block.strength / max_strength)
    supported = 0.0
    if supp is not None:
        d = abs(supp.distance_from(spot)) / spot * 100.0
        if d <= sl:
            supported = _clamp01(1.0 - d / sl) * min(1.0, supp.strength / max_strength)
    return _clamp01(0.6 * blocked + 0.4 * (1.0 - supported))


def _momentum_danger(chase: float, knife: float, oi: float | None,
                     vol_amp: float, vol_danger: float) -> float:
    """`momentumDanger` (:207-210), reweighted when OI is unavailable.

    The Kotlin's weights are knife .45 / chase .30 / oi .25 and sum to 1.0. With
    no OI feed the remaining two are renormalised to .60/.40 rather than letting
    the missing quarter read as "no danger" — a term you cannot measure must not
    become evidence of safety.
    """
    if oi is None:
        move = 0.60 * knife + 0.40 * chase
    else:
        move = 0.45 * knife + 0.30 * chase + 0.25 * oi
    return _clamp01(move * (1.0 + 0.4 * vol_amp) * 0.85 + vol_danger * 0.15)


def _blend_danger(base01: float, sl_first_prob: float | None) -> int:
    """:212-215"""
    v = base01 if sl_first_prob is None else (
        (1.0 - SIM_BLEND) * base01 + SIM_BLEND * sl_first_prob)
    return round(_clamp01(v) * 100)


def _directional_confidence(base: float, trend: float, ob: float, mom: float) -> int:
    """:217-222 — internally coherent factors read as a more confident answer."""
    spread = max(trend, ob, mom) - min(trend, ob, mom)
    coherence = 1.0 - spread
    return round(_clamp01(base * (0.85 + 0.15 * coherence)) * 100)


def _data_completeness(inp: RiskInputs) -> tuple[float, tuple[str, ...]]:
    """:224-233, plus the names of what is missing so the UI can be honest."""
    s = 0.0
    missing: list[str] = []
    if inp.oi_deviation_pct is not None:
        s += 0.25
    else:
        missing.append("open interest")
    if inp.long_ratio is not None and inp.short_ratio is not None:
        s += 0.20
    else:
        missing.append("long/short ratio")
    if inp.support and inp.resistance:
        s += 0.20
    else:
        missing.append("zones on both sides")
    n = int(inp.close.size)
    if n >= 60:
        s += 0.20
    elif n >= 30:
        s += 0.10
    else:
        missing.append("candle history")
    usable_tfs = [t for t in inp.tf_signals if t.usable]
    if len(usable_tfs) >= 3:
        s += 0.15
    else:
        missing.append("3+ usable timeframes")
    return _clamp01(s), tuple(missing)


# ---------------------------------------------------------------------------
# Historical double-barrier simulation (:239-306)
# ---------------------------------------------------------------------------


def simulate(inp: RiskInputs, tp: float, sl: float,
             s: RiskSettings) -> HistoricalSim | None:
    """Find past bars whose micro-conditions match now, and see what happened.

    Match = EMA28 distance within `SIM_EMA_TOL_PCT` AND the same RSI zone. Then
    walk forward `s.sim_horizon` bars and record which barrier was touched first.
    A bar that touches both is scored **SL first**, which is the conservative
    reading and the same tie-break `tools/validate_rule.py` uses.

    This is not a backtest. It has no entry rule, no position sizing, no fees and
    no sequencing — it is a conditional frequency over analogous bars, which is
    why it costs milliseconds and why it is blended at only 20%.
    """
    close = inp.close
    n = int(close.size)
    horizon = max(1, int(s.sim_horizon))
    if (inp.ema_28_series is None or inp.rsi_series is None
            or n < SIM_WARMUP + horizon + 2):
        return None
    ema28 = np.asarray(inp.ema_28_series, dtype="float64")
    rsi = np.asarray(inp.rsi_series, dtype="float64")
    if ema28.size != n or rsi.size != n:
        return None

    now = n - 1
    ema28_now = ema28[now]
    if not np.isfinite(ema28_now) or ema28_now <= 0:
        return None
    dist_now = (close[now] - ema28_now) / ema28_now * 100.0
    zone_now = _rsi_zone(rsi[now])

    start = max(SIM_WARMUP, n - int(s.lookback_bars))
    end = n - 1 - horizon
    if end <= start:
        return None

    # Vectorised candidate selection — the Kotlin loops because it runs on a
    # phone over a few hundred bars; here the search window can be 5,000+ and a
    # Python loop over all of them on a 5-second tick is not affordable.
    idx = np.arange(start, end + 1)
    e = ema28[idx]
    r = rsi[idx]
    ok = np.isfinite(e) & (e > 0) & np.isfinite(r)
    dist = np.full(idx.shape, np.nan)
    np.divide(close[idx] - e, e, out=dist, where=ok)
    dist *= 100.0
    ok &= np.abs(dist - dist_now) <= SIM_EMA_TOL_PCT
    ok &= _rsi_zone_arr(r) == zone_now
    cand = idx[ok]

    if cand.size == 0:
        return HistoricalSim(
            matches=0, long_tp_first=0, long_sl_first=0,
            short_tp_first=0, short_sl_first=0,
            lookback_label=s.lookback_label, horizon_label=s.horizon_label,
            summary=f"No matching micro-conditions in the last {s.lookback_label}.")

    high, low = inp.high, inp.low
    tp_up, sl_dn = tp / 100.0, sl / 100.0
    l_tp = l_sl = s_tp = s_sl = 0
    for i in cand.tolist():
        entry = float(close[i])
        long_tp, long_sl = entry * (1 + tp_up), entry * (1 - sl_dn)
        short_tp, short_sl = entry * (1 - tp_up), entry * (1 + sl_dn)
        j0, j1 = i + 1, min(i + horizon, n - 1)
        if j1 < j0:
            continue
        hi = high[j0:j1 + 1]
        lo = low[j0:j1 + 1]

        lt = _first_true(hi >= long_tp)
        ls = _first_true(lo <= long_sl)
        if ls is not None and (lt is None or ls <= lt):
            l_sl += 1                    # ties -> SL first (conservative)
        elif lt is not None:
            l_tp += 1

        st_ = _first_true(lo <= short_tp)
        ss = _first_true(hi >= short_sl)
        if ss is not None and (st_ is None or ss <= st_):
            s_sl += 1
        elif st_ is not None:
            s_tp += 1

    matches = int(cand.size)
    return HistoricalSim(
        matches=matches, long_tp_first=l_tp, long_sl_first=l_sl,
        short_tp_first=s_tp, short_sl_first=s_sl,
        lookback_label=s.lookback_label, horizon_label=s.horizon_label,
        summary=(f"Found {matches} matching micro-conditions in the last "
                 f"{s.lookback_label}. Long -> SL first {l_sl} / TP first {l_tp}; "
                 f"Short -> SL first {s_sl} / TP first {s_tp}."),
    )


def _first_true(mask: np.ndarray) -> int | None:
    hit = np.flatnonzero(mask)
    return int(hit[0]) if hit.size else None


def _rsi_zone(r: float) -> int:
    """:308-315"""
    if not np.isfinite(r):
        return -1
    if r < 30.0:
        return 0
    if r < 45.0:
        return 1
    if r < 55.0:
        return 2
    if r < 70.0:
        return 3
    return 4


def _rsi_zone_arr(r: np.ndarray) -> np.ndarray:
    out = np.full(r.shape, -1, dtype="int64")
    ok = np.isfinite(r)
    out[ok & (r >= 70.0)] = 4
    out[ok & (r < 70.0)] = 3
    out[ok & (r < 55.0)] = 2
    out[ok & (r < 45.0)] = 1
    out[ok & (r < 30.0)] = 0
    return out


# ---------------------------------------------------------------------------
# Reasons — the "Show Why" analyst breakdown (:321-375)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReasonCtx:
    spot: float
    ema_14: float | None
    rsi: float
    below_short: float
    above_short: float
    wick_bear: float
    wick_bull: float
    long_ob: float
    short_ob: float
    down_move: float
    up_move: float
    exhaustion_down: float
    exhaustion_up: float
    oi_expand: float | None
    vol_amp: float
    vol_danger: float


def _long_reasons(c: _ReasonCtx, tp: float, sl: float) -> tuple[RiskReason, ...]:
    out: list[RiskReason] = []
    def danger(f, t): out.append(RiskReason(LONG, DANGER, f, t))
    def support(f, t): out.append(RiskReason(LONG, SUPPORT, f, t))

    if c.below_short >= 0.66:
        danger(TREND_EMA, "Price trades below EMA7/14/28 — structurally bearish")
    if _ok(c.ema_14) and c.spot < c.ema_14 and c.wick_bear > 0.35:
        danger(TREND_EMA, "Price only bounced to EMA14 with upper-wick rejection")
    elif c.wick_bear > 0.4:
        danger(TREND_EMA, "Upper-wick rejections across multiple timeframes")
    if c.long_ob >= 0.5:
        danger(ORDER_BOOK, f"A resistance zone caps the upside within your +{_fmt(tp)}% target")
    if c.oi_expand is not None and c.oi_expand > 0.3 and c.down_move > 0.3:
        danger(MOMENTUM, "Open Interest increasing during the dump (aggressive selling)")
    if c.down_move > 0.5 and c.exhaustion_down < 0.5:
        danger(MOMENTUM, "Fresh downside momentum — catching a falling knife")
    if c.vol_danger > 0.6:
        danger(MOMENTUM, f"Volatility expanding — wide whipsaw risk against your {_fmt(sl)}% stop")

    if c.rsi < 25.0:
        support(MOMENTUM, "RSI is oversold — bounce risk favours a fade")
    if c.long_ob < 0.25:
        support(ORDER_BOOK, "Support sits within your stop band")
    if not out:
        out.append(RiskReason(LONG, SUPPORT, TREND_EMA,
                              "No dominant long-side danger vector right now"))
    return tuple(out)


def _short_reasons(c: _ReasonCtx, tp: float, sl: float) -> tuple[RiskReason, ...]:
    out: list[RiskReason] = []
    def danger(f, t): out.append(RiskReason(SHORT, DANGER, f, t))
    def support(f, t): out.append(RiskReason(SHORT, SUPPORT, f, t))

    if c.above_short >= 0.66:
        danger(TREND_EMA, "Price trades above EMA7/14/28 — chasing into strength")
    if c.wick_bull > 0.4:
        danger(TREND_EMA, "Lower-wick rejections — buyers are defending")
    if c.short_ob >= 0.5:
        danger(ORDER_BOOK, f"A support zone blocks the downside within your +{_fmt(tp)}% target")
    if c.down_move > 0.4 and c.exhaustion_down > 0.5:
        danger(MOMENTUM, "Selling into an exhausted, oversold dump (bounce risk)")
    if c.vol_amp > 0.5 and c.down_move > 0.4:
        danger(MOMENTUM, "Climax volume on the drop — late-move exhaustion")
    if c.vol_danger > 0.6:
        danger(MOMENTUM, f"Volatility expanding — wide whipsaw risk against your {_fmt(sl)}% stop")

    if c.rsi > 75.0:
        support(MOMENTUM, "RSI is overbought — momentum still favours continuation")
    if c.short_ob < 0.25:
        support(ORDER_BOOK, "Resistance sits within your stop band")
    if not out:
        out.append(RiskReason(SHORT, SUPPORT, TREND_EMA,
                              "No dominant short-side danger vector right now"))
    return tuple(out)


def _confidence_note(data_complete: float, agreement: float, n: int) -> str:
    """:377-382"""
    if n < 30:
        return "Low sample — thin candle history"
    if data_complete < 0.6:
        return "Partial data — no order book, no open interest on this feed"
    if agreement < 0.35:
        return "Choppy — timeframes disagree, read with caution"
    return "Multi-timeframe data complete and aligned"


# ---------------------------------------------------------------------------
# Wick bias — derived here, NOT ported. See the module docstring.
# ---------------------------------------------------------------------------


def wick_bias(open_: float, high: float, low: float, close: float) -> str:
    """Classify one candle's rejection tail.

    An **upper** wick that dominates is a BEARISH rejection: price went up and was
    sold back. A **lower** wick that dominates is BULLISH. A candle qualifies only
    when the wick is both `WICK_BODY_RATIO` x the body *and* `WICK_RANGE_FRAC` of
    the whole range — the body test alone fires on every doji, the range test
    alone on any long candle with a tail.
    """
    if not all(_ok(v) for v in (open_, high, low, close)):
        return WICK_NEUTRAL
    rng = high - low
    if rng <= 0:
        return WICK_NEUTRAL
    body = abs(close - open_)
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    # A zero body makes the ratio test degenerate; treat it as "any wick beats it"
    # by comparing against the range instead, which is what the fractions do.
    body_ok_up = upper >= WICK_BODY_RATIO * body if body > 0 else upper > 0
    body_ok_dn = lower >= WICK_BODY_RATIO * body if body > 0 else lower > 0
    if body_ok_up and upper / rng >= WICK_RANGE_FRAC and upper > lower:
        return BEARISH_REJECTION
    if body_ok_dn and lower / rng >= WICK_RANGE_FRAC and lower > upper:
        return BULLISH_REJECTION
    return WICK_NEUTRAL


def _weighted_wick(signals: Sequence[TfCandleSignal],
                   weights: dict[str, float], bias: str) -> float:
    """:392-401 — the multi-timeframe rejection vote, weighted by timeframe."""
    if not signals:
        return 0.0
    total = hit = 0.0
    for s in signals:
        w = weights.get(s.label, 1.0)
        total += w
        if s.wick_bias == bias:
            hit += w
    return hit / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# The gate — this is how the risk read is allowed to affect a trade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSettings:
    """Thresholds the risk read must clear before a fired rule signal is sent.

    Deliberately loose by default. The rule they gate has a measured +3.3-point
    edge; these weights have no measurement at all, so a tight gate would spend a
    known edge to buy an unknown one. `max_danger=70` vetoes only the clearly bad
    setups and leaves the ordinary ones alone.
    """

    enabled: bool = True
    max_danger: int = 70          # veto above this Danger%
    min_confidence: int = 25      # veto below this Confidence%
    require_bias: bool = False    # also require Bias to agree with the side
    veto_on_missing_data: bool = False


@dataclass(frozen=True)
class GateResult:
    passed: bool
    danger: int
    confidence: int
    bias: str
    vetoes: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"passed": self.passed, "danger": self.danger,
                "confidence": self.confidence, "bias": self.bias,
                "vetoes": list(self.vetoes)}


def gate(snap: RiskSnapshot, side: str,
         settings: GateSettings | None = None) -> GateResult:
    """Should a signal the RULE already fired actually be sent?

    This can only ever **subtract**. It never fires a trade on its own, because
    nothing in this file has been measured on gold — see the module docstring.
    """
    g = settings or GateSettings()
    score = snap.long if side.upper() in ("LONG", "BUY") else snap.short
    want = BULLISH if score.direction == LONG else BEARISH
    vetoes: list[str] = []
    if g.enabled:
        if score.danger > g.max_danger:
            vetoes.append(f"danger {score.danger} > {g.max_danger}")
        if score.confidence < g.min_confidence:
            vetoes.append(f"confidence {score.confidence} < {g.min_confidence}")
        if g.require_bias and snap.bias != want:
            vetoes.append(f"bias {snap.bias} != {want}")
        if g.veto_on_missing_data and snap.missing_inputs:
            vetoes.append("missing: " + ", ".join(snap.missing_inputs))
    return GateResult(passed=not vetoes, danger=score.danger,
                      confidence=score.confidence, bias=snap.bias,
                      vetoes=tuple(vetoes))


# ---------------------------------------------------------------------------
# Small utilities (:385-390, :404-412)
# ---------------------------------------------------------------------------


def _ok(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _clamp(x: float, lo: float, hi: float) -> float:
    if not math.isfinite(x):
        return lo
    return max(lo, min(hi, x))


def _clamp01(x: float) -> float:
    return _clamp(x, 0.0, 1.0)


def _frac(values: list[float], pred) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if pred(v)) / len(values)


def _last_finite(arr) -> float | None:
    if arr is None:
        return None
    a = np.asarray(arr, dtype="float64")
    fin = np.flatnonzero(np.isfinite(a))
    return float(a[fin[-1]]) if fin.size else None


def _recent_return_pct(close: np.ndarray, lookback: int) -> float:
    """:355-361"""
    n = int(np.asarray(close).size)
    if n < 2:
        return 0.0
    frm = float(close[max(0, n - 1 - lookback)])
    if frm <= 0:
        return 0.0
    return (float(close[n - 1]) - frm) / frm * 100.0


def _fmt(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:.1f}"


def _r(v, dp=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, dp)
