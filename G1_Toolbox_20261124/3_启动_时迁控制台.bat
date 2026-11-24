@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
set "CONSOLE_PY=%ROOT%GUI_3_Shiqian\runtime\python.exe"
set "BRIDGE_PY=%ROOT%Runtime_Python310\python310\python.exe"

if not exist "%CONSOLE_PY%" (
  echo.
  echo [错误] 找不到时迁自带 runtime：
  echo        %CONSOLE_PY%
  echo 请完整解压，或进入 GUI_3_Shiqian 用原版 ①双击启动.bat。
  echo.
  pause
  exit /b 1
)

rem 三个桥（传感/动作/视觉）用包内 python310；没有就让时迁自己找 conda
if exist "%BRIDGE_PY%" (
  set "SHIQIAN_SENSOR_PYTHON=%BRIDGE_PY%"
) else (
  echo [提醒] 没找到 运行环境\python310，机器人桥会退回本机 conda。
)
set "SHIQIAN_MOTION_DIR=%ROOT%GUI_3_Shiqian\时迁主控台_1.1.0\motions"
set "PYTHONPATH="
set "PYTHONHOME="
title G1 · 时迁控制台（含机器人桥）
cd /d "%ROOT%GUI_3_Shiqian"
"%CONSOLE_PY%" launcher.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [退出码 %RC%] 主控台异常退出，请把上面的信息截图。
  pause
)
