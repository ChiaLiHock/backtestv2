"""The decision function. One line of JSON per closed anchor bar, fired or not.

`AUTOMATION.md` §B / §D1. This is the component that decides; the executor only
carries out what this writes, and nothing else in the system is allowed to form
an opinion about an entry.

**Every evaluation is logged, not just the firing ones.** "Why did nothing open
today" is the same question as "why did that open", and answering it after the
fact needs the leg values as they were, not a reconstruction. A file of only the
firing bars cannot distinguish "the rule never triggered" from "the process was
not running".

**The config is `OPERATING_PLAN.md` §8, not the yaml.** Long only, TP 5.0 / SL 2.5
ATR fixed at entry, a 96-bar time stop, leg 5 off, `gold_session` with no
hour-of-day window, and a fixed 0.01 lot. Three of those are easy to omit by
accident and are therefore asserted at construction rather than left implicit:

* the **96-bar time stop** — 11% of measured trades (65 of 588) exit that way, so
  an executor without it is running a different strategy than the measured one;
* **no partial and no trail** — a partial at 2.5 ATR amputates exactly the winner
  that makes TP 5.0 work, and `ENTRY_RULES.md` §5 option B was never measured on
  this panel;
* **leg 5 explicitly off** — not merely absent because the 5m history is short.

**Closed bars only.** The forming bar never produces a signal. A leg read off an
unfinished bar can un-fire before that bar closes, which is what the app's own
CONFIRMED/LIVE split exists to prevent.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..data.db import INTERVAL_MS, CandleRepository, Database
from ..indicators.base import IndicatorConfig
from ..tools.validate_rule import LEGS_LONG, LEGS_SHORT, build_context
from .signals import evaluate_signal

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class RuleConfig:
    """The measured system. Changing a field here changes what was validated."""

    # Bybit gold, because that is the chart being watched and the series the
    # broker fills are drawn against. XM's spot `GOLD` sits ~3.38 under this
    # perp (README, measured over 348 fills), so a level quoted here is that much
    # above the one your terminal will show — the offset is constant and small
    # relative to a 25-point bracket, but it is not zero.
    symbol: str = "XAUUSDT"
    broker_symbol: str = "GOLD"   # what you would actually send the order to
    anchor: str = "15m"

    side: str = "long"            # OPERATING_PLAN §8: long only

    # --- the bracket ------------------------------------------------------
    # "dollars" is a FIXED distance in price units, the same on every trade:
    # +25 / -25 on gold. At 0.01 lot (= 1 oz) that is $25 of P/L either way, and
    # it is what the owner asked for.
    #
    # It is NOT the measured system. `OPERATING_PLAN.md` §8 chose TP 5.0 / SL 2.5
    # x ATR14 on survival grounds, and §8.2 measured a symmetric bracket as the
    # cell that took a $200 account to -$130 on the real 2022-23 sequence. A
    # symmetric bracket also scales with nothing: 25 points is ~1.8 ATR when ATR
    # is 13.5 and ~3.7 ATR when ATR is 6.7, so the same trade is a different bet
    # in a quiet week than a violent one.
    #
    # Set `bracket_mode="atr"` to go back to the measured one.
    bracket_mode: str = "dollars"   # "dollars" | "atr"
    tp_usd: float = 25.0
    sl_usd: float = 25.0
    tp_atr: float = 5.0             # used only when bracket_mode == "atr"
    sl_atr: float = 2.5
    time_stop_bars: int = 96      # 24 h; part of the measured system (§8.4.1)

    use_5m_leg: bool = False      # §8.4.3 — explicitly off, not merely absent
    dip_lookback: int = 3
    session: str = "gold_session"
    hour_window: bool = False     # §8: windows now measure WORSE than all day

    lots: float = 0.01            # fixed, every trade (§8; §3's sizing is void)
    # Whether the owner's OWN manual positions consume the bot's slots.
    #
    # False, and that is the honest default. `max_concurrent` is a statement
    # about how many positions THIS RULE may hold at once — it is the thing
    # `OPERATING_PLAN.md` §10.2 measured, by simulating the rule's own queue.
    # The owner also trades this instrument by hand, several positions at a time.
    # Counting those against the rule's cap does not model the measured system;
    # it just silences the rule for as long as a human is trading, which is
    # exactly when a second opinion is worth having.
    #
    # Set True only when the executor is live and sharing one account, where the
    # real constraint is margin and total exposure rather than the rule's queue.
    manual_positions_block: bool = False
    # OPERATING_PLAN §10.5: two simultaneous positions in the SAME symbol and
    # direction. Not diversification — the correlation shows up as longer losing
    # runs (worst streak 9 -> 15). Each has its own SL/TP/time stop and is
    # tracked by ticket; they are never netted.
    max_concurrent: int = 2
    account_usd: float = 350.0
    cost_bps: float = 0.85

    # Deliberately absent, and asserted absent: partial take-profit, chandelier
    # trail, any stop movement. See §8.4.2.
    partial_tp: bool = False
    trail: bool = False

    # Guards, in dollars, from OPERATING_PLAN §10.3 / §10.5. Each was measured
    # against the real sequence at THIS bracket and THIS slot count rather than
    # carried over from the planning numbers:
    #   consecutive 5  fires 12.3x/yr at two slots -> nuisance, gets switched off
    #   consecutive 12 fires once per 2.1 yr       -> a "something is broken" flag
    #   daily -$60     fires 7.8x/yr;  -$150 once per 2.1 yr; worst day -$155
    #   floor $80      real 2022-23 low is $115 from $350, so $35 of headroom;
    #                  $120 would have stopped right before the 2024-25 recovery
    consecutive_loss_halt: int = 12
    daily_loss_limit_usd: float = 150.0
    equity_floor_usd: float = 80.0
    max_spread_points: int = 8
    # Break-even here is 34.2% and the expected rate 42.5%; the old "< 52%" was
    # written for TP2.5/SL2.5 and would fire permanently.
    review_win_pct: float = 36.0
    review_window_trades: int = 50

    def bracket(self, entry: float, atr: float) -> tuple[float, float]:
        """``(sl, tp)`` for an entry at ``entry`` with signal-bar ATR ``atr``.

        One place computes the bracket, so the live evaluation, the occupancy
        simulation in `backfill` and the risk read cannot drift apart — they did
        not, but three copies of the same arithmetic is how they would.
        """
        if self.bracket_mode == "dollars":
            up, dn = self.tp_usd, self.sl_usd
        else:
            up, dn = self.tp_atr * atr, self.sl_atr * atr
        if self.side == "long":
            return entry - dn, entry + up
        return entry + dn, entry - up

    def bracket_label(self) -> str:
        if self.bracket_mode == "dollars":
            return (f"TP +{self.tp_usd:g} / SL -{self.sl_usd:g} in price, fixed, "
                    f"time stop {self.time_stop_bars} bars")
        return (f"TP {self.tp_atr:g} / SL {self.sl_atr:g} x ATR14, "
                f"time stop {self.time_stop_bars} bars")

    def is_measured_bracket(self) -> bool:
        """Whether the live bracket is the one `OPERATING_PLAN.md` §8 measured.

        Surfaced on the page rather than asserted, because running an unmeasured
        bracket is a decision the owner is allowed to make — running one without
        knowing it is not.
        """
        return (self.bracket_mode == "atr"
                and (self.tp_atr, self.sl_atr) == (5.0, 2.5))

    def assert_matches_measured_system(self) -> None:
        """The three omissions that would silently change the strategy.

        Deliberate, stated changes (the bracket) are allowed through — this
        guard exists for the ones that are easy to make by ACCIDENT and that
        leave every number looking normal afterwards.
        """
        problems = []
        if self.time_stop_bars != 96:
            problems.append(
                f"time_stop_bars={self.time_stop_bars}, measured system used 96 "
                "(11% of trades exit that way)")
        if self.partial_tp or self.trail:
            problems.append(
                "partial_tp/trail enabled — never measured on this panel, and a "
                "partial at 2.5 ATR removes the winner that makes TP 5.0 work")
        if self.use_5m_leg:
            problems.append(
                "use_5m_leg=True — every §8 number was measured with it off, and "
                "it caps the panel at 5m depth")
        if self.max_concurrent < 1:
            problems.append("max_concurrent must be >= 1")
        if self.consecutive_loss_halt < 8:
            problems.append(
                f"consecutive_loss_halt={self.consecutive_loss_halt} fires on "
                "ordinary variance at this win rate (5 fires 12.3x/yr at two "
                "slots); §10.3 sets 12")
        if self.review_win_pct > 45:
            problems.append(
                f"review_win_pct={self.review_win_pct} is above the expected "
                "42.5% and would fire permanently; §10.5 sets 36")
        if problems:
            raise SystemExit("live config does not match the measured system:\n  - "
                             + "\n  - ".join(problems))


@dataclass
class Evaluation:
    """One closed anchor bar, whether or not anything fired."""

    schema: int
    ts_utc: str
    bar_open_ms: int
    bar_close_ms: int
    symbol: str
    broker_symbol: str
    anchor: str
    side: str
    fired: bool
    # `fired` = every leg true, in session, warm. `would_enter` additionally
    # requires that no position is open — max_concurrent is 1, and the measured
    # system is non-overlapping sequential. The two rates differ by 5.7x
    # (15.1/week vs 2.66/week on the panel), so conflating them would make the
    # live firing rate look 5.7x too high and break the reconciliation in §D3.
    would_enter: bool
    position_since_ms: int | None
    legs: dict[str, bool]
    blocking: list[str]
    close: float | None
    atr_entry: float | None
    entry_ref: float | None
    sl: float | None
    tp: float | None
    time_stop_ms: int | None
    lots: float
    in_session: bool
    spread_at_eval: float | None
    reason_if_skipped: str | None
    order_ticket: int | None = None
    config_hash: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _round(v, dp=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, dp)


class LiveSignal:
    """Evaluates the rule on the newest CLOSED anchor bar and logs the result."""

    # Bars of occupancy simulation to run before the requested window. 20x the
    # 96-bar time stop is ~20 days of margin, far beyond any cascade.
    SIM_RUNUP_BARS = 20

    def __init__(
        self,
        cfg: RuleConfig | None = None,
        log_path: Path | None = None,
        indicators: IndicatorConfig | None = None,
        tail_bars: int | None = None,
    ) -> None:
        self.cfg = cfg or RuleConfig()
        self.cfg.assert_matches_measured_system()
        self.ind_cfg = indicators or IndicatorConfig()
        # None = all of history, which is what `backfill` and every reconciliation
        # needs. The LIVE caller passes a tail because it reads only the newest
        # bar and a full MT5 build is ~24 s; see `build_context`.
        self.tail_bars = tail_bars
        # (symbol, bar) already written, plus the file size that set was read at.
        # See `logged_bars`.
        self._seen_cache: set[tuple[str, int]] | None = None
        self._seen_size: int = -1
        self.log_path = Path(log_path) if log_path else (
            Path(__file__).resolve().parents[1] / "reports" / "signals.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._legs = LEGS_LONG if self.cfg.side == "long" else LEGS_SHORT

    # -- evaluation --------------------------------------------------------

    def evaluate_index(self, ctx, df, i: int, spread: float | None = None,
                       position_since_ms: int | None = None) -> Evaluation:
        """The rule at anchor bar ``i``. Pure — writes nothing.

        ``position_since_ms`` is the open time of the OLDEST position already
        running, or None when a slot is free. In the live path it is derived from
        the broker's open tickets; in a backfill it is simulated the same way the
        harness does it.
        """
        from ..engine import features as F

        c = self.cfg
        ot = ctx.open_time
        step = INTERVAL_MS[c.anchor]
        legs: dict[str, bool] = {}
        for label, name in self._legs:
            if not c.use_5m_leg and name.startswith("htf_5m"):
                continue
            legs[label] = bool(evaluate_signal(name, ctx)[i])

        in_session = (F.is_gold_session(int(ot[i]))
                      if c.session == "gold_session" else True)
        usable = bool(ctx.ind["usable"].to_numpy()[i]) if "usable" in ctx.ind else True
        atr = float(ctx.ind["atr_14"].to_numpy()[i])
        close = float(ctx.close[i])

        blocking = [k for k, v in legs.items() if not v]
        reason = None
        if not in_session:
            reason = "outside gold_session"
        elif not usable:
            reason = "indicator warm-up incomplete"
        elif not np.isfinite(atr) or atr <= 0:
            reason = "no ATR"
        elif blocking:
            reason = "legs false: " + ", ".join(blocking)

        fired = reason is None
        would_enter = fired and position_since_ms is None
        if fired and position_since_ms is not None:
            reason = ("already in a position opened "
                      + datetime.fromtimestamp(position_since_ms / 1000, timezone.utc)
                        .strftime("%Y-%m-%d %H:%M") + "Z")

        # Entry reference is the CLOSE of the signal bar; the fill will be the
        # NEXT bar's open, which is what the measurement assumed and what the
        # executor must reproduce. The levels below are quoted from that
        # reference and re-derived from the actual fill at send time.
        entry_ref = close if would_enter else None
        sl = tp = None
        if would_enter:
            sl, tp = c.bracket(entry_ref, atr)

        return Evaluation(
            schema=SCHEMA_VERSION,
            ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            bar_open_ms=int(ot[i]),
            bar_close_ms=int(ot[i]) + step,
            symbol=c.symbol,
            broker_symbol=c.broker_symbol,
            anchor=c.anchor,
            side=c.side,
            fired=fired,
            would_enter=would_enter,
            position_since_ms=position_since_ms,
            legs=legs,
            blocking=blocking,
            close=_round(close, 2),
            atr_entry=_round(atr, 4) if fired else _round(atr, 4),
            entry_ref=_round(entry_ref, 2),
            sl=_round(sl, 2),
            tp=_round(tp, 2),
            time_stop_ms=(int(ot[i]) + step + c.time_stop_bars * step)
                         if would_enter else None,
            lots=c.lots,
            in_session=in_session,
            spread_at_eval=_round(spread, 3),
            reason_if_skipped=reason,
            config_hash=self.config_hash(),
        )

    def config_hash(self) -> str:
        import hashlib

        blob = json.dumps(asdict(self.cfg), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    # -- driving -----------------------------------------------------------

    def context(self, db: Database, tail_bars: int | None = None):
        return build_context(db, self.cfg.symbol, self.cfg.anchor,
                             self.ind_cfg, self.cfg.dip_lookback,
                             tail_bars if tail_bars is not None else self.tail_bars)

    def evaluate_latest(self, db: Database, spread: float | None = None,
                        position_since_ms: int | None = None) -> Evaluation:
        """The newest CLOSED bar. Never the forming one."""
        ctx, df, _cov = self.context(db)
        return self.evaluate_index(ctx, df, len(ctx.open_time) - 1, spread,
                                   position_since_ms)

    def backfill(
        self, db: Database, since_ms: int | None = None, spread: float | None = None
    ) -> list[Evaluation]:
        """Every closed bar from ``since_ms``, oldest first.

        Used to seed the log and, in `live_vs_backtest.py`, to reconcile the live
        path against the harness bar for bar.
        """
        from ..tools.validate_rule import walk_forward

        c = self.cfg
        # Explicitly untailed, whatever this instance was constructed with: a
        # backfill walks the whole panel and is what the §D3 reconciliation
        # compares against the harness bar for bar.
        ctx, df, _cov = build_context(db, c.symbol, c.anchor, self.ind_cfg,
                                      c.dip_lookback, tail_bars=None)
        ot = ctx.open_time
        opens = df["open"].to_numpy()
        highs, lows, closes = (df[k].to_numpy() for k in ("high", "low", "close"))
        atr = ctx.ind["atr_14"].to_numpy()
        step = INTERVAL_MS[c.anchor]
        horizon_ms = c.time_stop_bars * step
        lo = 0 if since_ms is None else int(np.searchsorted(ot, int(since_ms), "left"))

        # Occupancy is simulated exactly as the harness does it — same
        # walk_forward, same bracket, same time stop — so a backfilled log can be
        # compared with a measurement bar for bar.
        #
        # The simulation starts BEFORE `since_ms`, never at it: a position opened
        # earlier would otherwise be invisible and its bars mislabelled as
        # entries. A position lasts at most `time_stop_bars`, but a wrongly
        # allowed entry can cascade into the next, so the run-up is a generous
        # multiple of that rather than exactly one. `--all` simulates the whole
        # panel and is what the §D3 reconciliation uses.
        sim_from = max(0, lo - self.SIM_RUNUP_BARS * c.time_stop_bars)
        out: list[Evaluation] = []
        live: list[tuple[int, int]] = []      # (opened_ms, exits_ms) per slot
        for i in range(sim_from, ot.size):
            live = [x for x in live if x[1] > ot[i]]
            blocked = len(live) >= c.max_concurrent
            oldest = min((x[0] for x in live), default=None) if blocked else None
            e = self.evaluate_index(ctx, df, i, spread, oldest)
            if i >= lo:
                out.append(e)
            if e.would_enter and i + 1 < ot.size:
                entry = float(opens[i + 1])
                a = float(atr[i])
                if not (np.isfinite(entry) and np.isfinite(a) and a > 0):
                    continue
                sl, tp = c.bracket(entry, a)
                _r, _px, xt, _amb = walk_forward(
                    ot, highs, lows, closes, int(ot[i + 1]), entry, tp, sl,
                    c.side == "long", horizon_ms)
                live.append((int(ot[i]), int(xt)))
        return out

    # -- logging -----------------------------------------------------------

    def logged_bars(self, use_cache: bool = False) -> set[tuple[str, int]]:
        """`(symbol, bar_open_ms)` already written, so a restart cannot duplicate.

        **Keyed on the symbol as well as the bar, and that is not cosmetic.**
        One log holds every evaluation the system has ever made, and gold on two
        venues sits on the same 15-minute grid — de-duplicating on `bar_open_ms`
        alone meant a `XAUUSDT` bar was silently dropped because an `MT5:GOLD`
        row already existed at that timestamp. The signal would simply never be
        written, and the audit trail would show nothing at all.
        """
        if not self.log_path.exists():
            return set()
        # A long-running watcher calls this on every cycle. Re-reading the whole
        # file each time is O(log size) forever: at 96 evaluations a day the log
        # passes 24 MB inside a year, and re-parsing it every 300 s is gigabytes
        # of pointless disk a day. So the set is cached and only re-read when the
        # file has grown by something OTHER than our own appends — which is how
        # a `cli signals` run in another window still gets noticed.
        if use_cache and self._seen_cache is not None:
            try:
                if self.log_path.stat().st_size == self._seen_size:
                    return self._seen_cache
            except OSError:
                pass
        seen: set[tuple[str, int]] = set()
        with self.log_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    seen.add((rec.get("symbol", ""), int(rec["bar_open_ms"])))
                except (ValueError, KeyError):
                    continue
        self._seen_cache = seen
        try:
            self._seen_size = self.log_path.stat().st_size
        except OSError:
            self._seen_size = -1
        return seen

    def write(self, evals: list[Evaluation], skip_existing: bool = True) -> int:
        """Append, one JSON object per line. Returns lines written.

        Append-only and never rewritten: this file is the audit trail that
        `broker_trades` is reconciled against, and an entry that can be edited
        after the fact cannot serve that purpose.
        """
        if not evals:
            return 0
        seen = self.logged_bars(use_cache=True) if skip_existing else set()
        rows = [e for e in evals if (e.symbol, e.bar_open_ms) not in seen]
        if not rows:
            return 0
        tmp = self.log_path.with_name(self.log_path.name + f".{os.getpid()}.part")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in rows:
                fh.write(json.dumps(asdict(e), separators=(",", ":")) + "\n")
        with self.log_path.open("a", encoding="utf-8") as out, \
                tmp.open(encoding="utf-8") as src:
            out.write(src.read())
        tmp.unlink(missing_ok=True)
        # Fold what we just wrote into the cache and re-stamp the size, so the
        # next call is a hit rather than a full re-read of our own append.
        if self._seen_cache is not None:
            self._seen_cache.update((e.symbol, e.bar_open_ms) for e in rows)
            try:
                self._seen_size = self.log_path.stat().st_size
            except OSError:
                self._seen_cache = None
        return len(rows)

    def attach_ticket(self, bar_open_ms: int, ticket: int) -> bool:
        """Record which order a signal produced.

        §D2: every fill must trace back to the line that caused it, so a fill
        with no matching signal can be detected — that means something else is
        trading the account. Written as a separate `link` record rather than by
        editing the original line, because the log is append-only.
        """
        rec = {"schema": SCHEMA_VERSION, "kind": "link",
               "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "bar_open_ms": int(bar_open_ms), "order_ticket": int(ticket)}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._seen_cache = None      # the file grew outside `write`
        return True


def read_log(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out
