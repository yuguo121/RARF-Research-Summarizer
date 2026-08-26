@echo off
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  ".venv\Scripts\python.exe" -m rarf_summarizer.desk %*
) else (
  python -m rarf_summarizer.desk %*
)
