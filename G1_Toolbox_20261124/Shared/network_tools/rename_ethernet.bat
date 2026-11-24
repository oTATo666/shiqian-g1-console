@echo off
title Rename Ethernet adapter for Unitree SDK

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$a = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Realtek PCIe GbE*'} | Select-Object -First 1; if (-not $a) { Write-Host '[ERROR] Wired adapter not found'; exit 1 }; if ($a.Name -eq 'Ethernet') { Write-Host 'Already named Ethernet. Nothing to do.' } else { Rename-NetAdapter -Name $a.Name -NewName 'Ethernet'; Write-Host ('OK: renamed [' + $a.Name + '] -> [Ethernet]') }"

if errorlevel 1 (
    echo [ERROR] Rename failed. See message above.
) else (
    echo.
    echo The static IP 192.168.123.222 is kept automatically.
    echo You can now run official examples, e.g.:
    echo     python unitree_sdk2_python\example\g1\high_level\g1_loco_client_example.py Ethernet
)
echo.
pause
