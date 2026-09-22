@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  PUBG自动登陆系统 - 编译成 exe
echo ============================================
echo.

set PY=
py -3.14 -c "import sys" >nul 2>nul && set PY=py -3.14
if "%PY%"=="" ( python -c "import sys" >nul 2>nul && set PY=python )
if "%PY%"=="" (
    echo [错误] 没有找到 Python，请先安装 Python 3.14：https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY% -c "import sys; print('使用解释器:', sys.version)"
echo.
echo [1/3] 安装/更新打包依赖 ...
%PY% -m pip install --upgrade pyinstaller ttkbootstrap pillow
if errorlevel 1 ( echo [错误] 依赖安装失败 & pause & exit /b 1 )

echo.
echo [2/3] 打包 ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name "PUBG自动登陆系统" steam_ban_manager.py
if errorlevel 1 ( echo [错误] 打包失败 & pause & exit /b 1 )

echo.
echo [3/3] 完成！exe 在 dist\ 目录里：
dir /b dist\*.exe
echo.
echo 提示：steam_login.py 必须和 steam_ban_manager.py 放在同一个文件夹里一起打包。
pause
