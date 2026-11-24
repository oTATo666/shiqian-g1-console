@echo off
chcp 65001 >nul
setlocal
set NAME=%1
if "%NAME%"=="" set NAME=ours_walk
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
set TASK=%REPO%\source\unitree_rl_lab\unitree_rl_lab\tasks\mimic\robots\g1_29dof\%NAME%
if not exist "%TASK%" (echo task not found: %TASK% & exit /b 1)
call D:/isaac/venv/Scripts/activate.bat
cd /d "%REPO%"
python scripts\mimic\csv_to_npz.py -f "%TASK%\ours_60hz.csv" --input_fps 60 --output_fps 50
