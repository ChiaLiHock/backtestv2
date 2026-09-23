@echo off
rem ---------------------------------------------------------------------------
rem  One analysis tick: has anything changed since the last check?
rem
rem  Double-click to run it by hand, or let the scheduled task call it every
rem  30 minutes. Either way it prints one of two things:
rem
rem    NO_CHANGE            nothing decision-relevant moved. One line was added
rem                         to reports\analysis\<today>.md. Nothing else to do.
rem
rem    CHANGED: <reasons>   followed by the full analysis snapshot, for a model
rem                         to read and write up.
rem
rem  ---------------------------------------------------------------------------
rem  WHY THIS FILE EXISTS rather than the scheduled task calling python directly:
rem
rem  `python -m backtest.tools.analysis_tick` only resolves when the CURRENT
rem  DIRECTORY is the parent of `backtest\`. The scheduled task starts in the
rem  project folder, which IS `backtest\`, so the module was not found and the
rem  run died with ModuleNotFoundError. Doing the `cd` here makes the command
rem  work from anywhere, and gives the permission allow-list a single stable
rem  string to match instead of a long python invocation.
rem
rem  Needs watch.bat running - it reads the dashboard's own API on port 8787.
rem  If the watcher is down that is recorded in the journal rather than being
rem  silently skipped, because a journal that looks quiet because nothing is
rem  running is the worst possible failure of a journal.
rem ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" (
    echo   Python venv not found at %PY%
    exit /b 1
)

"%PY%" -m backtest.tools.analysis_tick %*
