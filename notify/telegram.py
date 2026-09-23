"""Push notifications. `AUTOMATION.md` §F / §D5.

**No credential is asked for, stored in the repo, or logged.** The bot token and
chat id are read from the environment (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
or from a file the owner creates themselves at `backtest/notify/telegram.env`,
which is gitignored. Nothing in this repository contains a token, and
`tests/test_notify.py` fails if anything that looks like one appears in the
source tree. If neither source is present the notifier degrades to console
output — the trading system must never fail to trade because a chat bot is
unconfigured, and it must never be silent about the fact that it is not notifying.

**Delivery is best-effort and never blocks a decision.** A send happens after the
action it describes, never before, and a failed send is logged and dropped rather
than retried into a stall. The `signals.jsonl` log, not Telegram, is the record.

**What gets sent** (§D5, adjusted for the shipped config): a position opening, a
position closing with its reason and P/L, any P1 alert, and any halt. There is no
partial-fill or move-to-breakeven message because the shipped bracket has neither
— `OPERATING_PLAN.md` §8.4.2 keeps TP and SL fixed at entry.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)
MYT = timezone(timedelta(hours=8))

ENV_FILE = Path(__file__).with_name("telegram.env")
API = "https://api.telegram.org/bot{token}/sendMessage"

# Priority. P1 is "a human should look now"; anything else is a running commentary.
P1, P2 = "P1", "P2"


class Notifier(Protocol):
    """So the executor never knows or cares which backend is behind this."""

    def send(self, text: str, priority: str = P2) -> bool: ...


def _load_credentials() -> tuple[str, str] | None:
    """Environment first, then an owner-created file. Never a literal in code."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        return tok, chat
    if ENV_FILE.exists():
        vals = {}
        raw = ENV_FILE.read_text(encoding="utf-8-sig")  # BOM-tolerant
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            vals[k.strip().lstrip("\ufeff")] = v.strip().strip('"').strip("'")
        tok = vals.get("TELEGRAM_BOT_TOKEN", "")
        chat = vals.get("TELEGRAM_CHAT_ID", "")
        if tok and chat:
            return tok, chat
    return None


def env_value(key: str) -> str | None:
    """Read a NON-secret setting from the same env file the credentials use.

    Kept here rather than in the caller so there is one parser for that file and
    one place that knows its format. Only for settings like TELEGRAM_SIGNALS --
    the token and chat id go through `_load_credentials`, which is the only
    function that ever returns them.
    """
    v = os.environ.get(key, "").strip()
    if v:
        return v
    if ENV_FILE.exists():
        raw = ENV_FILE.read_text(encoding="utf-8-sig")  # BOM-tolerant
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip().lstrip("\ufeff") == key:
                val = val.strip().strip('"').strip("'")
                return val or None
    return None


# Any Telegram bot token, whether or not it is the one this process loaded.
# NO word-boundary anchors. Two reasons, the second of which bit hard:
#   1. In a URL the token is glued to the path -- "bot8637760813:AAE..." --
#      and t-to-8 is not a word boundary, so  would fail exactly where the
#      leak happens.
#   2. Written through a shell heredoc, "\b" became a literal backspace
#      (0x08) instead of the regex anchor, so this matched nothing at all and
#      the redaction filter was dead while still looking correct to grep.
_TOKEN_ANY = re.compile("[0-9]{6,}:[A-Za-z0-9_-]{30,}")


def _redact(text: str) -> str:
    """A token must never reach a log line, from ANY logger.

    Redacting only the loaded credential was not enough. `httpx` logs the full
    request URL at INFO, and the Bot API puts the token in the URL path, so the
    line

        HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage

    printed the secret in clear even though this module never logged it. The
    pattern is matched here rather than just the known value, so a token that
    belongs to some other process or config is scrubbed too.
    """
    # Replace the token itself only. Substituting "bot<token>" produced
    # "botbot<token>" in a URL that already ends in "bot".
    text = _TOKEN_ANY.sub("<token>", text)
    # Belt and braces for a credential that somehow does not match the shape.
    creds = _load_credentials()
    if creds:
        text = text.replace(creds[0], "<token>")
    return text


class _RedactTokens(logging.Filter):
    """Kept for direct use on a handler; the factory below is the real guard."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _TOKEN_ANY.search(msg):
            record.msg = _redact(msg)
            record.args = ()
        return True


_installed = False


def install_log_redaction() -> None:
    """Scrub bot tokens from EVERY log record, whoever creates it. Idempotent.

    Done with `setLogRecordFactory`, not with filters on loggers. A filter
    attached to a Logger only runs for records logged directly to that logger:
    records from a child logger propagate to the parent's HANDLERS but skip the
    parent's filters entirely. So a filter on the root logger misses everything
    that any library actually logs, which is precisely the case that leaked --
    `httpx` writes the Bot API URL, token and all, at INFO.

    Wrapping the record factory catches the record at creation, before any
    logger, handler, or formatter can see it, and it keeps working for loggers
    that do not exist yet.
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            msg = record.getMessage()
        except Exception:
            return record
        if _TOKEN_ANY.search(msg):
            record.msg = _redact(msg)
            record.args = ()
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


@dataclass
class ConsoleNotifier:
    """The fallback. Says clearly that nothing is being pushed anywhere."""

    prefix: str = "NOTIFY"

    def send(self, text: str, priority: str = P2) -> bool:
        log.info("[%s %s] %s", self.prefix, priority, text.replace("\n", " | "))
        return True


class TelegramNotifier:
    """Sends to one chat. Silent failures are logged, never raised."""

    # Telegram rejects bursts; the executor can produce several messages in one
    # cycle when two slots act together.
    MIN_INTERVAL_S = 0.4
    TIMEOUT_S = 8

    def __init__(self, token: str, chat_id: str, dry_run: bool = False) -> None:
        self._token = token
        self._chat = chat_id
        self.dry_run = dry_run
        self._last = 0.0
        self.sent = 0
        self.failed = 0

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "TelegramNotifier | None":
        creds = _load_credentials()
        return cls(creds[0], creds[1], dry_run) if creds else None

    def send(self, text: str, priority: str = P2) -> bool:
        if self.dry_run:
            log.info("[telegram dry-run %s] %s", priority, text.replace("\n", " | "))
            return True
        gap = time.time() - self._last
        if gap < self.MIN_INTERVAL_S:
            time.sleep(self.MIN_INTERVAL_S - gap)
        payload = {
            "chat_id": self._chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "false" if priority == P1 else "true",
        }
        try:
            # httpx, NOT urllib. On this machine urllib fails to verify
            # api.telegram.org -- "self-signed certificate in certificate
            # chain" -- because it validates against the Windows store, which
            # Python only reads ~30 roots out of. httpx validates against the
            # bundled `certifi` roots and connects fine, and it is what every
            # other outbound call in this project already uses. The rest of the
            # code reached Bybit over the same network without trouble, which is
            # what made the difference visible.
            import httpx

            r = httpx.post(API.format(token=self._token), data=payload,
                           timeout=self.TIMEOUT_S)
            ok = bool(r.json().get("ok", False))
            self._last = time.time()
            self.sent += ok
            self.failed += (not ok)
            if not ok:
                log.warning("telegram rejected the message: %s",
                            _redact(str(r.json().get("description", ""))))
            return ok
        except Exception as exc:
            # Never raise: a chat outage must not stop or delay trading, and the
            # authoritative record is signals.jsonl regardless.
            self.failed += 1
            log.warning("telegram send failed: %s", _redact(str(exc)))
            self._last = time.time()
            return False


def build(dry_run: bool = False) -> Notifier:
    """Telegram when configured, console otherwise — and it says which."""
    n = TelegramNotifier.from_env(dry_run=dry_run)
    if n is not None:
        log.info("notifications: Telegram%s", " (dry run)" if dry_run else "")
        return n
    log.warning(
        "notifications: CONSOLE ONLY — no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in "
        "the environment or %s. Trading is unaffected; you simply will not be "
        "told about it.", ENV_FILE.name)
    return ConsoleNotifier()


# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------


def _t(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, MYT).strftime("%Y-%m-%d %H:%M") + " MYT"


def opened(ticket: int, symbol: str, side: str, lots: float, price: float,
           sl: float, tp: float, time_stop_ms: int, slot: int, of: int) -> str:
    return (f"<b>OPEN</b> {side.upper()} {lots} {symbol} @ {price:,.2f}\n"
            f"SL {sl:,.2f}   TP {tp:,.2f}\n"
            f"time stop {_t(time_stop_ms)}\n"
            f"slot {slot}/{of}   ticket {ticket}")


def closed(ticket: int, symbol: str, reason: str, price: float, pnl: float,
           held_min: float, equity: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return (f"<b>CLOSE</b> {symbol} {reason.upper()} @ {price:,.2f}\n"
            f"P/L {sign}${pnl:,.2f}   held {held_min/60:.1f}h\n"
            f"equity ${equity:,.2f}   ticket {ticket}")


def halted(reason: str, detail: str, equity: float) -> str:
    return (f"<b>⛔ HALTED</b> — {reason}\n{detail}\n"
            f"equity ${equity:,.2f}\n"
            f"No new positions will open until this is cleared manually.")


def alert(what: str, detail: str) -> str:
    return f"<b>⚠ {what}</b>\n{detail}"


def signal_fired(name: str, symbol: str, side: str, price: float,
                 sl: float, tp: float, lots: float, bar_myt: str,
                 pattern: str = "", measured: bool = True,
                 note: str = "") -> str:
    """A signal firing. NOT a position -- nothing has been traded.

    `measured` is carried through and stated, because the five channels do not
    carry the same evidence: one is the validated rule (n=588 against a matched
    random-entry null) and four are mined patterns. A notification that made
    them look alike would be the single most misleading thing this file could
    send, since it arrives on a phone with no chart next to it.
    """
    head = "SIGNAL" if measured else "SIGNAL (mined)"
    arrow = "↗" if side.lower() == "long" else "↘"
    # parse_mode=HTML, so any bare "<" in a caller's text is read as a tag.
    # Signal 2's own label contains "(<=1.13 ATR)", which Telegram rejected
    # outright: 'can't parse entities: Unsupported start tag "" at byte 137'.
    # Escaping the dynamic parts, not the markup this function adds itself.
    symbol = html.escape(str(symbol))
    name = html.escape(str(name))
    pattern = html.escape(str(pattern)) if pattern else ""
    note = html.escape(str(note)) if note else ""
    bar_myt = html.escape(str(bar_myt))
    out = (f"<b>{arrow} {head} — {name}</b>\n"
           f"{side.upper()} {symbol} @ {price:,.2f}   {lots} lots\n"
           f"SL {sl:,.2f}   TP {tp:,.2f}\n"
           f"bar {bar_myt}")
    if pattern:
        out += f"\n<i>{pattern}</i>"
    if not measured:
        out += ("\n⚠ mined pattern, not the measured rule — "
                "it carries none of that evidence")
    if note:
        out += f"\n{note}"
    return out


def session_event(symbol: str, kind: str, price: float, level: float,
                  bar_myt: str, vol_ok: bool | None = None) -> str:
    """A session-map event — structure, not a trade.

    Deliberately does NOT reuse `signal_fired`: these are announcements about
    what the market did to a level (Asia high swept, a break reclaimed), and a
    phone reader with no chart must not mistake one for an entry
    recommendation. Sent at P2 — look soon, not now.
    """
    arrow = "↗" if kind.endswith("high") else "↘"
    vol_note = ""
    if kind.startswith("break") and vol_ok is False:
        vol_note = "\n⚠ quiet break — volume did not confirm it"
    return (f"<b>{arrow} SESSION — {html.escape(kind)}</b>\n"
            f"{html.escape(symbol)} @ {price:,.2f}"
            f"   (level {level:,.2f})\n"
            f"bar {html.escape(bar_myt)}"
            f"\n<i>structure, not a signal — nothing to act on by itself</i>"
            f"{vol_note}")


def day_verdict(symbol: str, bias: str, basis: list[str],
                as_of_myt: str) -> str:
    """The day's verdict from the session map: long, short or range.

    Sent once per CHANGE when the US session has had its half hour (20:30
    MYT). The basis lines are the facts the verdict was read from — a
    conclusion without its evidence would be exactly the kind of message
    this project refuses to send. P2 like the other session announcements.
    """
    arrow = {"long": "↗", "short": "↘", "range": "↔"}.get(bias, "•")
    label = {"long": "看多 LONG", "short": "看空 SHORT",
             "range": "震荡 RANGE"}.get(bias, bias)
    lines = [f"<b>{arrow} 日内判定 — {html.escape(label)}</b>",
             f"{html.escape(symbol)} · {html.escape(as_of_myt)}"]
    for b in basis[:4]:
        lines.append(f"· {html.escape(str(b))}")
    lines.append("<i>framework read, not a signal — 数据看预期，美盘定方向</i>")
    return "\n".join(lines)


def daily_summary(day: str, opened_n: int, closed_n: int, pnl: float,
                  equity: float, open_now: int, fired: int, would_enter: int) -> str:
    sign = "+" if pnl >= 0 else ""
    return (f"<b>{day}</b>\n"
            f"signals {fired} fired / {would_enter} actionable\n"
            f"opened {opened_n}   closed {closed_n}   P/L {sign}${pnl:,.2f}\n"
            f"equity ${equity:,.2f}   open now {open_now}")
