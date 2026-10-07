# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：GUI 主程序（BaiAi-Tavern.exe）。

用法::

    pyinstaller --noconfirm --clean pyinstaller.spec

生成的 exe 位于 dist/BaiAi-Tavern.exe，与 bot.exe 放在同一目录即可运行。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(ROOT, "resources"), "resources"),
    (os.path.join(ROOT, "config.example.yaml"), "."),
]

# GUI 不使用任何 Bot 侧依赖，显式排除可以显著减小体积
excludes = [
        "apscheduler",
    "aiosqlite",
    "openai",
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "PIL",
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "pyinstaller",
]

icon_path = os.path.join(ROOT, "resources", "icons", "app.ico")
icon = icon_path if os.path.exists(icon_path) else None

# 版本信息（让 exe 的「属性 → 详细信息」显示名称 / V0.2.2 / 作者），由 scripts/make_version_info.py 生成
version_path = os.path.join(ROOT, "build", "version_info_gui.txt")
version = version_path if os.path.exists(version_path) else None

a = Analysis(
    # 入口脚本负责 import app 包，这样包内的相对导入在打包后依然可用
    [os.path.join(ROOT, "scripts", "gui_entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "yaml",
        "httpx",
        "websockets",
        "websockets.sync.client",
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
    name="BaiAi-Tavern",
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
    icon=icon,
    version=version,
)
