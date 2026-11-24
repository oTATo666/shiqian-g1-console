@echo off
chcp 65001 >nul
title 「时迁」后台密钥管理
cd /d "%~dp0"
echo ============================================================
echo    「时迁」后台密钥管理
echo    所有内容都会加密进 data\.model_vault.enc，任何配置文件里都不会有明文
echo    注意：口令是源码里的硬编码常量，这里没有「改口令」这一项
echo ============================================================
echo.
echo   [1] 填写 / 更换云端大模型 API Key（推荐，输入时不显示）
echo   [2] 脱敏查看当前配置（需要二级底层总钥匙）
echo   [3] 测试本地 / 云端模型通道
echo   [4] 从环境变量 SHIQIAN_CLOUD_API_KEY 导入 Key
echo   [5] 口令常量自检（只读）
echo   [0] 退出
echo.
set /p choice=请输入编号并回车:
if "%choice%"=="1" call _python_launcher.bat tools\set_secret.py --cloud-key
if "%choice%"=="2" call _python_launcher.bat tools\set_secret.py --show
if "%choice%"=="3" call _python_launcher.bat tools\set_secret.py --test
if "%choice%"=="4" call _python_launcher.bat tools\set_secret.py --from-env
if "%choice%"=="5" call _python_launcher.bat -m modules.core_auth --check
echo.
pause
