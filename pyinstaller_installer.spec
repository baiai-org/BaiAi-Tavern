# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：安装程序（BaiAi-Tavern V0.1.exe）。

**只有一个 EXE**：既是安装程序也是卸载入口（安装后在「设置 → 应用」里卸载时，
调用的是安装目录里的 ``BaiAi-Tavern.exe --uninstall``）。
单文件 exe 内部携带 ``payload``（程序文件）：

    pyinstaller --noconfirm --clean pyinstaller_installer.spec

payload 由 ``scripts/build_installer.bat`` 先组装到 ``build/payload``。
"""

import os

ROOT = os.path.abspath(os.getcwd())

payload = os.path.join(ROOT, "build", "payload")
datas = [(payload, "payload")] if os.path.isdir(payload) else []

excludes = [
    "apscheduler",
    "aiosqlite",
    "openai",
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "PIL",
    "numpy",
    "pandas",
    "PySide6",
    "shiboken6",
]

icon_path = os.path.join(ROOT, "resources", "icons", "app.ico")
icon = icon_path if os.path.exists(icon_path) else None

# 版本信息（让 exe 的「属性 → 详细信息」显示名称 / V0.1 / 作者），由 scripts/make_version_info.py 生成
version_path = os.path.join(ROOT, "build", "version_info_installer.txt")
version = version_path if os.path.exists(version_path) else None

a = Analysis(
    [os.path.join(ROOT, "installer", "installer_main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=["installer.common"],
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
    name="BaiAi-Tavern V0.2",
    debug=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=icon,
    version=version,
)
