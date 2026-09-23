"""The only file in this project that can send an order.

`AUTOMATION.md` §C / `OPERATING_PLAN.md` §10.5. `data/mt5_live.py` is read-only by
construction and a test enforces that; the ability to trade lives here and
nowhere else, so "can this code place an order" has exactly one answer to check.

**`execute` defaults to False.** Nothing is sent until it is explicitly turned on
*and* the terminal's own AutoTrading is enabled. Both are the owner's decision.

**Every entry carries its stop in the same `order_send`.** Never an order followed
by a stop: that window is not theoretical — four ETHUSD shorts sat open with no
stop on this account on 2026-08-23. If `sl` is empty this code refuses to send,
before the broker is ever asked.

**Two slots, tracked by ticket, never netted.** `max_concurrent = 2` means two
independent positions in the same symbol and the same direction, each with its
own stop, target and time stop. MT5 on a hedging account keeps them separate and
so does this: one position's exit logic never touches the other's. They are not
diversification — the correlation shows up as longer losing runs (worst streak
9 -> 15 going from one slot to two).

**Restart safety comes before evaluation.** On start, open tickets are read back
from the terminal and adopted *before* any signal is considered. A restart that
evaluated first would see zero known positions, find a free slot, and open a
third.

The guards are in dollars and each was measured on the real sequence at this
bracket and this slot count — see `OPERATING_PLAN.md` §10.3/§10.5. A guard that
fires on ordinary variance gets switched off by its owner and is then not there
for the one time it mattered.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..data.db import Database
from ..engine.live_signal import Evaluation, LiveSignal, RuleConfig
from ..notify import telegram as N

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

# MT5 constants, named rather than magic.
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TIME_GTC = 0
RETCODE_DONE = 10009
RETCODE_REQUOTE, RETCODE_PRICE_OFF = 10004, 10021
POSITION_TYPE_BUY = 0


@dataclass
class Position:
    """One slot. Levels are frozen at entry and never moved."""

    ticket: int
    symbol: str
    side: str
    lots: float
    opened_ms: int
    entry: float
    sl: float
    tp: float
    time_stop_ms: int
    signal_bar_ms: int | None = None
    adopted: bool = False        # found at startup rather than opened by us


@dataclass
class GuardState:
    """Everything a halt decision needs, persisted so a restart cannot forget."""

    consecutive_losses: int = 0
    day: str = ""
    day_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    recent_results: list[int] = field(default_factory=list)   # 1 win, 0 loss

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


@dataclass
class Preflight:
    ok: bool
    failures: list[str]
    equity: float = 0.0
    spread_points: int = 0


class Executor:
    """Evaluates, guards, and — only if `execute` — sends."""

    def __init__(
        self,
        cfg: RuleConfig | None = None,
        execute: bool = False,
        db_path: str | Path | None = None,
        state_path: Path | None = None,
        notifier: N.Notifier | None = None,
    ) -> None:
        self.cfg = cfg or RuleConfig()
        self.cfg.assert_matches_measured_system()
        self.execute = bool(execute)
        self.db_path = db_path
        self.signal = LiveSignal(self.cfg)
        self.notify = notifier or N.build()
        root = Path(__file__).resolve().parents[1] / "reports"
        root.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(state_path) if state_path else root / "exec_state.json"
        self.halt_file = root / "HALT"
        self.order_log = root / "orders.jsonl"
        self.positions: dict[int, Position] = {}
        self.state = self._load_state()

    # -- state -------------------------------------------------------------

    def _load_state(self) -> GuardState:
        if self.state_path.exists():
            try:
                return GuardState(**json.loads(self.state_path.read_text(encoding="utf-8")))
            except Exception as exc:
                log.warning("could not read %s (%s); starting fresh",
                            self.state_path.name, exc)
        return GuardState(day=self._today())

    def _save_state(self) -> None:
        self.state_path.write_text(self.state.to_json(), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _log_order(self, kind: str, request: dict, result: Any) -> None:
        """Every request and every result, whether or not it succeeded."""
        rec = {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "kind": kind, "execute": self.execute, "request": request,
               "result": None if result is None else {
                   k: getattr(result, k, None)
                   for k in ("retcode", "deal", "order", "price", "volume", "comment")}}
        with self.order_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    # -- startup: adopt before evaluating ---------------------------------

    def adopt_open_positions(self, api) -> list[Position]:
        """Read live tickets back from the terminal. Must run before any signal.

        A restart that evaluated first would see no positions, believe both slots
        free, and open a third. Adopted positions keep the broker's own SL/TP;
        their time stop is reconstructed from the open time, which is the one
        thing MT5 does not store for us.
        """
        self.positions.clear()
        try:
            live = api.positions_get(symbol=self.cfg.broker_symbol) or []
        except Exception as exc:
            log.error("could not read open positions: %s", exc)
            raise
        from ..data.broker_clock import server_naive_to_utc_ms
        from ..data.db import INTERVAL_MS

        step = INTERVAL_MS[self.cfg.anchor]
        for p in live:
            opened = int(server_naive_to_utc_ms([int(p.time)])[0])
            pos = Position(
                ticket=int(p.ticket), symbol=str(p.symbol),
                side="long" if p.type == POSITION_TYPE_BUY else "short",
                lots=float(p.volume), opened_ms=opened,
                entry=float(p.price_open), sl=float(p.sl or 0.0),
                tp=float(p.tp or 0.0),
                time_stop_ms=opened + self.cfg.time_stop_bars * step,
                adopted=True)
            self.positions[pos.ticket] = pos
            if not pos.sl:
                self.notify.send(N.alert(
                    "ADOPTED POSITION HAS NO STOP",
                    f"ticket {pos.ticket} {pos.symbol} {pos.side} {pos.lots} lots "
                    f"opened {datetime.fromtimestamp(opened/1000, MYT):%Y-%m-%d %H:%M} MYT. "
                    "This code never sends an order without one, so it was opened "
                    "elsewhere."), N.P1)
        log.info("adopted %d open position(s): %s", len(self.positions),
                 sorted(self.positions))
        return list(self.positions.values())

    # -- guards ------------------------------------------------------------

    def check_halt(self, equity: float) -> tuple[bool, str]:
        """Order matters: the kill switch is checked before anything else."""
        if self.halt_file.exists():
            return True, f"kill switch present ({self.halt_file.name})"
        if self.state.halted:
            return True, self.state.halt_reason or "halted"
        if equity and equity <= self.cfg.equity_floor_usd:
            return True, (f"equity ${equity:,.2f} at or below the "
                          f"${self.cfg.equity_floor_usd:,.0f} floor")
        if self.state.consecutive_losses >= self.cfg.consecutive_loss_halt:
            return True, (f"{self.state.consecutive_losses} consecutive losses "
                          f"(limit {self.cfg.consecutive_loss_halt}; this fires "
                          "about once per 2.1 years, so treat it as a fault)")
        if self.state.day == self._today() and \
                self.state.day_pnl <= -self.cfg.daily_loss_limit_usd:
            return True, (f"daily loss ${-self.state.day_pnl:,.2f} at or beyond the "
                          f"${self.cfg.daily_loss_limit_usd:,.0f} limit")
        return False, ""

    def review_due(self) -> tuple[bool, str]:
        """Not a halt — a flag that the edge may have gone."""
        r = self.state.recent_results[-self.cfg.review_window_trades:]
        if len(r) < self.cfg.review_window_trades:
            return False, ""
        wr = sum(r) / len(r) * 100
        if wr < self.cfg.review_win_pct:
            return True, (f"last {len(r)} closed trades won {wr:.1f}%, below the "
                          f"{self.cfg.review_win_pct:.0f}% review trigger "
                          "(break-even is 34.2%)")
        return False, ""

    def preflight(self, api) -> Preflight:
        """Refuse to trade unless every one of these passes."""
        fails: list[str] = []
        equity = 0.0
        spread = 0

        ti, ai = api.terminal_info(), api.account_info()
        if ti is None or ai is None:
            return Preflight(False, ["terminal not attached"], 0.0, 0)
        equity = float(ai.equity)
        if not ti.trade_allowed:
            fails.append("terminal AutoTrading is off (trade_allowed=False)")
        if not getattr(ai, "trade_expert", True):
            fails.append("account does not permit expert trading")

        si = api.symbol_info(self.cfg.broker_symbol)
        if si is None:
            fails.append(f"symbol {self.cfg.broker_symbol} unavailable")
        else:
            if int(si.trade_mode) == 0:
                fails.append(f"{self.cfg.broker_symbol} trading disabled by broker")
            spread = int(si.spread)
            if spread > self.cfg.max_spread_points:
                fails.append(f"spread {spread} points > "
                             f"{self.cfg.max_spread_points} limit")
            need = float(si.volume_min) * 2
            if float(ai.margin_free) <= 0 or float(ai.margin_free) < need:
                fails.append(f"free margin ${ai.margin_free:,.2f} insufficient")

        halted, why = self.check_halt(equity)
        if halted:
            fails.append(why)
        return Preflight(not fails, fails, equity, spread)

    # -- sending -----------------------------------------------------------

    def open_position(self, api, ev: Evaluation, price_hint: float | None = None):
        """Send entry and stop as ONE order. Refuses if the stop is missing."""
        c = self.cfg
        if not ev.would_enter:
            raise ValueError("open_position called on a bar that would not enter")
        si = api.symbol_info(c.broker_symbol)
        tick = api.symbol_info_tick(c.broker_symbol)
        if si is None or tick is None:
            raise RuntimeError("no symbol info or tick")

        # Levels are re-derived from the ACTUAL fill reference, not the signal
        # bar's close — the measurement entered at the next bar's open and the
        # bracket is a multiple of the signal bar's ATR, so the multiple travels
        # with the fill rather than the level.
        px = float(price_hint if price_hint is not None else
                   (tick.ask if c.side == "long" else tick.bid))
        atr = float(ev.atr_entry or 0.0)
        if atr <= 0:
            raise ValueError("no ATR on the signal bar; refusing to size a stop")
        digits = int(si.digits)
        if c.side == "long":
            sl, tp = round(px - c.sl_atr * atr, digits), round(px + c.tp_atr * atr, digits)
        else:
            sl, tp = round(px + c.sl_atr * atr, digits), round(px - c.tp_atr * atr, digits)

        # The refusal that matters. Checked here, before the broker is asked.
        if not sl or sl <= 0 or sl == px:
            raise ValueError("refusing to send an order without a valid stop loss")

        req = {
            "action": TRADE_ACTION_DEAL,
            "symbol": c.broker_symbol,
            "volume": float(c.lots),
            "type": ORDER_TYPE_BUY if c.side == "long" else ORDER_TYPE_SELL,
            "price": px,
            "sl": sl,
            # tp is left to the broker as a convenience only; the executor's own
            # time stop and exit logic are authoritative.
            "tp": tp,
            "deviation": 20,
            "type_time": ORDER_TIME_GTC,
            "type_filling": int(getattr(si, "filling_mode", 1)),
            "comment": f"rule {ev.config_hash}",
        }

        if not self.execute:
            self._log_order("open.dry", req, None)
            log.info("[execute=false] would open %s %s @ %.2f sl %.2f tp %.2f",
                     c.side, c.lots, px, sl, tp)
            return None

        res = api.order_send(req)
        self._log_order("open", req, res)
        if res is not None and res.retcode in (RETCODE_REQUOTE, RETCODE_PRICE_OFF):
            # One retry at the refreshed price, then give up: chasing a moving
            # market is how a rule-based entry turns into a discretionary one.
            tick = api.symbol_info_tick(c.broker_symbol)
            req["price"] = float(tick.ask if c.side == "long" else tick.bid)
            res = api.order_send(req)
            self._log_order("open.retry", req, res)
        if res is None or res.retcode != RETCODE_DONE:
            code = getattr(res, "retcode", "none")
            self.notify.send(N.alert("ORDER REJECTED",
                                     f"{c.side} {c.lots} {c.broker_symbol}: {code}"),
                             N.P1)
            return None

        pos = Position(ticket=int(res.order), symbol=c.broker_symbol, side=c.side,
                       lots=float(c.lots), opened_ms=int(time.time() * 1000),
                       entry=float(res.price), sl=sl, tp=tp,
                       time_stop_ms=int(ev.time_stop_ms or 0),
                       signal_bar_ms=ev.bar_open_ms)
        self.positions[pos.ticket] = pos
        self.signal.attach_ticket(ev.bar_open_ms, pos.ticket)
        self.notify.send(N.opened(pos.ticket, pos.symbol, pos.side, pos.lots,
                                  pos.entry, sl, tp, pos.time_stop_ms,
                                  len(self.positions), c.max_concurrent), N.P1)
        return pos

    def close_position(self, api, pos: Position, reason: str):
        """Market close of ONE ticket. Never touches another slot."""
        si = api.symbol_info(pos.symbol)
        tick = api.symbol_info_tick(pos.symbol)
        px = float(tick.bid if pos.side == "long" else tick.ask)
        req = {
            "action": TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.lots,
            "type": ORDER_TYPE_SELL if pos.side == "long" else ORDER_TYPE_BUY,
            "position": pos.ticket,          # this ticket, and only this ticket
            "price": px,
            "deviation": 20,
            "type_time": ORDER_TIME_GTC,
            "type_filling": int(getattr(si, "filling_mode", 1)),
            "comment": f"exit {reason}",
        }
        if not self.execute:
            self._log_order("close.dry", req, None)
            log.info("[execute=false] would close %s (%s) @ %.2f", pos.ticket, reason, px)
            return None
        res = api.order_send(req)
        self._log_order("close", req, res)
        if res is None or res.retcode != RETCODE_DONE:
            self.notify.send(N.alert("CLOSE REJECTED",
                                     f"ticket {pos.ticket}: "
                                     f"{getattr(res, 'retcode', 'none')}"), N.P1)
            return None
        self.positions.pop(pos.ticket, None)
        return res

    # -- accounting --------------------------------------------------------

    def record_close(self, pnl: float, equity: float) -> None:
        """Update the guards from a realised result."""
        today = self._today()
        if self.state.day != today:
            self.state.day, self.state.day_pnl = today, 0.0
        self.state.day_pnl += pnl
        won = pnl > 0
        self.state.consecutive_losses = 0 if won else self.state.consecutive_losses + 1
        self.state.recent_results.append(1 if won else 0)
        del self.state.recent_results[:-200]

        halted, why = self.check_halt(equity)
        if halted and not self.state.halted:
            self.state.halted, self.state.halt_reason = True, why
            self.notify.send(N.halted("guard tripped", why, equity), N.P1)
        due, msg = self.review_due()
        if due:
            self.notify.send(N.alert("REVIEW TRIGGER", msg), N.P1)
        self._save_state()

    def time_stopped(self, now_ms: int | None = None) -> list[Position]:
        """Positions past their 24 h stop. Part of the measured system (§8.4.1)."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return [p for p in self.positions.values()
                if p.time_stop_ms and now >= p.time_stop_ms]

    def free_slots(self) -> int:
        return max(0, self.cfg.max_concurrent - len(self.positions))
