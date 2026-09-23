@echo off
rem ---------------------------------------------------------------------------
rem  ASCII ONLY. See the note at the top of the login .bat - non-ASCII in a .bat
rem  is parsed in the ANSI codepage and breaks the `echo` keyword itself.
rem
rem  --force so it analyses whatever the current state is rather than waiting for
rem  a trigger. A test that only works when the market happens to move is not a
rem  test you can run on demand.
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title Test one full analysis

cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    pause
    exit /b 1
)

echo.
echo   Running one forced analysis. Needs watch.bat to be running.
echo   First run may take 1-2 minutes.
echo.
echo   ------------------------------------------------------------------
echo.

"%PY%" -m backtest.tools.analysis_ai --force

echo.
echo   ------------------------------------------------------------------
echo.
echo   Read the last line above:
echo.
echo     WROTE ... chars     OK - the whole chain works, runs every 30 min
echo     NOT_LOGGED_IN       run the login .bat first (file 1)
echo     WATCHER_DOWN        run watch.bat first
echo     FAILED / TIMEOUT    screenshot it for me
echo.
echo   Journal: backtest\reports\analysis\
echo.
pause
