@echo off
title G1 SDK Console

call "C:\Users\33823\miniconda3\Scripts\activate.bat" g1
cd /d D:\DATAWORK\G1

python -c "import unitree_sdk2py" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] conda env "g1" not activated, or unitree_sdk2py missing.
    echo         Run: C:\Users\33823\miniconda3\Scripts\activate.bat g1
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   Unitree G1 SDK   ^|   conda env: g1   ^|   D:\DATAWORK\G1
echo ============================================================
echo.

python scripts\check_env.py

echo.
echo ------------------------------------------------------------
echo   Adapter tip: use your wired NIC name, e.g. "Ethernet"
echo   Example: python unitree_sdk2_python\example\g1\loco\g1_loco_client_example.py "Ethernet"
echo   Examples folder: unitree_sdk2_python\example\g1\
echo ------------------------------------------------------------
echo.

cmd /k
