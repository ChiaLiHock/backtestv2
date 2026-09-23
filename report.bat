@echo off
rem ---------------------------------------------------------------------------
rem  Run a strategy config, then open the interactive report.
rem
rem  Double-click for gold + BTC + ETH in one page with a symbol switcher.
rem
rem  From a terminal:
rem      report.bat configs\ut_1h_long_atr.yaml
rem      report.bat configs\ut_1h_long_v1.yaml XAUUSDT
rem      report.bat configs\ut_1h_long_v1.yaml XAUUSDT,BTCUSDT 5m,15m,1h
rem
rem  Arguments: config, symbols, timeframes.
rem
rem  This report is a STATIC file - it does not update. For a live chart use
rem  watch.bat instead.
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

set "CFG=%~1"
if "%CFG%"=="" set "CFG=backtest\configs\ut_1h_long_v1.yaml"

set "SYMS=%~2"
if "%SYMS%"=="" set "SYMS=XAUUSDT,BTCUSDT,ETHUSDT"

set "TFS=%~3"
if "%TFS%"=="" set "TFS=5m,15m,30m,1h,4h"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    pause
    exit /b 1
)

echo.
echo   Running %CFG% on %SYMS% ...
echo.
"%PY%" -m backtest.cli backtest "%CFG%" --symbols %SYMS%
if errorlevel 1 (
    echo.
    echo   Backtest failed.
    pause
    exit /b 1
)

echo.
echo   Building report ...
"%PY%" -m backtest.cli report --symbols %SYMS% --tf %TFS% --open
if errorlevel 1 (
    echo.
    echo   Report failed.
    pause
    exit /b 1
)

echo.
pause
