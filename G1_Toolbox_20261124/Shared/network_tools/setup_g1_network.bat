@echo off
title Setup Ethernet for Unitree G1

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Setting wired adapter to 192.168.123.222/24 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Realtek PCIe GbE*'} | Select-Object -First 1; if (-not $a) { Write-Host '[ERROR] Wired adapter not found'; exit 1 }; Remove-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue; Set-NetIPInterface -InterfaceIndex $a.ifIndex -Dhcp Disabled -ErrorAction SilentlyContinue; New-NetIPAddress -InterfaceIndex $a.ifIndex -IPAddress 192.168.123.222 -PrefixLength 24 | Out-Null; Write-Host ('OK: ' + $a.Name + ' = 192.168.123.222/24')"

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to set IP. See message above.
    pause
    exit /b 1
)

echo.
echo Pinging G1 robot at 192.168.123.161 ...
ping -n 4 192.168.123.161

echo.
echo If replies appear above, the link to the robot is OK.
echo To revert this adapter back to automatic IP, run restore_ethernet_dhcp.bat
echo.
pause
