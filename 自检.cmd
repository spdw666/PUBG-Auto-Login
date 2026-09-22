@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 运行内置自检（导入 1200 行测试 CSV、密码加解密回读、明文不入库、封禁结果写入、分页、API Key 加解密）...
echo.
py -3.14 steam_ban_manager.py --self-test
if errorlevel 1 ( python steam_ban_manager.py --self-test )
echo.
echo 退出码 %errorlevel%（0 = 通过）
pause
