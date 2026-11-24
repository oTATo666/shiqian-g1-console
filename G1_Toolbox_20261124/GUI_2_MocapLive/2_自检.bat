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

title MocapLive · 自检
echo ============================================================
echo 自检 1/3：合成动作直连（验 GMR / 关节顺序 / 方向 / 实时性）
echo ============================================================
"%PY%" tools\selftest.py

echo.
echo ============================================================
echo 自检 2/3：VMC(UDP) 全量骨骼
echo ============================================================
"%PY%" tools\selftest.py --vmc

echo.
echo ============================================================
echo 自检 3/3：VMC(UDP) 只给 5 点（实际配置）
echo ============================================================
"%PY%" tools\selftest.py --vmc --points 5

echo.
pause
