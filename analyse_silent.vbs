' ---------------------------------------------------------------------------
'  Run analyse.bat with NO console window.
'
'  Windows Task Scheduler is what keeps the journal honest: it fires every 30
'  minutes whether or not Claude is open, idle, or permitted, and appends one
'  heartbeat line to reports\analysis\<today>.md. That record is the part that
'  must never be missed, so it is the part that depends on nothing.
'
'  Running analyse.bat directly from the scheduler flashes a console window
'  every half hour. On a machine someone is watching a chart on, that is not a
'  small annoyance - it is a reason the task gets deleted. WScript.Shell.Run
'  with intWindowStyle 0 runs it hidden.
'
'  Arguments:
'    0     hidden window
'    False do not wait for it to finish - the scheduler should not hold a slot
'          open while python starts up
'
'  Registered by:
'    schtasks /create /tn "GoldAnalysisTick" /sc minute /mo 30 ^
'      /tr "wscript.exe //B \"C:\...\backtest\analyse_silent.vbs\"" /f
'
'  Remove with:
'    schtasks /delete /tn "GoldAnalysisTick" /f
' ---------------------------------------------------------------------------
Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
' Two calls, in order and deliberately:
'   1. the RECORDER - one heartbeat line, always, needs nothing but the watcher
'   2. the ANALYST  - only writes when something actually triggered, and needs
'                     the CLI to be logged in; exits 3 harmlessly if it is not
' Keeping them separate means a broken analyst never costs you the record.
shell.Run """" & here & "analyse.bat"" --record-only", 0, True
shell.Run """" & here & "analyse_ai.bat""", 0, False
