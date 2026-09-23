@echo off
rem ---------------------------------------------------------------------------
rem  Keep the rule running against live data and serve the page.
rem
rem  LIVE MODE - there is no backtest in this loop. Every cycle evaluates the
rem  ENTRY RULE on the newest CLOSED bar, reads the RiskExecutiveEngine risk
rem  vectors, and sends a signal when the rule fires and the risk gate allows it.
rem  Pass --backtest to get the old re-run-the-strategy behaviour back.
rem
rem  Double-click for gold + BTC + ETH. The RULE fires on gold and its
rem  signals are drawn on the gold chart, next to your own broker fills.
rem
rem  From a terminal:
rem      watch.bat configs\ut_mtf_ride_long.yaml
rem      watch.bat configs\ut_1h_long_v1.yaml XAUUSDT
rem      watch.bat configs\ut_1h_long_v1.yaml XAUUSDT,BTCUSDT 120 8788
rem
rem  Arguments: config, symbols, seconds between rule+page rebuilds, port.
rem  The chart and the risk read refresh every 5 seconds regardless - that is a
rem  separate and much cheaper loop. See the top of watch.py for why.
rem
rem  ---------------------------------------------------------------------------
rem  ONE WATCHER AT A TIME. They all write backtest\reports\, so a second one
rem  overwrites the first's page. If you want two, give the second its own
rem  directory with --report-dir.
rem
rem  RESTART THIS AFTER ANY CODE CHANGE. Python caches modules at import; the
rem  HTML template does not. A watcher left running across an edit serves a new
rem  page with old data and says so in a banner.
rem
rem  KEY LEVELS need a one-time cache build per symbol:
rem      .venv\Scripts\python.exe -m backtest.cli zones --symbols XAUUSDT
rem
rem  Leave this window open. Ctrl+C stops it.
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

set "CFG=%~1"
if "%CFG%"=="" set "CFG=backtest\configs\ut_1h_long_v1.yaml"

set "SYMS=%~2"
if "%SYMS%"=="" set "SYMS=XAUUSDT"

set "SECS=%~3"
if "%SECS%"=="" set "SECS=300"

set "PORT=%~4"
if "%PORT%"=="" set "PORT=8787"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    pause
    exit /b 1
)

echo.
echo   LIVE MODE - no backtest. The rule is evaluated on each closed bar.
echo   Bracket: +25 / -25 in price, fixed. NOT the OPERATING_PLAN 8 bracket.
echo.
echo   Config:   %CFG%
echo   Symbols:  %SYMS%
echo   Chart + risk every 5s - rule + page rebuild every %SECS%s
echo   Report:   http://127.0.0.1:%PORT%/
echo.
echo   Signals fire on GOLD and are drawn on the gold chart, so they sit
echo   next to your own broker fills and can be compared directly.
echo.
echo   Leave this window open. Ctrl+C to stop.
echo.

"%PY%" -m backtest.cli watch "%CFG%" --symbols %SYMS% --interval %SECS% --live-interval 5 --port %PORT% --open

echo.
pause
