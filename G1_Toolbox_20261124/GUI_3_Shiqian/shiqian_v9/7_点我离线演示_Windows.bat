@echo off
chcp 65001 >nul
title 「时迁」一键离线演示
cd /d "%~dp0"
echo ============================================================
echo    「时迁」一键离线演示
echo    不用麦克风 / 不用机器人 / 不用 API Key / 断网也能跑
echo ============================================================
echo.
call _python_launcher.bat tools\demo.py %*
echo.
echo （演示结束。上面每一步都是真跑的，不是示例数据）
pause
