"""Bot 进程的配置门面。

真正的实现在 :mod:`common.config`（GUI 与 Bot 共用同一个 config.yaml）。
这里只做再导出与 Bot 侧常用便捷函数，保持开发书中的目录结构。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from common.config import (  # noqa: F401  (再导出)
    DEFAULTS,
    AttrDict,
    ConfigManager,
    deep_merge,
    default_config_dict,
    get_config,
    to_plain,
)
from common.paths import ensure_dirs

__all__ = [
    "DEFAULTS",
    "AttrDict",
    "ConfigManager",
    "bot_config",
    "deep_merge",
    "default_config_dict",
    "ensure_config",
    "get_config",
    "to_plain",
]


def bot_config(path: Optional[Path] = None) -> ConfigManager:
    """获取 Bot 进程使用的配置管理器单例。"""
    ensure_dirs()
    return get_config(path)


def ensure_config(path: Optional[Path] = None) -> ConfigManager:
    """确保配置文件存在并完成首次加载。"""
    manager = bot_config(path)
    manager.ensure_file()
    manager.load(force=True)
    return manager
