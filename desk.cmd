@echo off
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m rarf_summarizer.desk %*
if errorlevel 1 (
  echo.
  echo RARF Desk exited with an error. See messages above.
  pause
)
