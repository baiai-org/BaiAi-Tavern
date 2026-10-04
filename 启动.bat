@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   BaiAi-Tavern 一键启动
echo ============================================================
echo.

rem 依次查找主程序：脚本所在目录 -> dist\BaiAi-Tavern -> dist
set "APPDIR="
if exist "%~dp0BaiAi-Tavern.exe" set "APPDIR=%~dp0"
if not defined APPDIR if exist "%~dp0dist\BaiAi-Tavern\BaiAi-Tavern.exe" set "APPDIR=%~dp0dist\BaiAi-Tavern\"
if not defined APPDIR if exist "%~dp0dist\BaiAi-Tavern.exe" set "APPDIR=%~dp0dist\"

if not defined APPDIR (
    echo [提示] 未找到 BaiAi-Tavern.exe，改用源码方式启动（需要先安装依赖）。
    if exist "%~dp0.venv\Scripts\pythonw.exe" (
        start "" "%~dp0.venv\Scripts\pythonw.exe" -m app.main
    ) else (
        start "" python -m app.main
    )
    goto :tips
)

echo 正在启动 BaiAi-Tavern ...
start "" "%APPDIR%BaiAi-Tavern.exe"

:tips
echo.
echo 提示：
echo   1) 首次运行会弹出「配置引导」，填好 LLM 接口与 QQ 官方机器人凭据即可；
echo   2) QQ 官方机器人需要在 https://q.qq.com 创建应用，拿到 AppID / AppSecret；
echo   3) 程序会最小化到系统托盘，双击托盘图标可重新打开窗口。
echo.
pause
