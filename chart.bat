@echo off
rem ---------------------------------------------------------------------------
rem  Sync the latest candles, render a static chart, and open it.
rem
rem  This is the cross-check tool: it prints the legend values so you can compare
rem  them against the phone app. For an interactive chart use watch.bat.
rem
rem  Double-click for a 30m gold chart, or from a terminal:
rem      chart.bat 1h
rem      chart.bat 15m 150
rem      chart.bat 30m 90 UTC
rem      chart.bat 1h 120 MYT BTCUSDT
rem
rem  Arguments: timeframe, bars, timezone, symbol.
rem ---------------------------------------------------------------------------
setlocal

rem The CLI is a package under this folder's parent, so run from there.
cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

set "TF=%~1"
if "%TF%"=="" set "TF=30m"

set "BARS=%~2"
if "%BARS%"=="" set "BARS=90"

set "TZ=%~3"
if "%TZ%"=="" set "TZ=MYT"

set "SYM=%~4"
if "%SYM%"=="" set "SYM=XAUUSDT"

if not exist "%PY%" (
    echo.
    echo   Python venv not found at:
    echo     %PY%
    echo.
    echo   Create it with:
    echo     python -m venv "%~dp0.venv"
    echo     "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0charts" mkdir "%~dp0charts"
set "OUT=%~dp0charts\chart_%SYM%_%TF%.png"

echo.
echo   Syncing latest %SYM% %TF% candles from Bybit...
"%PY%" -m backtest.cli sync --symbol %SYM% --tf %TF% --from 2026-03-09 --to now 1>nul 2>&1
if errorlevel 1 (
    echo   [warning] sync failed - drawing from whatever is already stored.
)

echo   Rendering %BARS% bars in %TZ%...
echo.
"%PY%" -m backtest.tools.plot_chart --symbol %SYM% --tf %TF% --bars %BARS% --tz %TZ% --out "%OUT%"
if errorlevel 1 (
    echo.
    echo   Rendering failed.
    pause
    exit /b 1
)

echo.
echo   Opening %OUT%
start "" "%OUT%"

rem Keep the window up so the legend numbers above stay readable for comparison.
echo.
pause
