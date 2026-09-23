@echo off
rem ---------------------------------------------------------------------------
rem  Re-mine SIGNAL 4 against the last five trading days.
rem
rem  Signal 4 is the only signal meant to be refitted on a schedule. The others
rem  were fitted once and their thresholds live in Python; this one keeps its
rem  pattern in configs\signal4.json, and the running watcher notices that file
rem  change and reloads it on the next cycle. So this needs no restart.
rem
rem  Run by Task Scheduler weekly (see refit_signal4_silent.vbs), or by hand:
rem      refit_signal4.bat
rem      refit_signal4.bat --dry-run                  look, write nothing
rem      refit_signal4.bat --min-win 0.80             loosen the bar
rem      refit_signal4.bat --days 10                  fit on ten days instead
rem
rem  IF NOTHING MEETS THE BAR IT WRITES NOTHING and leaves last week's config in
rem  place, exiting 2. That is deliberate: a weekly job forced to always produce
rem  an answer produces a worse one every week the data has no pattern in it.
rem
rem  Output is appended to reports\signal4_refit.log so a scheduled run that
rem  found nothing can still be read back afterwards.
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "LOG=%~dp0reports\signal4_refit.log"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    exit /b 1
)
if not exist "%~dp0reports" mkdir "%~dp0reports"

echo. >> "%LOG%"
echo ==================================================================== >> "%LOG%"
echo   signal4 refit  %DATE% %TIME% >> "%LOG%"
echo ==================================================================== >> "%LOG%"

rem A dry run also exits 0, so the exit code alone cannot tell "wrote a new
rem config" from "showed what it would have written". Saying the wrong one in a
rem log that is read weeks later is worse than saying nothing.
set "DRY="
echo %* | find /i "--dry-run" >nul && set "DRY=1"

"%PY%" -W ignore -m backtest.tools.mine_signal4 %* >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if "%RC%"=="0" if defined DRY echo   RESULT: dry run - NOTHING written >> "%LOG%"
if "%RC%"=="0" if not defined DRY echo   RESULT: config updated >> "%LOG%"
if "%RC%"=="2" echo   RESULT: nothing met the bar - previous config kept >> "%LOG%"
if "%RC%"=="1" echo   RESULT: FAILED >> "%LOG%"

exit /b %RC%
