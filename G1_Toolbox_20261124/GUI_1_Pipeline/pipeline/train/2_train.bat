@echo off
chcp 65001 >nul
setlocal
set NAME=%1
if "%NAME%"=="" set NAME=ours_walk
set ENVS=%2
if "%ENVS%"=="" set ENVS=2048
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
call D:/isaac/venv/Scripts/activate.bat
cd /d "%REPO%"
python scripts\rsl_rl\train.py --task Unitree-G1-29dof-Mimic-OursWalk --headless --num_envs %ENVS%
