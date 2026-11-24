@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "runtime\python.exe" (
    echo [ERROR] runtime\python.exe not found.
    echo.
    echo Please extract the WHOLE zip package first ^(keep the runtime folder^),
    echo then double-click this file again.
    echo Details are in 00_*.txt
    echo.
    pause
    exit /b 1
)

"runtime\python.exe" launcher.py
if errorlevel 1 pause
