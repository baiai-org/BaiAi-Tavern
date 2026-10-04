"""PyInstaller 打包入口：GUI 主程序。

原因同 ``scripts/bot_entry.py``：入口脚本若直接用 ``app/main.py``，
其中的相对导入（``from . import ...``）在打包后无法解析。

开发时仍然推荐::

    python -m app.main
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
