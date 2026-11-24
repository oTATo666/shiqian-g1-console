@echo off
chcp 65001 >nul
title G1 - Connect Robot Network
set "ARGS=%*"
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    if defined ARGS (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%ARGS%' -Verb RunAs"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    )
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0connect_g1.ps1" %*
