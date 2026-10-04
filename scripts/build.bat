@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo ============================================================
echo   BaiAi-Tavern 一键打包（PyInstaller）
echo ============================================================
echo.

rem 优先使用项目自带的虚拟环境，其次使用系统 Python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到可用的 Python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.9 及以上（推荐 3.10+）。
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('"%PY%" -V 2^>^&1') do set "PYVER=%%v"
echo [1/6] 使用解释器：%PY%（版本 %PYVER%）

echo.
echo [2/6] 安装 / 检查依赖 ...
"%PY%" -m pip install --upgrade pip >nul 2>nul
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败。
    pause
    exit /b 1
)

echo.
echo [3/6] 生成图标与版本信息 ...
"%PY%" scripts\make_icons.py
"%PY%" scripts\make_version_info.py

echo.
echo [4/6] 打包 GUI（BaiAi-Tavern.exe）...
"%PY%" -m PyInstaller --noconfirm --clean pyinstaller.spec
if errorlevel 1 (
    echo [错误] GUI 打包失败。
    pause
    exit /b 1
)

echo.
echo [5/6] 打包 Bot 进程（bot.exe）...
"%PY%" -m PyInstaller --noconfirm --clean pyinstaller_bot.spec
if errorlevel 1 (
    echo [错误] Bot 打包失败。
    pause
    exit /b 1
)

echo.
echo [6/6] 组装发布目录 dist\BaiAi-Tavern ...
set "OUTDIR=dist\BaiAi-Tavern"
rem 只覆盖程序文件，**保留用户数据**（data\、config.yaml），
rem 否则重新打包会把用户的配置和聊天记录一起删掉。
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
copy /y dist\BaiAi-Tavern.exe "%OUTDIR%\" >nul
copy /y dist\bot.exe "%OUTDIR%\" >nul
if not exist "%OUTDIR%\config.example.yaml" copy /y config.example.yaml "%OUTDIR%\" >nul
if not exist "%OUTDIR%\config.yaml" copy /y config.example.yaml "%OUTDIR%\config.yaml" >nul
copy /y 启动.bat "%OUTDIR%\" >nul
xcopy /e /i /y resources "%OUTDIR%\resources" >nul
rem 旧版本留下的第三方组件目录（已不再支持）直接清掉
if exist "%OUTDIR%\napcat" rmdir /s /q "%OUTDIR%\napcat"
if exist "%OUTDIR%\卸载 BaiAi-Tavern.exe" del /f /q "%OUTDIR%\卸载 BaiAi-Tavern.exe"

echo.
echo ============================================================
echo   打包完成！
echo     GUI    : %OUTDIR%\BaiAi-Tavern.exe
echo     Bot    : %OUTDIR%\bot.exe
echo     启动器 : %OUTDIR%\启动.bat
echo.
echo   想做成安装包：运行 scripts\build_installer.bat
echo ============================================================
pause
