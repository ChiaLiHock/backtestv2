@echo off
rem ---------------------------------------------------------------------------
rem  Route B: run the tick, and if it TRIGGERED, have the bundled Claude Code CLI
rem  write the analysis into today's journal.
rem
rem  Double-click to run it by hand, or let Windows Task Scheduler call it.
rem  Either way it needs nothing from the Claude desktop app - not open, not
rem  idle, no permission prompt. That is the whole point of route B: the in-app
rem  scheduler fired 0 of 12 times overnight while a Windows task fired 12 of 12.
rem
rem  It costs no API credits. The bundled CLI signs in against the same
rem  subscription the desktop app uses.
rem
rem  ---------------------------------------------------------------------------
rem  ONE-TIME SETUP, and only you can do it:
rem
rem      "%APPDATA%\Claude\claude-code\2.1.237\claude.exe" auth login
rem
rem  The CLI keeps its own credential store, separate from the desktop app's
rem  (the app's lives inside its MSIX container, where the CLI cannot read it).
rem  It is an OAuth browser flow, so it has to be done by hand, once. Until then
rem  this script stops with NOT_LOGGED_IN rather than failing obscurely at 3am.
rem
rem  ---------------------------------------------------------------------------
rem  Exit codes, so a scheduled run is diagnosable after the fact:
rem      0  wrote an entry, or there was no trigger, or the watcher is down
rem      1  the model was called and failed or timed out (heading still written)
rem      2  no bundled claude.exe found
rem      3  not logged in
rem
rem  Needs watch.bat running - it reads the dashboard API on port 8787.
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    exit /b 1
)

"%PY%" -m backtest.tools.analysis_ai %*
