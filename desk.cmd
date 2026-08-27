@echo off
setlocal
cd /d "%~dp0"
rem Prefer pythonw (no console window). The server keeps running after this window closes.
if exist ".venv\Scripts\pythonw.exe" (
  set "PYW=.venv\Scripts\pythonw.exe"
) else (
  set "PYW=pythonw"
)
start "RARF Desk" /b "" "%PYW%" -m rarf_summarizer.desk %*
