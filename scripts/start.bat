@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo ============================================================
echo   BaiAi-Tavern 开发模式启动
echo ============================================================
echo.

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [提示] 未找到虚拟环境，正在创建 .venv 并安装依赖 ...
    python -m venv .venv
    set "PY=.venv\Scripts\python.exe"
    if not exist "%PY%" (
        echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.9+ 并加入 PATH。
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败。
        pause
        exit /b 1
    )
)

if not exist "data\config.yaml" (
    echo [提示] 首次运行，正在生成配置文件 data\config.yaml ...
    "%PY%" -c "from common.config import get_config; get_config().ensure_file()"
)

echo 正在启动 GUI（开发模式）...
echo   - Bot 进程会由 GUI 自动拉起（python -m bot.main）
echo   - 日志：data\logs\gui.log 与 data\logs\bot.log
echo.
"%PY%" -m app.main
