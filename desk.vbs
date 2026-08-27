' RARF Desk launcher — fully invisible (no console window, not even a flash).
' Double-click this file; the browser opens automatically after ~10 seconds.
' The server keeps running in the background; stop it from the web UI (shutdown)
' or via:  curl -X POST http://127.0.0.1:8765/api/shutdown
Dim fso, shell, root, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pythonw"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run """" & pyw & """ -m rarf_summarizer.desk", 0, False
