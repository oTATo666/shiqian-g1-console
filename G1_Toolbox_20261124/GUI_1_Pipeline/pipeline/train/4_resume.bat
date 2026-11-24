@echo off
chcp 65001 >nul
setlocal
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
if exist "D:\isaac\venv\Scripts\activate.bat" call "D:\isaac\venv\Scripts\activate.bat"
cd /d "%REPO%"
if "%1"=="" set TASK=Unitree-G1-29dof-Mimic-OursShort
if not "%1"=="" set TASK=%1
if "%2"=="" set ENVS=1024
if not "%2"=="" set ENVS=%2
echo Resuming latest run of %TASK% with %ENVS% envs ...
python scripts\rsl_rl\train.py --task %TASK% --headless --num_envs %ENVS% --resume --load_run -1 --checkpoint -1
