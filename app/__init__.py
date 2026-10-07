"""BaiAi-Tavern 图形界面（PySide6）。

GUI 进程只负责界面与进程编排，不直接访问数据库：
所有业务操作都通过 HTTP API 调用 Bot 进程。
"""

__version__ = "0.2"
APP_VERSION_DISPLAY = "V0.2.2"
APP_NAME = "BaiAi-Tavern"
APP_DISPLAY_NAME = "BaiAi-Tavern"
APP_AUTHOR = "baiai.org"
APP_HOMEPAGE = "https://baiai.org"

__all__ = [
    "APP_AUTHOR",
    "APP_DISPLAY_NAME",
    "APP_HOMEPAGE",
    "APP_NAME",
    "APP_VERSION_DISPLAY",
    "__version__",
]
