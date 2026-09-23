"""Event-driven, bar-by-bar backtester.

Determinism contract: same DB + same config = byte-identical trades. No wall
clock inside the loop, no RNG, no set iteration order affecting output. The one
wall-clock read is `created_at` on the run row, which is metadata and excluded
from `config_hash`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.db import INTERVAL_MS, CandleRepository, Database, MetaRepository
from ..indicators.registry import compute_indicators
from . import features as F
from . import fills
from .config import StrategyConfig
from .signals import SignalContext, evaluate_entry

log = logging.getLogger(__name__)

CODE_VERSION = "phase3.0"


@dataclass(slots=True)
class Trade:
    trade_id: str
    run_id: str
    symbol: str
    side: str
    signal_id: str
    entry_time: int
    entry_price: float
    qty: float
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    net_pnl: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0
    bars_held: int = 0
    ambiguous: int = 0
    entry_index: int = -1


@dataclass(slots=True)
class RunResult:
    run_id: str
    config: StrategyConfig
    trades: list[Trade] = field(default_factory=list)
    feature_rows: list[tuple] = field(default_factory=list)
    start_ms: int = 0
    end_ms: int = 0
    bars_evaluated: int = 0
    signals_fired: int = 0
    skipped_cooldown: int = 0
    skipped_in_position: int = 0
    skipped_session: int = 0
    data_fingerprint: str = ""
    gaps: dict[str, int] = field(default_factory=dict)


class Backtester:
    def __init__(self, db: Database, config: StrategyConfig) -> None:
        self.db = db
        self.cfg = config
        self.candles = CandleRepository(db)
        self.meta = MetaRepository(db)

    # -- data ---------------------------------------------------------------

    def _load(self) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
        """Load candles and compute indicators for every required timeframe.

        Indicators are computed over the FULL stored history, then the run window
        is applied. That is what makes the warm-up burn-in meaningful: a run
        starting on 2026-06-01 still has months of history behind its first bar.
        """
        raw: dict[str, pd.DataFrame] = {}
        ind: dict[str, pd.DataFrame] = {}
        for tf in self.cfg.required_timeframes():
            df = self.candles.load(self.cfg.symbol, tf, end_ms=self.cfg.period.end_ms)
            if df.empty:
                raise SystemExit(
                    f"no candles for {self.cfg.symbol} {tf} — run `cli sync` first"
                )
            raw[tf] = df
            # 1m is only used to resolve ambiguous bars; it needs no indicators.
            if tf != self.cfg.execution.intrabar_resolution or tf == self.cfg.timeframe:
                ind[tf] = compute_indicators(df, tf, self.cfg.indicators)
        return raw, ind

    # -- run ----------------------------------------------------------------

    def run(self, run_id: str | None = None, allow_gaps: bool = False) -> RunResult:
        cfg = self.cfg
        run_id = run_id or uuid.uuid4().hex[:16]
        raw, ind = self._load()

        anchor_raw = raw[cfg.timeframe]
        anchor = ind[cfg.timeframe]

        # --- gap check ----------------------------------------------------
        gaps: dict[str, int] = {}
        for tf in cfg.required_timeframes():
            found = self.candles.find_gaps(
                cfg.symbol, tf, cfg.period.start_ms, cfg.period.end_ms
            )
            gaps[tf] = sum(g.missing_bars for g in found)
        total_missing = sum(gaps.values())
        if total_missing and not allow_gaps:
            raise SystemExit(
                f"refusing to run over unfilled gaps: {gaps}. "
                "Re-sync, or pass --allow-gaps to accept them."
            )

        open_time = anchor_raw["open_time"].to_numpy(dtype="int64")
        o = anchor_raw["open"].to_numpy()
        h = anchor_raw["high"].to_numpy()
        low = anchor_raw["low"].to_numpy()
        c = anchor_raw["close"].to_numpy()
        n = len(anchor_raw)

        # --- higher-timeframe alignment (as-of CLOSED bars) ---------------
        htf_frames = {
            tf: (raw[tf]["open_time"].to_numpy(dtype="int64"), ind[tf])
            for tf in cfg.features.timeframes
            if tf in ind
        }
        htf_indices = F.build_htf_alignment(open_time, cfg.timeframe, htf_frames)

        # HTF signals read the same as-of-closed frames, unprefixed.
        htf_for_signals = {
            tf: F.aligned_frame(frame, htf_indices[tf])
            for tf, (_ot, frame) in htf_frames.items()
            if tf != cfg.timeframe
        }

        # --- signals ------------------------------------------------------
        ctx = SignalContext(
            ind=anchor,
            close=c,
            high=h,
            low=low,
            open_time=open_time,
            thresholds={
                "ut_near_atr": cfg.entry.ut_near_atr,
                "zone_near_atr": cfg.entry.zone_near_atr,
                "dynamic_near_atr": cfg.entry.dynamic_near_atr,
                "zone_touch_atr": cfg.entry.zone_touch_atr,
            },
            htf=htf_for_signals,
        )
        fired, branches = evaluate_entry(cfg.entry, ctx)

        # --- eligibility masks --------------------------------------------
        warmup = cfg.indicators.warmup_bars
        eligible = np.zeros(n, dtype=bool)
        eligible[warmup:] = True
        eligible &= anchor["usable"].to_numpy().astype(bool)

        if cfg.period.start_ms is not None:
            eligible &= open_time >= cfg.period.start_ms
        if cfg.period.end_ms is not None:
            eligible &= open_time <= cfg.period.end_ms

        mode = cfg.session_filter.mode
        if mode == "gold_session":
            session_ok = np.array([F.is_gold_session(int(t)) for t in open_time])
        elif mode == "weekdays_myt":
            session_ok = np.array([F.is_weekday_myt(int(t)) for t in open_time])
        else:
            session_ok = np.ones(n, dtype=bool)

        # --- 1m index for ambiguity resolution -----------------------------
        minute_df = raw.get(cfg.execution.intrabar_resolution or "", None)
        minute_ot = minute_high = minute_low = None
        if minute_df is not None and not minute_df.empty:
            minute_ot = minute_df["open_time"].to_numpy(dtype="int64")
            minute_high = minute_df["high"].to_numpy()
            minute_low = minute_df["low"].to_numpy()

        # --- lower-timeframe trail ("ride it until 5m breaks the EMA") -----
        trail = cfg.exit.trailing
        ltf = None
        if trail is not None and trail.mode != "ltf_ema_break":
            # The schema accepts `atr_mult` and `ut_level` but the engine does not
            # implement them yet. Silently ignoring a configured exit would let a
            # run report results for a strategy nobody actually tested.
            raise SystemExit(
                f"exit.trailing.mode = {trail.mode!r} is declared in the config schema "
                "but not implemented in the engine. Only 'ltf_ema_break' works today. "
                "Remove the trailing block or use ltf_ema_break."
            )
        if trail is not None and trail.mode == "ltf_ema_break":
            ltf_raw = raw[trail.timeframe]
            ltf_ind = ind[trail.timeframe]
            if trail.ema not in ltf_ind.columns:
                raise SystemExit(
                    f"trailing ema {trail.ema!r} is not a column on the "
                    f"{trail.timeframe} frame"
                )
            ltf = {
                "ot": ltf_raw["open_time"].to_numpy(dtype="int64"),
                "high": ltf_raw["high"].to_numpy(),
                "low": ltf_raw["low"].to_numpy(),
                "close": ltf_raw["close"].to_numpy(),
                "ema": ltf_ind[trail.ema].to_numpy(),
                "needed": trail.closes,
                "arm_on_recross": trail.arm_on_recross,
            }

        result = RunResult(
            run_id=run_id,
            config=cfg,
            start_ms=int(open_time[eligible][0]) if eligible.any() else 0,
            end_ms=int(open_time[eligible][-1]) if eligible.any() else 0,
            gaps=gaps,
        )
        result.bars_evaluated = int(eligible.sum())
        result.data_fingerprint = self.candles.fingerprint(
            cfg.symbol, cfg.timeframe, result.start_ms, result.end_ms
        )

        atr = anchor["atr_14"].to_numpy()
        step = INTERVAL_MS[cfg.timeframe]
        qty = cfg.sizing.qty
        costs = cfg.costs
        side = cfg.entry.side

        funding = None
        if costs.apply_funding:
            funding = self.meta.load_funding(
                cfg.symbol, int(open_time[0]), int(open_time[-1]) + step
            )

        # --- the loop ------------------------------------------------------
        position: Trade | None = None
        pos_target = pos_stop = None
        pos_bars = 0
        pos_breaks = 0
        pos_armed = True
        pos_highs: list[float] = []
        pos_lows: list[float] = []
        last_entry_index = -10**9
        pending_entry: tuple[int, str] | None = None

        for t in range(n):
            # ---- manage an open position on THIS bar ----------------------
            if position is not None:
                pos_bars += 1
                pos_highs.append(h[t])
                pos_lows.append(low[t])

                mh = ml = None
                if minute_ot is not None:
                    lo_i = int(np.searchsorted(minute_ot, open_time[t], side="left"))
                    hi_i = int(np.searchsorted(minute_ot, open_time[t] + step, side="left"))
                    if hi_i > lo_i:
                        mh = minute_high[lo_i:hi_i]
                        ml = minute_low[lo_i:hi_i]

                if ltf is not None:
                    a = int(np.searchsorted(ltf["ot"], open_time[t], side="left"))
                    b = int(np.searchsorted(ltf["ot"], open_time[t] + step, side="left"))
                    minute_slices = None
                    if minute_ot is not None and b > a:
                        minute_slices = []
                        for k in range(a, b):
                            sub_start = int(ltf["ot"][k])
                            sub_end = sub_start + INTERVAL_MS[trail.timeframe]
                            m0 = int(np.searchsorted(minute_ot, sub_start, side="left"))
                            m1 = int(np.searchsorted(minute_ot, sub_end, side="left"))
                            minute_slices.append(
                                (minute_high[m0:m1], minute_low[m0:m1])
                            )
                    filled, pos_breaks, pos_armed = fills.resolve_bar_with_ltf(
                        side, pos_target, pos_stop,
                        ltf["high"][a:b], ltf["low"][a:b],
                        ltf["close"][a:b], ltf["ema"][a:b],
                        ltf["needed"], pos_breaks, pos_armed,
                        minute_slices, cfg.execution.ambiguous_policy,
                    )
                else:
                    filled = fills.resolve_bar(
                        side, o[t], h[t], low[t], pos_target, pos_stop,
                        mh, ml, cfg.execution.ambiguous_policy,
                    )
                time_stop = (
                    cfg.exit.time_stop_bars is not None
                    and pos_bars >= cfg.exit.time_stop_bars
                )

                if filled is not None:
                    self._close(position, filled.price, int(open_time[t]) + step,
                                filled.reason, filled.ambiguous, pos_bars,
                                pos_highs, pos_lows, funding, costs, qty, side)
                    result.trades.append(position)
                    position = None
                elif time_stop:
                    exit_price = fills.apply_slippage(
                        c[t], side, entering=False,
                        ticks=costs.slippage_ticks, tick_size=costs.tick_size,
                    )
                    self._close(position, exit_price, int(open_time[t]) + step,
                                "time_stop", False, pos_bars, pos_highs, pos_lows,
                                funding, costs, qty, side)
                    result.trades.append(position)
                    position = None

            # ---- open a pending entry at THIS bar's open ------------------
            if position is None and pending_entry is not None:
                src_index, label = pending_entry
                pending_entry = None
                entry_price = fills.apply_slippage(
                    o[t], side, entering=True,
                    ticks=costs.slippage_ticks, tick_size=costs.tick_size,
                )
                trade = Trade(
                    trade_id=uuid.uuid4().hex[:16],
                    run_id=run_id,
                    symbol=cfg.symbol,
                    side=side,
                    signal_id=label,
                    entry_time=int(open_time[t]),
                    entry_price=entry_price,
                    qty=qty,
                    entry_index=src_index,
                )
                trade.fees += fills.fee_for(entry_price * qty, costs.taker_fee_bps)
                pos_target, pos_stop = fills.exit_levels(
                    side, entry_price, qty, float(atr[src_index]),
                    cfg.exit.take_profit, cfg.exit.stop_loss,
                )
                position = trade
                pos_bars = 0
                pos_breaks = 0
                pos_armed = not (ltf is not None and ltf["arm_on_recross"])
                pos_highs, pos_lows = [], []

                result.feature_rows.extend(
                    self._features(trade, src_index, anchor, htf_frames,
                                   htf_indices, c, atr, open_time, label,
                                   last_entry_index)
                )
                last_entry_index = src_index

            # ---- evaluate a signal on THIS closed bar --------------------
            if not (eligible[t] and fired[t]):
                continue
            result.signals_fired += 1
            if not session_ok[t]:
                result.skipped_session += 1
                continue
            if position is not None or pending_entry is not None:
                result.skipped_in_position += 1
                continue
            if t - last_entry_index < cfg.entry.cooldown_bars:
                result.skipped_cooldown += 1
                continue
            if t + 1 >= n and cfg.execution.entry_fill == "next_bar_open":
                continue  # no next bar to fill on

            label = next(
                (name for name, mask in branches.items() if mask[t]), "entry"
            )
            if cfg.execution.entry_fill == "same_bar_close":
                pending_entry = None
                entry_price = fills.apply_slippage(
                    c[t], side, entering=True,
                    ticks=costs.slippage_ticks, tick_size=costs.tick_size,
                )
                trade = Trade(
                    trade_id=uuid.uuid4().hex[:16], run_id=run_id,
                    symbol=cfg.symbol, side=side, signal_id=label,
                    entry_time=int(open_time[t]), entry_price=entry_price,
                    qty=qty, entry_index=t,
                )
                trade.fees += fills.fee_for(entry_price * qty, costs.taker_fee_bps)
                pos_target, pos_stop = fills.exit_levels(
                    side, entry_price, qty, float(atr[t]),
                    cfg.exit.take_profit, cfg.exit.stop_loss,
                )
                position = trade
                pos_bars = 0
                pos_breaks = 0
                pos_armed = not (ltf is not None and ltf["arm_on_recross"])
                pos_highs, pos_lows = [], []
                result.feature_rows.extend(
                    self._features(trade, t, anchor, htf_frames, htf_indices,
                                   c, atr, open_time, label, last_entry_index)
                )
                last_entry_index = t
            else:
                pending_entry = (t, label)

        # ---- still open at the end of the data ----------------------------
        if position is not None:
            exit_price = fills.apply_slippage(
                c[-1], side, entering=False,
                ticks=costs.slippage_ticks, tick_size=costs.tick_size,
            )
            self._close(position, exit_price, int(open_time[-1]) + step,
                        "end_of_data", False, pos_bars, pos_highs, pos_lows,
                        funding, costs, qty, side)
            result.trades.append(position)

        return result

    # -- helpers ------------------------------------------------------------

    def _close(self, trade: Trade, exit_price: float, exit_time: int, reason: str,
               ambiguous: bool, bars_held: int, highs: list[float], lows: list[float],
               funding: pd.DataFrame | None, costs, qty: float, side: str) -> None:
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = reason
        trade.ambiguous = int(ambiguous)
        trade.bars_held = bars_held

        direction = 1.0 if side == "long" else -1.0
        trade.gross_pnl = (exit_price - trade.entry_price) * qty * direction
        trade.fees += fills.fee_for(exit_price * qty, costs.taker_fee_bps)

        if funding is not None and not funding.empty:
            # A perpetual charges funding every 8h (Bybit publishes the schedule);
            # a long pays when the rate is positive.
            window = funding[
                (funding["funding_time"] > trade.entry_time)
                & (funding["funding_time"] <= exit_time)
            ]
            if not window.empty:
                notional = trade.entry_price * qty
                trade.funding = float(window["rate"].sum()) * notional * direction * -1.0

        trade.net_pnl = trade.gross_pnl - trade.fees + trade.funding
        trade.mae, trade.mfe = fills.excursions(
            side, trade.entry_price, qty, np.asarray(highs), np.asarray(lows)
        )

    def _features(self, trade: Trade, index: int, anchor: pd.DataFrame,
                  htf_frames, htf_indices, close, atr, open_time,
                  label: str, last_entry_index: int) -> list[tuple]:
        cfg = self.cfg
        extras: dict[str, float | str | None] = {
            "entry_bar_close": float(close[index]),
            "entry_atr_14": float(atr[index]),
            "signal_branch": label,
            "bars_since_last_signal": (
                float(index - last_entry_index) if last_entry_index > -10**8 else None
            ),
        }
        if "funding_rate" in cfg.features.derived:
            row = self.meta.load_funding(
                cfg.symbol, int(open_time[index]) - 8 * 3_600_000, int(open_time[index])
            )
            if not row.empty:
                extras["funding_rate"] = float(row["rate"].iloc[-1])

        return F.snapshot(
            trade_id=trade.trade_id,
            entry_index=index,
            entry_open_time=int(open_time[index]),
            anchor_interval=cfg.timeframe,
            anchor=anchor,
            htf_frames=htf_frames,
            htf_indices=htf_indices,
            include=cfg.features.include,
            derived=cfg.features.derived,
            extras=extras,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist(db: Database, result: RunResult, config_yaml: str) -> None:
    """Write a run and its trades. Never overwrites — every run is a new run_id."""
    cfg = result.config
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO runs (run_id, created_at, config_yaml, config_hash, symbol, "
            "timeframe, start_ms, end_ms, code_version, data_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                result.run_id,
                int(time.time() * 1000),
                config_yaml,
                cfg.config_hash(),
                cfg.symbol,
                cfg.timeframe,
                result.start_ms,
                result.end_ms,
                CODE_VERSION,
                result.data_fingerprint,
            ),
        )
        cur.executemany(
            "INSERT INTO trades (trade_id, run_id, symbol, side, signal_id, entry_time, "
            "entry_price, qty, exit_time, exit_price, exit_reason, gross_pnl, fees, "
            "funding, net_pnl, mae, mfe, bars_held, ambiguous) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    t.trade_id, t.run_id, t.symbol, t.side, t.signal_id, t.entry_time,
                    t.entry_price, t.qty, t.exit_time, t.exit_price, t.exit_reason,
                    t.gross_pnl, t.fees, t.funding, t.net_pnl, t.mae, t.mfe,
                    t.bars_held, t.ambiguous,
                )
                for t in result.trades
            ],
        )
        if result.feature_rows:
            cur.executemany(
                "INSERT OR REPLACE INTO trade_features (trade_id, feature, value_num, value_txt) "
                "VALUES (?,?,?,?)",
                result.feature_rows,
            )
