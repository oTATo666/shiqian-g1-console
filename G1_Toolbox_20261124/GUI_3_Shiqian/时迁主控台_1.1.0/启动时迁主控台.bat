@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY=..\runtime\python.exe"
if not exist "%PY%" set "PY=python"

if exist "..\launcher.py" (
    "%PY%" "..\launcher.py"
) else (
    "%PY%" console_ui.py
)

echo.
pause
