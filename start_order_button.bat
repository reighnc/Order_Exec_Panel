@echo off
setlocal

REM Run from this script's directory
cd /d "%~dp0"

REM Prefer project virtualenv python if available
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "app_ui.py"
) else (
    python "app_ui.py"
)

endlocal
