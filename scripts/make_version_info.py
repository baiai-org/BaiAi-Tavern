"""生成 PyInstaller 用的版本信息文件（让 exe 的「属性 → 详细信息」显示名称/版本/作者）。

用法::

    python scripts/make_version_info.py

会在 ``build/`` 下生成：

* ``version_info_gui.txt``  —— 主程序 BaiAi-Tavern.exe
* ``version_info_bot.txt``  —— Bot 进程 bot.exe
* ``version_info_installer.txt`` —— 安装 / 卸载程序（同一个 exe）

打包脚本（``scripts/build.bat``、``scripts/build_installer.bat``）会自动调用本脚本；
对应的 spec 文件会在文件存在时引用它。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import APP_AUTHOR, APP_HOMEPAGE, APP_NAME, APP_VERSION_DISPLAY  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OUTPUT_DIR = ROOT / "build"
VERSION_TUPLE = (0, 2, 0, 0)  # 供 Windows 文件版本使用（V0.2）
FILE_VERSION = "%d.%d.%d.%d" % VERSION_TUPLE

TEMPLATE = """# UTF-8 编码，由 scripts/make_version_info.py 生成，请勿手工修改。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(version_tuple)s,
    prodvers=%(version_tuple)s,
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'080404B0',
          [StringStruct(u'CompanyName', u'%(author)s'),
           StringStruct(u'FileDescription', u'%(description)s'),
           StringStruct(u'FileVersion', u'%(file_version)s'),
           StringStruct(u'InternalName', u'%(internal_name)s'),
           StringStruct(u'LegalCopyright', u'%(author)s  %(homepage)s'),
           StringStruct(u'OriginalFilename', u'%(original_name)s'),
           StringStruct(u'ProductName', u'%(product_name)s'),
           StringStruct(u'ProductVersion', u'%(product_version)s')]
        )
      ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"""

TARGETS = [
    (
        "version_info_gui.txt",
        {
            "description": "%s —— QQ 多角色 AI 主动消息桌面应用（界面 + 托盘）" % APP_NAME,
            "internal_name": APP_NAME,
            "original_name": "%s.exe" % APP_NAME,
        },
    ),
    (
        "version_info_bot.txt",
        {
            "description": "%s Bot 服务进程（FastAPI + 官方机器人网关）" % APP_NAME,
            "internal_name": "%s-bot" % APP_NAME,
            "original_name": "bot.exe",
        },
    ),
    (
        "version_info_installer.txt",
        {
            "description": "%s %s 安装 / 卸载程序" % (APP_NAME, APP_VERSION_DISPLAY),
            "internal_name": "%s-setup" % APP_NAME,
            "original_name": "%s %s.exe" % (APP_NAME, APP_VERSION_DISPLAY),
        },
    ),
]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, extra in TARGETS:
        content = TEMPLATE % {
            "version_tuple": VERSION_TUPLE,
            "author": APP_AUTHOR,
            "homepage": APP_HOMEPAGE,
            "product_name": APP_NAME,
            "product_version": APP_VERSION_DISPLAY,
            "file_version": FILE_VERSION,
            **extra,
        }
        path = OUTPUT_DIR / filename
        path.write_text(content, encoding="utf-8")
        print("已生成 %s" % path)
    print("版本信息生成完成：%s %s（作者 %s）" % (APP_NAME, APP_VERSION_DISPLAY, APP_AUTHOR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
