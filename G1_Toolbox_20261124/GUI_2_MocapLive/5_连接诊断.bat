@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0.."
set "PY=%ROOT%\Runtime_Python310\python310\python.exe"
set "PATH=%ROOT%\Runtime_Python310\python310;%ROOT%\Runtime_Python310\python310\Library\bin;%ROOT%\Runtime_Python310\python310\DLLs;%PATH%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%\Shared\unitree_sdk2_python"
set "G1_DEVKIT=%ROOT%\Shared\G1_DevKit"
if not exist "%PY%" (
  echo [错误] 找不到包内运行环境：%PY%
  echo        请完整解压，或先跑根目录 4_安装依赖_备用.bat
  pause
  exit /b 1
)
cd /d "%~dp0"

title MocapLive · 连接诊断
echo ============================================================
echo  SlimeVR 连接诊断（15 秒）
echo  戴好动捕，在 SlimeVR 里站直、动一动，这里会给结论
echo ============================================================
echo.
"%PY%" tools\vmc_probe.py --port 39539 --seconds 15
echo.
pause
