@echo off
chcp 65001 >nul
setlocal
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
if exist "D:\isaac\venv\Scripts\activate.bat" call "D:\isaac\venv\Scripts\activate.bat"
cd /d "%REPO%"
if "%1"=="" set TASK=Unitree-G1-29dof-Mimic-OursShort
if not "%1"=="" set TASK=%1
echo Smoke test: 64 envs x 3 iterations, only proves the pipeline works.
python scripts\rsl_rl\train.py --task %TASK% --headless --num_envs 64 --max_iterations 3
