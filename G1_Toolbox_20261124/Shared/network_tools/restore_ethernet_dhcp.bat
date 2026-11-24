@echo off
title Restore Ethernet to DHCP

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$a = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Realtek PCIe GbE*'} | Select-Object -First 1; if (-not $a) { Write-Host '[ERROR] Wired adapter not found'; exit 1 }; Remove-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue; Set-NetIPInterface -InterfaceIndex $a.ifIndex -Dhcp Enabled; Write-Host ('OK: ' + $a.Name + ' restored to DHCP (automatic IP)')"

if errorlevel 1 (
    echo [ERROR] Failed. See message above.
) else (
    ipconfig /renew >nul 2>&1
    echo Done. Adapter is back to automatic IP.
)
echo.
pause
