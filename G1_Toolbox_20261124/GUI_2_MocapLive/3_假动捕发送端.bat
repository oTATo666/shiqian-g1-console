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

title MocapLive · 假动捕发送端
echo 假 SlimeVR：按 VMC 协议往本机发骨骼，用来在没硬件时验证界面/链路
echo 用法：先跑这个，再启动 1_启动界面.bat（源选 SlimeVR，端口 39539）
"%PY%" tools\fake_vmc_sender.py --port 39539 --motion walk %*
pause
