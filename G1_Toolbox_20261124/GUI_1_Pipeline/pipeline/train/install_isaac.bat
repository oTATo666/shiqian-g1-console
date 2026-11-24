@echo off
chcp 65001 >nul
setlocal
set PYTHONPATH=
set PYTHONSTARTUP=
set PYTHONNOUSERSITE=1
set ROOT=D:/isaac
set REPO=%~dp0..\..\G1_DevKit\04_rl\unitree_rl_lab
echo ============================================
echo  Isaac Sim / Isaac Lab installer  (D://isaac)
echo ============================================
if not exist "%ROOT%" mkdir "%ROOT%"
if "%ISAAC_PY%"=="" (
    if exist "C:/Users/33823/miniconda3/envs/g1/python.exe" (
        set ISAAC_PY=C:/Users/33823/miniconda3/envs/g1/python.exe
    ) else (
        set ISAAC_PY=python
    )
)
if not exist "%ROOT%\venv" (
    echo [1/5] creating venv with %ISAAC_PY%
    "%ISAAC_PY%" -m venv "%ROOT%\venv" || goto :err
)
call "%ROOT%\venv\Scripts\activate.bat"
python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
python -m pip install "setuptools<81" wheel -i https://pypi.tuna.tsinghua.edu.cn/simple
echo [2/5] upgrader done, installing isaacsim 4.5.0.0 ...
pip install "isaacsim[all]==4.5.0.0" -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.nvidia.com || goto :err
echo [3/5] installing isaaclab 2.1.0 ...
pip install --no-build-isolation "isaaclab==2.1.0" -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.nvidia.com || goto :err
echo [4/5] install unitree_rl_lab (editable) ...
pip install --no-build-isolation -e "%REPO%\source\unitree_rl_lab" || goto :err
echo [5/5] done
echo Activate with: "%ROOT%\venv\Scripts\activate.bat"
goto :eof
:err
echo.
echo [ERROR] a step failed, read the message above.
