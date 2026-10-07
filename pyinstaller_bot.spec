# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Bot 服务进程（bot.exe）。

用法::

    pyinstaller --noconfirm --clean pyinstaller_bot.spec

生成的 dist/bot.exe 需要与 BaiAi-Tavern.exe 放在同一目录。
"""

import os

ROOT = os.path.abspath(os.getcwd())

# 版本信息（让 exe 的「属性 → 详细信息」显示名称 / V0.2.2 / 作者），由 scripts/make_version_info.py 生成
_version_path = os.path.join(ROOT, "build", "version_info_bot.txt")
version = _version_path if os.path.exists(_version_path) else None



datas = [
    (os.path.join(ROOT, "config.example.yaml"), "."),
    # 内置默认角色卡：角色池为空时自动导入
    (os.path.join(ROOT, "resources", "characters"), os.path.join("resources", "characters")),
]

# Bot 进程不需要 GUI / 科学计算相关依赖
excludes = [
    "PySide6",
    "shiboken6",
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "pyinstaller",
]

a = Analysis(
    # 入口脚本负责 import bot 包，这样包内的相对导入在打包后依然可用
    [os.path.join(ROOT, "scripts", "bot_entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=[
        # 通过字符串动态加载的模块，必须显式声明
        "bot.api",
        "bot.runtime",
        "bot.qq_official.client",
        "bot.qq_official.gateway",
        "bot.qq_official.receiver",
        "bot.qq_official.messaging",
        "bot.scheduler.proactive",
        "bot.scheduler.triggers",
        "bot.character_manager.loader",
        "bot.character_manager.registry",
        "bot.memory.short_term",
        "bot.memory.long_term",
        "bot.ai_engine.engine",
        "bot.ai_engine.llm_client",
        "bot.ai_engine.prompt_builder",
        # 富媒体（V0.2）：hub / 生图 / 语音
        "bot.media",
        "bot.media.hub",
        "bot.media.store",
        "bot.media.voice",
        "bot.media.images",
        # 第三方
        "aiosqlite",
        "apscheduler",
        "apscheduler.schedulers.asyncio",
        "apscheduler.triggers.cron",
        "apscheduler.triggers.date",
        "apscheduler.triggers.interval",
        "tzlocal",
        "openai",
        "fastapi",
        "starlette",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "websockets",
        "yaml",
        "httpx",
        "common.config",
        "common.paths",
        "common.logging_setup",
        "common.utils",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=version,
)
