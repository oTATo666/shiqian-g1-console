@echo off
chcp 65001 >nul
setlocal
set NAME=%1
if "%NAME%"=="" set NAME=ours_walk
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
set OUT=%~dp0..\..\policy\%NAME%
if not exist "%OUT%" mkdir "%OUT%"
for /f "delims=" %%i in ('dir /b /s "%REPO%\logs\rsl_rl\*\exported\policies\*.pt" 2^>nul') do (
    echo copying %%i
    copy /y "%%i" "%OUT%\policy.pt" >nul
)
if exist "%OUT%\policy.pt" (echo exported to %OUT%\policy.pt) else (echo no exported policy found under logs\rsl_rl)
