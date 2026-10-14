' Hidden launcher for cloud_memory_tunnel.cmd.
'
' Task Scheduler runs this with wscript.exe so the tunnel has no console window.
' Runs the sibling .cmd with window mode 0 (hidden) and returns immediately
' (the .cmd itself is the long-lived reconnect loop, not this script).
'
' Keep this file ASCII-only: wscript reads .vbs in the system ANSI codepage.
Option Explicit

Dim shell, fso, here, target
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = here & "\cloud_memory_tunnel.cmd"

If Not fso.FileExists(target) Then
    WScript.Quit 3
End If

shell.Run """" & target & """", 0, False
