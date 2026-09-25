' ============================================================
'  MCSR-Launcher - silent launcher (no cmd window at all).
'  Target of the desktop shortcut; also double-clickable.
'
'  ASCII only: Windows Script Host reads .vbs as ANSI unless the
'  file is UTF-16 with a BOM, so non-ASCII text here would be
'  mis-decoded the same way a UTF-8 .cmd is.
' ============================================================
Option Explicit

Dim fso, shell, here, pythonw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = here & "\.venv\Scripts\pythonw.exe"
cmd = here & "\run-console.cmd"

If Not fso.FileExists(pythonw) Then
    MsgBox "MCSR-Launcher: .venv not found." & vbCrLf & vbCrLf & _
           "Folder: " & here & vbCrLf & vbCrLf & _
           "Run these two commands once in that folder:" & vbCrLf & _
           "    python -m venv .venv" & vbCrLf & _
           "    .venv\Scripts\python -m pip install -r requirements.txt", _
           16, "MCSR-Launcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = here
' 0 = hidden window, False = do not wait for it to finish
shell.Run """" & pythonw & """ -m mcsl", 0, False
