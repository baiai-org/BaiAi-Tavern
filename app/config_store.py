"""GUI 侧配置门面（与 Bot 共用同一个 config.yaml）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from common.config import ConfigManager, get_config, to_plain
from common.paths import ensure_dirs


def gui_config(path: Optional[Path] = None) -> ConfigManager:
    ensure_dirs()
    manager = get_config(path)
    manager.ensure_file()
    manager.load()
    return manager


def save_config(manager: ConfigManager, patch: Dict[str, Any]) -> None:
    """局部更新并写回文件。

    写之前先检查磁盘上的外部变更（Bot 进程也会写同一个文件），
    以免用界面里较旧的副本覆盖掉 Bot 刚保存的配置。
    """
    manager.reload_if_changed()
    manager.patch(patch, persist=True)
    manager.load(force=True)


def snapshot(manager: ConfigManager) -> Dict[str, Any]:
    return to_plain(manager.data)


__all__ = ["ConfigManager", "gui_config", "save_config", "snapshot"]
