"""PyInstaller 打包入口：Bot 进程。

为什么需要这个文件？
--------------------
PyInstaller 会把入口脚本当作 ``__main__`` 顶层模块执行，此时 ``bot.main`` 里的
``from . import ...`` 这类相对导入会失败（attempted relative import with no
known parent package）。用一个入口脚本 import 包，包内就可以继续使用相对导入。

开发时仍然推荐::

    python -m bot.main
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
