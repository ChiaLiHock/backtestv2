"""One-time Telegram setup, run by the owner, in the owner's own terminal.

    python -m backtest.tools.setup_telegram
    python -m backtest.tools.setup_telegram --status      report, change nothing
    python -m backtest.tools.setup_telegram --test        send a test message

## Why this exists instead of someone just editing the file

The bot token is a credential: anyone holding it controls the bot completely.
So it is typed **here**, straight into `notify/telegram.env`, and never pasted
into a chat, a ticket, or a commit. This tool:

* reads the token with `getpass`, so it is not echoed to the screen and does not
  land in the shell history;
* finds the chat id automatically from `getUpdates`, so that never has to be
  copied by hand out of a browser either;
* verifies the pair works by sending a real message before writing anything;
* writes the file with a redacted confirmation, printing only the last 4
  characters of the token so a typo is diagnosable without exposing it.

`notify/telegram.env` is gitignored, and `tests/test_notify.py` scans the whole
tree for the token shape -- so a credential that escapes into a tracked file
fails the suite rather than reaching a commit.

## If a token has ever been shared anywhere

Revoke it. @BotFather -> /revoke -> pick the bot -> it issues a new one and the
old one stops working immediately. A token that has been in a chat log, a
screenshot or a paste bin should be treated as public, because it is.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

from ..notify.telegram import ENV_FILE, build

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
LINE = "=" * 68


def say(*parts: str) -> None:
    print(*parts, sep="\n", flush=True)


def call(token: str, method: str, params: dict | None = None) -> dict:
    """Call one Bot API method.

    Uses httpx rather than urllib: on this machine urllib cannot verify
    api.telegram.org ("self-signed certificate in certificate chain") because
    it validates against the Windows store, while httpx uses the bundled
    `certifi` roots and connects fine. Everything else in this project already
    talks HTTP through httpx.

    `transport_error` is set when the request never reached Telegram at all.
    That distinction matters: a TLS or DNS failure reported as "Telegram
    rejected your token" sends someone off revoking a token that was never the
    problem.
    """
    import httpx

    url = API.format(token=token, method=method)
    try:
        r = httpx.post(url, data=params or {}, timeout=15)
    except Exception as exc:
        return {"ok": False, "transport_error": True,
                "description": f"{type(exc).__name__}: {exc}"}
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def tail(token: str) -> str:
    """Last 4 characters only -- enough to tell two tokens apart, not to use one."""
    return "..." + token[-4:] if len(token) > 4 else "????"


def find_chat_id(token: str) -> tuple[str | None, str]:
    """The chat id of whoever has messaged the bot most recently."""
    res = call(token, "getUpdates")
    if not res.get("ok"):
        return None, res.get("description", "getUpdates failed")
    updates = res.get("result") or []
    if not updates:
        return None, "no messages yet"
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            who = chat.get("username") or chat.get("first_name") or "?"
            return str(chat["id"]), f"from @{who}"
    return None, "updates carried no chat id"


def write_env(token: str, chat_id: str, channels: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        "# Written by tools/setup_telegram.py. Gitignored. Treat as a password.\n"
        "# Revoke and re-run this if the token is ever shared or pasted anywhere.\n\n"
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"TELEGRAM_CHAT_ID={chat_id}\n\n"
        "# Channels that push. signal5 is the busy one at ~16 a day; it is\n"
        "# capped at 3 concurrent positions, so a 35-minute run of firing bars\n"
        "# is 3 alerts and not 7. Drop a name from this list to mute it.\n"
        f"TELEGRAM_SIGNALS={channels}\n",
        encoding="utf-8")


def cmd_status() -> int:
    n = build()
    kind = type(n).__name__
    say("", LINE, "  Telegram status", LINE, "",
        f"  env file : {ENV_FILE}",
        f"  exists   : {'yes' if ENV_FILE.exists() else 'no'}",
        f"  notifier : {kind}", "")
    if kind != "TelegramNotifier":
        say("  Not configured. Run this without --status to set it up.", "")
        return 1
    say("  Configured. Send a test with --test.", "")
    return 0


def cmd_test() -> int:
    n = build()
    if type(n).__name__ != "TelegramNotifier":
        say("", "  Not configured yet -- run without --status/--test first.", "")
        return 1
    ok = n.send("<b>Test</b>\nGold watcher is wired up. "
                "You will get a message here when a signal fires.", priority="P1")
    say("", "  sent" if ok else "  send FAILED -- see the log line above", "")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.tools.setup_telegram")
    ap.add_argument("--status", action="store_true", help="report and exit")
    ap.add_argument("--test", action="store_true", help="send a test message")
    ap.add_argument("--signals",
                    default="rule,signal2,signal3,signal4,signal5",
                    help="channels that push")
    a = ap.parse_args(argv)

    if a.status:
        return cmd_status()
    if a.test:
        return cmd_test()

    say("", LINE, "  設定 Telegram 手機通知（只需要做這一次）", LINE, "",
        "  待會請你貼上 bot token。它會直接寫進：",
        f"    {ENV_FILE}",
        "  這個檔案在 .gitignore 裡，只有通知模組會讀它。",
        "",
        "  ⚠ 輸入時畫面「不會顯示任何字」，這是正常的（跟輸入密碼一樣）。",
        "    貼上後直接按 Enter 就好。",
        "",
        "  ⚠ 如果這個 token 曾經貼給任何人、貼進聊天室、或截過圖，",
        "    請先撤銷再回來：",
        "      Telegram → @BotFather → /revoke → 選你的 bot → 拿新的一組",
        "",
        "-" * 68, "")

    try:
        token = getpass.getpass("  貼上 bot token（不會顯示，貼完按 Enter）: ").strip()
    except (KeyboardInterrupt, EOFError):
        say("", "  已取消，沒有寫入任何東西。", "")
        return 1
    if not token:
        say("", "  你沒有輸入任何東西，沒有寫入。", "")
        return 1
    if not TOKEN_RE.match(token):
        say("", "  這看起來不像 bot token。",
            "  正確格式長這樣：1234567890:AA...（數字、冒號、約 35 個字元）",
            "  沒有寫入任何東西，可以重跑一次。", "")
        return 1

    say("", f"  正在驗證 token {tail(token)} ...")
    me = call(token, "getMe")
    if me.get("transport_error"):
        say("  ✗ 連不上 Telegram —— 請求根本沒有送到。",
            f"    {me.get('description')}",
            "",
            "  這 不是 token 的問題，先不要去撤銷。",
            "  常見原因：防毒軟體/公司網路攔截 HTTPS、沒有網路、或 DNS 被擋。",
            "",
            "  沒有寫入任何東西。", "")
        return 1
    if not me.get("ok"):
        say(f"  ✗ Telegram 收到了請求，但拒絕這組 token：{me.get('description')}",
            "",
            "  這 是 token 本身的問題（打錯，或已經被撤銷）。",
            "  去 @BotFather 拿新的一組，再跑一次這個檔案。",
            "",
            "  沒有寫入任何東西。", "")
        return 1
    bot = (me.get("result") or {}).get("username", "?")
    say(f"  ✓ token 正確，bot 是 @{bot}", "")

    chat_id, how = find_chat_id(token)
    if chat_id is None:
        say(f"  ✗ 還找不到你的 chat id（{how}）。", "",
            f"  請先在 Telegram 傳「任何一則訊息」給 @{bot}，",
            "  然後再跑一次這個檔案就會自動抓到。",
            "  （bot 要先收過你的訊息，才知道要發給誰）",
            "",
            "  沒有寫入任何東西。", "")
        return 2
    say(f"  ✓ 找到你的 chat id：{chat_id}  ({how})", "")

    say("", "  正在發送測試訊息 ...")
    res = call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "<b>Test</b>\nGold watcher setup confirmed. "
                "Signals will arrive here.",
        "parse_mode": "HTML"})
    if not res.get("ok"):
        say("  ✗ 發送失敗："
            + ("連不上 Telegram" if res.get("transport_error") else "Telegram 拒絕了")
            + f"：{res.get('description')}",
            "",
            "  沒有寫入任何東西 —— 一個「發不出去卻看起來正常」的設定，",
            "  比沒有設定更糟。", "")
        return 1
    say("  ✓ 已送出，去看你的手機。", "")

    write_env(token, chat_id, a.signals)
    say("", LINE, "  設定完成！", LINE, "",
        f"  檔案     : {ENV_FILE}",
        f"  bot      : @{bot}   token 尾碼 {tail(token)}",
        f"  chat id  : {chat_id}",
        f"  會通知的 : {a.signals}",
        "",
        "  （signal5 最頻繁，一天約 16 次。它限制同時最多 3 筆，",
        "    所以連續觸發 35 分鐘只會發 3 則，不是 7 則。",
        "    想關掉某個頻道，就從上面那個檔案的名單裡刪掉它。）",
        "",
        "  最後一步：重開 watch.bat。",
        "  它啟動時會印出 'notifications: Telegram' 就代表成功了。",
        "",
        "  之後想檢查，雙擊這個檔案時加參數，或直接跑：",
        "    3_設定手機通知.bat --status    看有沒有設定好",
        "    3_設定手機通知.bat --test      再發一則測試訊息", "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
