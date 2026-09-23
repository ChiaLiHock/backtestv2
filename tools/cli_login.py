"""One-time sign-in for the bundled Claude Code CLI, with all UI text in Python.

    python -m backtest.tools.cli_login
    python -m backtest.tools.cli_login --status

## Why this is a Python module and not just a .bat

The first version put the instructions directly in a `.bat` as `echo` lines.
`cmd.exe` parses a batch file byte-by-byte in the system ANSI codepage, so the
multi-byte UTF-8 sequences for Chinese characters split the `echo` keyword
itself — the file executed as a stream of nonsense commands
(`'次完整分析' is not recognized as an internal or external command`).
`chcp 65001` does not fix it: it changes how output is *rendered*, not how the
file is *parsed*.

So the `.bat` is now a pure-ASCII three-line launcher and every character a
person reads comes from here, where UTF-8 works.

## What it does

Finds the newest bundled `claude.exe`, reports whether it is already signed in,
and if not, hands over to `claude auth login` — an OAuth browser flow that only
the account owner can complete. It cannot be automated and should not be.

The CLI keeps its own credential store (`~/.claude`), separate from the desktop
app's, which lives inside an MSIX container the CLI cannot read. Being signed in
to the app is therefore not enough.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .analysis_ai import find_cli, logged_in

LINE = "=" * 68


def say(*parts: str) -> None:
    print(*parts, sep="\n", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backtest.tools.cli_login")
    ap.add_argument("--status", action="store_true", help="report and exit")
    a = ap.parse_args(argv)

    cli = find_cli()
    if cli is None:
        say("", LINE, "  找不到 Claude CLI", LINE, "",
            "  探測實際看到的東西：")
        find_cli(diagnose=True)
        say("",
            "  預期位置：%APPDATA%\\Claude\\claude-code\\<版本>\\claude.exe",
            "",
            "  修法：把 claude.exe 的完整路徑設進環境變數 CLAUDE_CLI_PATH，",
            "  然後開一個新視窗再跑一次。", "")
        return 2

    ok, detail = logged_in(cli)

    if a.status:
        say("", f"  CLI：{cli}", f"  已登入：{'是' if ok else '否'}", "")
        return 0 if ok else 1

    if ok:
        say("", LINE, "  已經登入過了，不用再做。", LINE, "",
            f"  CLI：{cli}", "",
            "  下一步：雙擊 2_測試分析.bat，跑一次確認整條路通了。", "")
        return 0

    say("", LINE, "  Claude CLI 登入（只需要做這一次）", LINE, "",
        f"  找到 CLI：{cli}", "",
        "  為什麼要另外登入一次：",
        "  CLI 的憑證存在 C:\\Users\\User\\.claude，跟 Claude 桌面版分開。",
        "  桌面版的憑證在它自己的容器裡，CLI 讀不到 —— 所以桌面版已經登入",
        "  並不算數。",
        "",
        "  接下來會打開瀏覽器要你登入 Anthropic 帳號。",
        "  登入完回到這個視窗，它會自己確認。",
        "", "-" * 68, "")

    try:
        # Inherit stdio: the login flow prints a URL and waits. Capturing it
        # would hide the one thing the person needs to see.
        subprocess.run([str(cli), "auth", "login"], check=False)
    except Exception as exc:
        say("", f"  啟動登入失敗：{type(exc).__name__}: {exc}", "")
        return 1

    say("", "-" * 68, "")
    ok, detail = logged_in(cli)
    if ok:
        say(LINE, "  登入成功！", LINE, "",
            "  下一步：雙擊 2_測試分析.bat，跑一次確認整條路通了。", "")
        return 0

    say("  還沒成功 —— 登入沒有完成。", "",
        "  可以再跑一次這個檔案。如果一直不行，把畫面截圖給我看。", "",
        f"  狀態回報：{detail}", "")
    return 1


if __name__ == "__main__":
    sys.exit(main())
