@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 「时迁」网页核销后台 (http://127.0.0.1:8090)
echo ============================================================
echo    正在启动「时迁」网页核销后台，请稍候...
echo    启动后请用浏览器打开： http://127.0.0.1:8090
echo    关闭本窗口即可停止服务（Ctrl+C 也可以）
echo ============================================================
echo.
call _python_launcher.bat web_redeem_server.py
echo.
echo [提示] 服务已停止，按任意键关闭窗口。
pause >nul
