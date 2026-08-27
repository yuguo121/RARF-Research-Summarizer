@echo off
rem Stop the background RARF Desk started by desk.cmd
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*rarf_summarizer.desk*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo RARF Desk stopped.
pause
