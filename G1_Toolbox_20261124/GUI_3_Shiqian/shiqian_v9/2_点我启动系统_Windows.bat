@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo    「时迁」主控程序启动
echo    提示：没有麦克风也能用（会自动切到键盘输入）
echo          想强制键盘输入：在本文件末尾加 --keyboard 后双击
echo ============================================================
echo [第一步] 正在后台启动 G1 光影动作模拟接收器...
start "G1_Action_Receiver" cmd /k "chcp 65001 >nul && call _python_launcher.bat mock_robot_server.py"
timeout /t 2 >nul
echo [第二步] 正在启动「时迁」主控程序...
echo.
call _python_launcher.bat main.py %*
pause
