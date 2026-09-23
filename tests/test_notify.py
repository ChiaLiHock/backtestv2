"""Notifications, and the guard that keeps a bot token out of the repository.

`notify/telegram.py` and `tools/setup_telegram.py` both promise in their
docstrings that this file fails if anything token-shaped reaches the source
tree. Until now that promise was not kept by anything -- the file did not exist.
It does now, because the failure it guards against actually happened: a live
token was pasted into a chat during setup, and the next step after revoking one
is making sure the replacement cannot be committed by accident.

What is pinned here:

* **No credential in the tree.** A repo-wide scan for the Telegram token shape,
  covering every text file except the gitignored env file itself.
* **The env file stays ignored.** A `.gitignore` entry that gets reorganised
  away would be silent until the day it was not.
* **Redaction works.** A token must not reach a log line even when it appears
  inside an exception message.
* **Nothing about notifying can break trading.** An unconfigured or failing
  notifier degrades to console and returns False; it never raises.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from backtest.notify import telegram as tg

ROOT = Path(__file__).resolve().parents[1]

# digits, a colon, then ~35 chars of base64-ish -- the Telegram bot token shape.
TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")

SCAN_SUFFIXES = {".py", ".json", ".md", ".txt", ".yaml", ".yml", ".bat", ".vbs",
                 ".html", ".env", ".example", ".cfg", ".ini", ".jsonl"}
SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "reports"}


def _files():
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        # The owner's own credential file is gitignored by design; scanning it
        # would fail the moment setup succeeds, which is exactly backwards.
        if p == tg.ENV_FILE:
            continue
        yield p


class TestNoCredentialInTheRepo:
    def test_nothing_token_shaped_anywhere_in_the_source_tree(self):
        hits = []
        for p in _files():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in TOKEN_RE.finditer(text):
                line = text[:m.start()].count("\n") + 1
                hits.append(f"{p.relative_to(ROOT)}:{line}")
        assert not hits, (
            "something token-shaped is in the repository: "
            + ", ".join(hits)
            + " -- revoke it with @BotFather /revoke and remove it")

    def test_the_example_file_ships_empty(self):
        """The template must never carry a real value."""
        ex = tg.ENV_FILE.with_suffix(".env.example")
        if not ex.exists():
            pytest.skip("no example file")
        text = ex.read_text(encoding="utf-8")
        assert not TOKEN_RE.search(text)
        for line in text.splitlines():
            if line.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID=")):
                assert line.split("=", 1)[1].strip() == "", line

    def test_env_file_is_gitignored(self):
        """One reorganised .gitignore away from committing a password."""
        rel = tg.ENV_FILE.relative_to(ROOT).as_posix()
        found = False
        for gi in (ROOT / ".gitignore", ROOT.parent / ".gitignore"):
            if not gi.exists():
                continue
            for line in gi.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line.lstrip("/") in (
                        rel, tg.ENV_FILE.name, f"notify/{tg.ENV_FILE.name}"):
                    found = True
        assert found, f"{rel} must be gitignored"


class TestRedaction:
    def test_a_token_in_an_error_string_is_not_logged(self, monkeypatch):
        """Send failures log the exception text, and a URLError from urllib
        carries the full request URL -- which contains the token.

        The fake is assembled rather than written out: a literal would be
        token-shaped, and the scan above would then fail on this very file.
        """
        fake = "1234567890" + ":" + "A" * 35
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", fake)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        leaked = f"failed opening https://api.telegram.org/bot{fake}/sendMessage"
        assert fake not in tg._redact(leaked)

    def test_redaction_leaves_ordinary_text_alone(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:" + "A" * 35)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        assert "connection refused" in tg._redact("connection refused")


class TestDegradesRatherThanBreaks:
    def test_unconfigured_falls_back_to_console(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setattr(tg, "ENV_FILE", tmp_path / "absent.env")
        assert type(tg.build()).__name__ == "ConsoleNotifier"

    def test_console_notifier_always_succeeds_and_never_raises(self):
        assert tg.ConsoleNotifier().send("anything") is True

    def test_a_dead_endpoint_returns_false_rather_than_raising(self, monkeypatch):
        """A chat outage must not stop or delay trading."""
        n = tg.TelegramNotifier("1234567890:" + "A" * 35, "999")
        # The module's API string takes {token} only -- port 9 is discard, so
        # this is a connection that cannot succeed.
        monkeypatch.setattr(tg, "API", "http://127.0.0.1:9/bot{token}/sendMessage")
        monkeypatch.setattr(n, "MIN_INTERVAL_S", 0, raising=False)
        assert n.send("hello") is False
        assert n.failed >= 1

    def test_dry_run_sends_nothing(self):
        n = tg.TelegramNotifier("1234567890:" + "A" * 35, "999", dry_run=True)
        assert n.send("hello") is True
        assert n.sent == 0


class TestSignalMessage:
    def test_a_mined_signal_says_it_is_mined(self):
        """Five channels arrive on a phone with no chart beside them. If they
        read alike they will be trusted alike, and only one is measured."""
        txt = tg.signal_fired("signal5", "XAUUSDT", "short", 4600.0, 4610.0,
                              4590.0, 0.01, "x", pattern="p", measured=False)
        assert "mined" in txt.lower()
        assert "not the measured rule" in txt

    def test_the_measured_rule_carries_no_such_warning(self):
        txt = tg.signal_fired("rule", "XAUUSDT", "long", 4600.0, 4575.0,
                              4625.0, 0.01, "x", measured=True)
        assert "not the measured rule" not in txt

    def test_direction_is_visible_without_reading_the_words(self):
        up = tg.signal_fired("rule", "X", "long", 1, 1, 1, 1, "x")
        dn = tg.signal_fired("rule", "X", "short", 1, 1, 1, 1, "x")
        assert up[:1] != dn[:1] or "↗" in up


class TestEnvValue:
    def test_reads_a_non_secret_setting_from_the_env_file(self, monkeypatch, tmp_path):
        f = tmp_path / "telegram.env"
        f.write_text("TELEGRAM_SIGNALS=rule,signal3\n", encoding="utf-8")
        monkeypatch.setattr(tg, "ENV_FILE", f)
        monkeypatch.delenv("TELEGRAM_SIGNALS", raising=False)
        assert tg.env_value("TELEGRAM_SIGNALS") == "rule,signal3"

    def test_environment_wins_over_the_file(self, monkeypatch, tmp_path):
        f = tmp_path / "telegram.env"
        f.write_text("TELEGRAM_SIGNALS=fromfile\n", encoding="utf-8")
        monkeypatch.setattr(tg, "ENV_FILE", f)
        monkeypatch.setenv("TELEGRAM_SIGNALS", "fromenv")
        assert tg.env_value("TELEGRAM_SIGNALS") == "fromenv"

    def test_missing_key_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tg, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("NOPE", raising=False)
        assert tg.env_value("NOPE") is None


class TestSetupToolRejectsBadInputBeforeTheNetwork:
    @pytest.mark.parametrize("bad", ["", "not-a-token", "12345", "abc:def",
                                     "1234567890:short"])
    def test_malformed_tokens_are_refused(self, bad):
        from backtest.tools.setup_telegram import TOKEN_RE as T
        assert not T.match(bad)

    def test_a_well_formed_token_is_accepted(self):
        from backtest.tools.setup_telegram import TOKEN_RE as T
        assert T.match("1234567890:" + "A" * 35)

    def test_tail_shows_four_characters_at_most(self):
        """Enough to tell two tokens apart in a confirmation line, never enough
        to use one."""
        from backtest.tools.setup_telegram import tail
        t = "1234567890:" + "A" * 30 + "WXYZ"
        out = tail(t)
        assert out == "...WXYZ"
        assert t not in out and len(out) <= 7


class TestTransportIsNotConfusedWithRejection:
    """The bug that cost a token.

    `urllib` cannot verify api.telegram.org on this machine -- it validates
    against the Windows store, of which Python reads only ~30 roots, and gets
    "self-signed certificate in certificate chain". httpx uses the bundled
    certifi roots and connects. Everything else in this project already used
    httpx; only the notifier did not.

    The failure was then reported as "Telegram rejected this token", which is a
    sentence about the token, so the token got revoked. It was never the token.
    These pin both halves: the right transport, and an error message that says
    which of the two things actually went wrong.
    """

    def test_notifier_and_setup_both_use_httpx_not_urllib(self):
        import ast
        import pathlib

        import backtest.tools.setup_telegram as st

        for mod in (tg, st):
            src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
            tree = ast.parse(src)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            assert "urllib" not in imported, (
                f"{mod.__name__} imports urllib; it cannot verify "
                "api.telegram.org on this machine")
            assert "httpx" in imported or "httpx" in src, mod.__name__

    def test_a_transport_failure_is_flagged_as_transport(self, monkeypatch):
        """Unreachable host -> transport_error, so the message can say 'could
        not reach Telegram' instead of blaming the credential."""
        import backtest.tools.setup_telegram as st

        monkeypatch.setattr(st, "API", "http://127.0.0.1:9/bot{token}/{method}")
        out = st.call("1234567890" + ":" + "A" * 35, "getMe")
        assert out["ok"] is False
        assert out["transport_error"] is True

    def test_a_real_rejection_is_not_flagged_as_transport(self):
        """Telegram answering 'Unauthorized' is a token problem and must not be
        reported as a network one. Needs the network; skipped without it."""
        import backtest.tools.setup_telegram as st

        out = st.call("1234567890" + ":" + "A" * 35, "getMe")
        if out.get("transport_error"):
            pytest.skip("no network to Telegram in this environment")
        assert out["ok"] is False
        assert not out.get("transport_error")
        assert "auth" in str(out.get("description", "")).lower()


class TestTheTokenCannotReachAnyLog:
    """Two real leaks, both found in production output rather than in review.

    1. Switching the transport to httpx made `httpx` log the Bot API URL at
       INFO -- and the token lives in that URL's path. Redaction covered only
       this module's own log calls, so the secret was printed in clear.
    2. The first fix looked right and did nothing: written through a shell
       heredoc, the pattern's "\b" anchors became literal backspace bytes
       (0x08), so it matched nothing. `_redact` still appeared to work because
       it also does a literal replace of the loaded credential -- which masked
       the dead regex exactly when a test would have caught it.

    Hence: every check below uses a token the process has NEVER loaded, so only
    the regex can catch it and the literal-replace fallback cannot flatter it.
    """

    UNSEEN = "1122334455" + ":" + "ZZZZbbbbCCCCddddEEEEffffGGGGhhhh123"

    def test_the_pattern_is_not_corrupted_by_stray_control_characters(self):
        """The bug that made the guard useless while looking fine in a grep."""
        pat = tg._TOKEN_ANY.pattern
        assert "\x08" not in pat, "backspace byte in the pattern"
        assert all(ord(c) >= 32 for c in pat), f"control char in {pat!r}"

    def test_it_matches_a_token_glued_into_a_url_path(self):
        """`bot8637...` -- 't' to '8' is not a word boundary, so \b anchors
        would fail exactly where the leak happens."""
        url = f"https://api.telegram.org/bot{self.UNSEEN}/sendMessage"
        assert tg._TOKEN_ANY.search(url)

    def test_redaction_works_on_a_token_this_process_never_loaded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tg, "ENV_FILE", tmp_path / "absent.env")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        out = tg._redact(f"POST https://api.telegram.org/bot{self.UNSEEN}/sendMessage")
        assert self.UNSEEN not in out
        assert "botbot" not in out, "double prefix from substituting bot<token>"

    def _capture(self, logger_name, message, *args):
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        tg.install_log_redaction()
        root = logging.getLogger()
        h = Cap()
        root.addHandler(h)
        old = root.level
        root.setLevel(logging.INFO)
        try:
            logging.getLogger(logger_name).info(message, *args)
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        return records

    @pytest.mark.parametrize("logger_name", [
        "httpx", "httpcore", "urllib3",
        # A logger nobody has thought of. A filter attached to specific loggers
        # cannot cover this; the record factory does.
        "some.library.added.next.year",
    ])
    def test_no_logger_can_print_a_token(self, logger_name):
        got = self._capture(
            logger_name,
            "HTTP Request: POST https://api.telegram.org/bot%s/sendMessage",
            self.UNSEEN)
        assert got, "nothing captured"
        assert not any(self.UNSEEN in m for m in got), got

    def test_ordinary_logging_is_left_alone(self):
        got = self._capture("httpx",
                            "HTTP Request: GET https://api.bytick.com/v5/market/time")
        assert any("bytick" in m for m in got)


class TestHtmlIsEscaped:
    """`parse_mode=HTML` plus an unescaped "<" is a rejected message.

    Signal 2's own label contains "(<=1.13 ATR)", and Telegram answered
    `can't parse entities: Unsupported start tag "" at byte offset 137`. The
    signal fired, the alert did not arrive, and only a console line said so.
    """

    def test_the_exact_label_that_was_rejected_now_survives(self):
        from backtest.engine.signal2 import Signal2Config

        label = Signal2Config().label
        assert "<" in label, "the failing case needs a bare < in it"
        msg = tg.signal_fired("signal2", "XAUUSDT", "short", 4600.0, 4625.0,
                              4575.0, 0.01, "x", pattern=label, measured=False)
        assert "&lt;" in msg
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            msg = msg.replace(tag, "")
        assert "<" not in msg and ">" not in msg, msg

    def test_hostile_text_in_any_field_cannot_inject_markup(self):
        msg = tg.signal_fired("<script>x</script>", "<b>SYM</b>", "long",
                              1, 1, 1, 1, "<i>t</i>", pattern="a < b > c",
                              note="<u>n</u>", measured=True)
        for tag in ("<b>", "</b>", "<i>", "</i>"):
            msg = msg.replace(tag, "")
        assert "<" not in msg and ">" not in msg, msg

    def test_the_markup_this_function_adds_is_still_there(self):
        msg = tg.signal_fired("rule", "XAUUSDT", "long", 1, 1, 1, 1, "x")
        assert msg.startswith("<b>") and "</b>" in msg
