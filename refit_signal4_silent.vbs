' ---------------------------------------------------------------------------
'  Run refit_signal4.bat with NO console window.
'
'  Same reasoning as analyse_silent.vbs: a console flashing open on a machine
'  someone is watching a chart on is a reason the scheduled task gets deleted.
'  The refit takes a couple of minutes (it labels every bar of the window by
'  walking 1m data), which is long enough for a visible window to be genuinely
'  in the way.
'
'  Waits for completion (True) rather than firing and forgetting, because the
'  exit code is meaningful here -- 0 wrote a new config, 2 means nothing met the
'  bar and last week's config was deliberately kept -- and the scheduler records
'  it as the task's last result.
'
'  Registered by:
'    schtasks /create /tn "GoldSignal4Refit" /sc weekly /d SUN /st 09:00 ^
'      /tr "wscript.exe //B \"C:\...\backtest\refit_signal4_silent.vbs\"" /f
'
'  Remove with:
'    schtasks /delete /tn "GoldSignal4Refit" /f
'
'  Read what it did:
'    backtest\reports\signal4_refit.log
' ---------------------------------------------------------------------------
Dim shell, here, rc
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
rc = shell.Run("""" & here & "refit_signal4.bat""", 0, True)
WScript.Quit rc
