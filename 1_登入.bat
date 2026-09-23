@echo off
rem ---------------------------------------------------------------------------
rem  ASCII ONLY. Do not put non-ASCII text in this file.
rem
rem  cmd.exe parses a .bat byte-by-byte in the system ANSI codepage. A UTF-8
rem  multi-byte sequence splits the `echo` keyword itself, and the file runs as a
rem  stream of nonsense commands. `chcp 65001` does not help - it changes how
rem  output is rendered, not how the file is parsed.
rem
rem  So this is a launcher and nothing else. Every character a person reads comes
rem  from tools/cli_login.py, where UTF-8 works.
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title Claude CLI login - one time only

cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    pause
    exit /b 1
)

"%PY%" -m backtest.tools.cli_login

echo.
pause
