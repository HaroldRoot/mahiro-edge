' run-hidden.vbs - launch IconEnforcer.ps1 with NO visible window.
'
' Why this exists: powershell.exe is a console-subsystem program. When Windows
' Terminal is the user's default terminal app (Windows 11 default on many setups),
' the new console is handed off to a WindowsTerminal.exe window in a SEPARATE
' process. So the enforcer's own GetConsoleWindow+ShowWindow(SW_HIDE) cannot hide
' it -- the visible window belongs to WindowsTerminal.exe, not to powershell.exe,
' and -WindowStyle Hidden loses the race against the terminal handoff too.
'
' wscript.exe is NOT a console program, so launching powershell from it with
' window style 0 (hidden) creates no console and triggers no terminal handoff.
' This is launch-method independent and works whether the default terminal is
' Windows Terminal or classic conhost.
Option Explicit
Dim shell, base, cmd
Set shell = CreateObject("WScript.Shell")
base = shell.ExpandEnvironmentStrings("%ProgramData%") & "\MahiroEdge"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & base & "\IconEnforcer.ps1"""
' 0 = hidden window, False = do not wait (wscript exits immediately, leaving the
' enforcer running detached with no window).
shell.Run cmd, 0, False
