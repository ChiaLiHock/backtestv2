@echo off
rem ---------------------------------------------------------------------------
rem  ASCII ONLY. Do not put non-ASCII text in this file.
rem
rem  cmd.exe parses a .bat byte-by-byte in the system ANSI codepage. A UTF-8
rem  multi-byte sequence splits the `echo` keyword itself, and the file runs as a
rem  stream of nonsense commands. `chcp 65001` does not help - it changes how
rem  output is rendered, not how the file is parsed. The Chinese in the FILENAME
rem  is fine; the file's CONTENTS are what break.
rem
rem  So this is a launcher and nothing else. Every character a person reads comes
rem  from tools/setup_telegram.py, where UTF-8 works.
rem
rem  Double-click to set up Telegram notifications. Runs once.
rem
rem  Afterwards, drag this file onto a cmd window and append:
rem      --status    is it configured
rem      --test      send a test message
rem
rem  (The filename itself is Chinese and that is fine -- Windows stores it as
rem  UTF-16. Only the BYTES INSIDE a .bat have to stay ASCII, which is why this
rem  comment does not repeat the filename.)
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title Telegram notifications - setup

cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    pause
    exit /b 1
)

"%PY%" -m backtest.tools.setup_telegram %*

echo.
pause
