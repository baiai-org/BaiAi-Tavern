"""开机自启（Windows 注册表 Run 项）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from common.logging_setup import get_logger
from common.paths import app_root, is_frozen

log = get_logger("app.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BaiAi-Tavern"


def launch_command() -> Optional[List[str]]:
    """返回开机自启应执行的命令（不含引号）。"""
    if is_frozen():
        return [str(Path(sys.executable).resolve()), "--minimized"]
    entry = app_root() / "app" / "main.py"
    if not entry.exists():
        return None
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return [str(interpreter), str(entry), "--minimized"]


def _quote(command: List[str]) -> str:
    return " ".join('"%s"' % item for item in command)


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except Exception:
        return False


def set_enabled(enabled: bool) -> bool:
    """写入/移除注册表项，返回是否操作成功。"""
    if os.name != "nt":
        return False
    command = launch_command()
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                if not command:
                    return False
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _quote(command))
                log.info("已开启开机自启: %s", _quote(command))
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                    log.info("已关闭开机自启")
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        log.warning("设置开机自启失败: %s", exc)
        return False


__all__ = ["is_enabled", "launch_command", "set_enabled"]
