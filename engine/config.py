"""Strategy configuration — the YAML schema and its hashing.

The config is the unit of reproducibility: same DB + same config = same trades.
`config_hash` is computed over the *normalised* model, not the raw text, so
reformatting the YAML or reordering keys does not invent a new run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ..indicators.base import IndicatorConfig

TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def parse_time(value: Any) -> int | None:
    """ISO / date / 'now' -> epoch ms UTC. ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    text = str(value).strip()
    if text.lower() == "now":
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(
                datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000
            )
        except ValueError:
            continue
    raise ValueError(f"cannot parse time {value!r}")


class Condition(BaseModel):
    """One node of the entry-condition tree.

    Exactly one of ``signal`` / ``expr`` / ``all_of`` / ``any_of`` must be set,
    which lets a config nest arbitrarily:

        any_of:
          - signal: ut_cross_up
          - all_of:
              - signal: ut_bias_bullish
              - signal: ut_level_near
    """

    model_config = {"extra": "forbid"}

    signal: str | None = None
    expr: str | None = None
    all_of: list["Condition"] | None = None
    any_of: list["Condition"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Condition":
        set_fields = [
            f for f in ("signal", "expr", "all_of", "any_of")
            if getattr(self, f) is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                f"a condition needs exactly one of signal/expr/all_of/any_of, got {set_fields}"
            )
        return self


Condition.model_rebuild()


class SessionFilter(BaseModel):
    model_config = {"extra": "forbid"}

    # gold_session -> the app's own MarketHours (core/MarketHours.kt:20-33):
    #   Sunday 21:00 UTC open, Friday 22:00 UTC close, Saturday shut.
    # weekdays_myt -> naive Mon-Fri in UTC+8. Measured to keep 115 dead weekend
    #   bars and drop 144 prime-time ones; kept only for comparison.
    mode: Literal["gold_session", "weekdays_myt", "none"] = "gold_session"
    hours_myt: list[str] | None = None  # e.g. ["08:00-23:59"]


class EntryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    side: Literal["long", "short"] = "long"
    all_of: list[Condition] | None = None
    any_of: list[Condition] | None = None
    ut_near_atr: float = 0.6          # AlertEngine.kt:47 UT_NEAR_ATR
    zone_near_atr: float = 0.5        # AlertEngine.kt:44 ZONE_NEAR_ATR
    dynamic_near_atr: float = 0.5     # AlertEngine.kt:54 DYNAMIC_NEAR_ATR
    zone_touch_atr: float = 0.25      # AlertEngine.kt:57 ZONE_TOUCH_ATR
    cooldown_bars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _needs_a_condition(self) -> "EntryConfig":
        if not self.all_of and not self.any_of:
            raise ValueError("entry needs at least one of all_of / any_of")
        return self


class ExitLeg(BaseModel):
    model_config = {"extra": "forbid"}

    # usd_pnl     -> absolute P/L in quote currency at the configured qty
    # price_delta -> absolute price move
    # atr_mult    -> multiple of ATR-14 as of the ENTRY bar (frozen, not trailing)
    # rr          -> multiple of the stop distance (take_profit only)
    mode: Literal["usd_pnl", "price_delta", "atr_mult", "rr"]
    value: float


class TrailingConfig(BaseModel):
    """How a position is ridden rather than capped.

    ``ltf_ema_break`` is the "let a trend run" exit: stay in until a bar on a
    LOWER timeframe closes through a moving average. Unlike a fixed take-profit
    it has no ceiling, which is the point — a one-way move is captured whole.

    Ordering inside one strategy bar is deliberate and pessimistic: the stop is
    checked against each sub-bar's high/low FIRST, because a stop is an intrabar
    event that would have filled before the sub-bar closed, whereas an EMA break
    is only knowable at that close.
    """

    model_config = {"extra": "forbid"}

    mode: Literal["atr_mult", "ut_level", "ltf_ema_break"]
    value: float = 2.0
    # ltf_ema_break only:
    timeframe: str | None = None      # e.g. "5m"
    ema: str = "ema_14"               # column on that timeframe's indicator frame
    closes: int = Field(default=1, ge=1)   # consecutive closes required
    # A pullback entry starts on the wrong side of a fast EMA, so an unarmed
    # trail would fire immediately. True = wait for the LTF to close back on
    # the favourable side before the trail goes live.
    arm_on_recross: bool = True

    @model_validator(mode="after")
    def _ltf_needs_a_timeframe(self) -> "TrailingConfig":
        if self.mode == "ltf_ema_break":
            if self.timeframe is None:
                raise ValueError("ltf_ema_break trailing needs a `timeframe`")
            if self.timeframe not in TIMEFRAMES:
                raise ValueError(f"unknown trailing timeframe {self.timeframe!r}")
        return self


class ExitConfig(BaseModel):
    model_config = {"extra": "forbid"}

    take_profit: ExitLeg | None = None
    stop_loss: ExitLeg | None = None
    time_stop_bars: int | None = None
    trailing: TrailingConfig | None = None


class SizingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["fixed_qty", "risk_per_trade"] = "fixed_qty"
    qty: float = 1.0
    risk_usd: float | None = None


class CostsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    taker_fee_bps: float = 5.5      # 0.055%
    slippage_ticks: int = 1
    tick_size: float = 0.01         # confirmed from instruments-info
    apply_funding: bool = True


class ExecutionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    entry_fill: Literal["next_bar_open", "same_bar_close"] = "next_bar_open"
    intrabar_resolution: str | None = "1m"
    # When 1m is unavailable for an ambiguous bar, assume the STOP was hit first.
    # Pessimistic by construction — the alternative flatters the strategy.
    ambiguous_policy: Literal["stop_first", "target_first"] = "stop_first"
    max_concurrent_positions: int = 1


class FeaturesConfig(BaseModel):
    model_config = {"extra": "forbid"}

    timeframes: list[str] = Field(default_factory=lambda: ["5m", "15m", "30m", "1h", "4h"])
    include: list[str] = Field(default_factory=list)
    derived: list[str] = Field(default_factory=list)

    @field_validator("timeframes")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        bad = [tf for tf in v if tf not in TIMEFRAMES]
        if bad:
            raise ValueError(f"unknown timeframe(s) {bad}")
        return v


class Period(BaseModel):
    model_config = {"extra": "forbid"}

    start: Any = None
    end: Any = None

    @property
    def start_ms(self) -> int | None:
        return parse_time(self.start)

    @property
    def end_ms(self) -> int | None:
        return parse_time(self.end)


class StrategyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    symbol: str = "XAUUSDT"
    timeframe: str = "1h"
    period: Period = Field(default_factory=Period)
    session_filter: SessionFilter = Field(default_factory=SessionFilter)
    entry: EntryConfig
    exit: ExitConfig = Field(default_factory=ExitConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    indicators: IndicatorConfig = Field(default_factory=IndicatorConfig)

    @field_validator("timeframe")
    @classmethod
    def _known_tf(cls, v: str) -> str:
        if v not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {v!r}")
        return v

    # -- provenance --------------------------------------------------------

    def canonical(self) -> str:
        """Stable JSON of the resolved config, for hashing.

        ``period.end: now`` is excluded because it is wall-clock dependent — two
        runs of the same strategy an hour apart are the same *config*, and the
        actual range used is recorded separately on the run row.
        """
        data = self.model_dump(mode="json", exclude={"period"})
        data["period"] = {
            "start": str(self.period.start),
            "end": str(self.period.end),
        }
        if str(self.period.end).strip().lower() == "now":
            data["period"]["end"] = "now"
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]

    # -- loading -----------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> "StrategyConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    @classmethod
    def from_text(cls, text: str) -> "StrategyConfig":
        return cls.model_validate(yaml.safe_load(text))

    def required_timeframes(self) -> list[str]:
        """Every timeframe that must be loaded for this run."""
        needed = {self.timeframe, *self.features.timeframes}
        if self.execution.intrabar_resolution:
            needed.add(self.execution.intrabar_resolution)
        if self.exit.trailing and self.exit.trailing.timeframe:
            needed.add(self.exit.trailing.timeframe)
        return sorted(needed, key=TIMEFRAMES.index)
