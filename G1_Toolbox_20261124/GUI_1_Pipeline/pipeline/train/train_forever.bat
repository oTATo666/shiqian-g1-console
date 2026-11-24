@echo off
chcp 65001 >nul
setlocal
set JOB=%1
if "%JOB%"=="" set JOB=short_v1
cd /d "%~dp0..\.."
echo ============================================
echo   训练看门狗 (job = %JOB%)
echo   进程意外退出会自动从最近 checkpoint 续训
echo   关掉这个窗口 = 停止训练（已存的 checkpoint 保留）
echo ============================================
echo.
C:\Users\33823\miniconda3\envs\g1\python.exe -m pipeline.train.watchdog %JOB%
echo.
echo 训练进程已退出。按任意键关闭。
pause >nul
