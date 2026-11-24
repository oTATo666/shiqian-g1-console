@echo off
chcp 65001 >nul
rem ============================================================================
rem  _python_launcher.bat —— 统一找 Python 的小启动器（v6）
rem
rem  【为什么需要它】
rem  换一台电脑最容易翻车的不是代码，而是"这台机器上到底哪个命令能叫出 Python、
rem  哪个 Python 里装了依赖"：
rem      · 官方安装器装的 Windows：有 py 启动器，不一定有 python
rem      · 精简版 / 商店版：python 可能是个假链接，一敲就弹应用商店
rem      · 装了两个 Python：依赖只装在其中某一个里
rem  所以这里按「有依赖 > 没依赖」的顺序挑解释器：
rem      1) py -3 并且能 import yaml   -> 用它
rem      2) python 并且能 import yaml  -> 用它
rem      3) 都没有依赖             -> 用能起来的那个（报错交给脚本自己提示）
rem
rem  【用法】给其它 .bat 内部调用，不用你自己敲：
rem      call _python_launcher.bat tools\demo.py --rounds 5
rem ============================================================================
setlocal

:trypy
where py >nul 2>nul
if errorlevel 1 goto :trypython
py -3 -c "import yaml" >nul 2>nul
if not errorlevel 1 goto :runpy

:trypython
where python >nul 2>nul
if errorlevel 1 goto :lastresort_py
python -c "import yaml" >nul 2>nul
if not errorlevel 1 goto :runpython

:lastresort_py
where py >nul 2>nul
if not errorlevel 1 goto :runpy
where python >nul 2>nul
if not errorlevel 1 goto :runpython

echo.
echo ❌ 这台电脑上找不到 Python。
echo    请先到 https://www.python.org/downloads/ 安装 Python 3.9 或更高版本，
echo    安装时务必勾选「Add Python to PATH」，装完重新双击本脚本即可。
echo.
exit /b 9009

:runpy
py -3 %*
exit /b %errorlevel%

:runpython
python %*
exit /b %errorlevel%
