@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo ============================================================
echo   BaiAi-Tavern 制作安装包（单个 EXE：安装 + 卸载）
echo ============================================================
echo.

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/5] 检查主程序是否已打包 ...
if not exist "dist\BaiAi-Tavern.exe" (
    echo [错误] 没找到 dist\BaiAi-Tavern.exe，请先运行 scripts\build.bat
    pause
    exit /b 1
)
if not exist "dist\bot.exe" (
    echo [错误] 没找到 dist\bot.exe，请先运行 scripts\build.bat
    pause
    exit /b 1
)

echo [2/5] 生成版本信息 ...
"%PY%" scripts\make_version_info.py
if errorlevel 1 (
    echo [错误] 生成版本信息失败。
    pause
    exit /b 1
)

echo [3/5] 组装 payload（安装包内部携带的程序文件）...
if exist "build\payload" rmdir /s /q "build\payload"
mkdir "build\payload"
copy /y "dist\BaiAi-Tavern.exe" "build\payload\" >nul
copy /y "dist\bot.exe" "build\payload\" >nul
copy /y "config.example.yaml" "build\payload\" >nul
copy /y "启动.bat" "build\payload\" >nul
xcopy /e /i /y "resources" "build\payload\resources" >nul

echo [4/5] 打包安装程序（内部携带 payload，体积较大请耐心等待）...
rem 先删掉上一次的产物：被安全软件短暂占用时，PyInstaller 更新 PE 校验和会失败
if exist "dist\BaiAi-Tavern V0.1.exe" del /f /q "dist\BaiAi-Tavern V0.1.exe"
"%PY%" -m PyInstaller --noconfirm --clean pyinstaller_installer.spec
if errorlevel 1 (
    echo.
    echo [提示] 上一次打包失败。最常见原因是杀毒软件正在扫描刚生成的 exe，
    echo        导致 PyInstaller 无法更新 PE 校验和（PermissionError / 拒绝访问）。
    echo        这里等 5 秒后自动重试一次 ...
    ping -n 6 127.0.0.1 >nul
    if exist "dist\BaiAi-Tavern V0.1.exe" del /f /q "dist\BaiAi-Tavern V0.1.exe"
    "%PY%" -m PyInstaller --noconfirm pyinstaller_installer.spec
    if errorlevel 1 (
        echo [错误] 安装程序打包失败，请把杀毒软件对本项目目录的实时扫描关掉后重试。
        pause
        exit /b 1
    )
)

echo [5/5] 检查产物 ...
if not exist "dist\BaiAi-Tavern V0.1.exe" (
    echo [错误] 没找到 dist\BaiAi-Tavern V0.1.exe，请查看上面的打包日志。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   安装包：dist\BaiAi-Tavern V0.1.exe（单个文件，安装/卸载都在里面）
echo   绿色版：dist\BaiAi-Tavern\（可直接启动，无需安装）
echo.
echo   安装后：桌面图标 + 开始菜单 + 「Windows 设置 → 应用」里的卸载项；
echo   卸载默认保留用户数据（data / config.yaml）。
echo.
echo   自检：python -m tests.installer_smoke
echo ============================================================
pause
