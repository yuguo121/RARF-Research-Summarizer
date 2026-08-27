@echo off
setlocal
cd /d "%~dp0"
rem Prefer pythonw (no console window). The server keeps running in the background.
if exist ".venv\Scripts\pythonw.exe" (
  set "PYW=%CD%\.venv\Scripts\pythonw.exe"
) else (
  set "PYW=pythonw"
)
start "RARF Desk" /b "%PYW%" -m rarf_summarizer.desk %*
