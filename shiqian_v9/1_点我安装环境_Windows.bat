@echo off
chcp 65001 >nul
echo ========================================================
echo   正在为你的电脑自动安装「时迁」系统所需的全部运行库...
echo   （请保持电脑联网，大约需要 1-2 分钟，请耐心等待）
echo ========================================================
call "%~dp0_python_launcher.bat" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
echo ✅ 安装完成！现在你可以直接双击【2_点我启动系统_Windows.bat】启动系统了！
pause
