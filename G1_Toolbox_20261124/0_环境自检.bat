@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PY=%ROOT%Runtime_Python310\python310\python.exe"
set "PATH=%ROOT%Runtime_Python310\python310;%ROOT%Runtime_Python310\python310\Library\bin;%ROOT%Runtime_Python310\python310\DLLs;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%Shared\unitree_sdk2_python"
set "G1_DEVKIT=%ROOT%Shared\G1_DevKit"
set "SHIQIAN_MOTION_DIR=%ROOT%GUI_3_Shiqian\时迁主控台_1.1.0\motions"
if not exist "%PY%" (
  echo.
  echo [错误] 找不到内置运行环境：
  echo        %PY%
  echo.
  echo 请确认压缩包是完整解压的；若运行环境被删了，
  echo 先双击 4_安装依赖_备用.bat 重建。
  echo.
  pause
  exit /b 1
)

title G1 · 环境自检
cd /d "%ROOT%"
"%PY%" "%ROOT%Env\check_env.py" %*
echo.
pause
