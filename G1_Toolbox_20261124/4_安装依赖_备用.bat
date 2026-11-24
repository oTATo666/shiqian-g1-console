@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PY=%ROOT%Runtime_Python310\python310\python.exe"
set "PATH=%ROOT%Runtime_Python310\python310;%ROOT%Runtime_Python310\python310\Library\bin;%PATH%"

if not exist "%PY%" (
  echo [提示] 包内运行环境不存在，改用系统 Python。
  set "PY=python"
)

title G1 · 安装依赖（备用）
"%PY%" "%ROOT%环境\pip_install.py" --all %*
echo.
pause
