"""统一的路径解析。

开发模式下所有路径相对项目根目录；PyInstaller 打包后相对 exe 所在目录。
用户数据目录优先落在 exe 同级 ``data/``，若该目录不可写（例如装在
``C:\\Program Files``）则自动回落到 ``%APPDATA%/BaiAi-Tavern``。

环境变量：新名字 ``BAIAI_HOME`` / ``BAIAI_DATA_DIR``；旧名字 ``QQAI_HOME`` /
``QQAI_DATA_DIR`` 仍然兼容（改名前的脚本、快捷方式不用改也能用）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

# (新名字, 兼容的旧名字)
_ENV_ROOT = ("BAIAI_HOME", "QQAI_HOME")
_ENV_DATA = ("BAIAI_DATA_DIR", "QQAI_DATA_DIR")
# 数据回落到 %APPDATA% 时的目录名（新 → 旧，旧目录存在时继续沿用，避免丢配置）
_APPDATA_NAMES = ("BaiAi-Tavern", "QQ-AI-Tavern")

_data_dir_cache: Optional[Path] = None


def _env(names: Tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境中。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """可写根目录：打包后为 exe 所在目录，开发时为项目根目录。"""
    env = _env(_ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """只读资源根目录（onefile 模式下为临时解包目录）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_root()


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def data_dir() -> Path:
    """用户数据根目录（可写）。"""
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache
    env = _env(_ENV_DATA)
    if env:
        _data_dir_cache = Path(env).expanduser().resolve()
        return _data_dir_cache
    candidate = app_root() / "data"
    if not _is_writable(candidate):
        appdata = Path(os.environ.get("APPDATA") or str(Path.home()))
        # 优先新目录；旧名字的目录已存在则继续沿用（老用户的配置和数据不会丢）
        candidate = appdata / _APPDATA_NAMES[0] / "data"
        legacy = appdata / _APPDATA_NAMES[1] / "data"
        if not candidate.exists() and legacy.exists():
            candidate = legacy
        _is_writable(candidate)
    _data_dir_cache = candidate
    return candidate


def logs_dir() -> Path:
    return data_dir() / "logs"


def characters_dir() -> Path:
    return data_dir() / "characters"


def avatars_dir() -> Path:
    return characters_dir() / "avatars"


def cards_dir() -> Path:
    return characters_dir() / "cards"


def database_path() -> Path:
    return data_dir() / "bot.db"


def resource_path(*parts: str) -> Path:
    """打包后优先使用解包目录中的资源，开发时使用项目 resources/。"""
    return bundle_dir().joinpath("resources", *parts)


def builtin_characters_dir() -> Path:
    """随程序分发的内置角色卡目录（首次运行可一键导入）。"""
    return resource_path("characters")


def default_config_path() -> Path:
    for name in ("BAIAI_CONFIG", "QQAI_CONFIG"):
        env = os.environ.get(name)
        if env:
            return Path(env).expanduser().resolve()
    return data_dir() / "config.yaml"


def example_config_path() -> Path:
    """随包分发的配置模板。"""
    candidate = bundle_dir() / "config.example.yaml"
    if candidate.exists():
        return candidate
    return app_root() / "config.example.yaml"


def ensure_dirs() -> None:
    for path in (data_dir(), logs_dir(), characters_dir(), avatars_dir(), cards_dir()):
        path.mkdir(parents=True, exist_ok=True)


def relpath(path: Path, base: Optional[Path] = None) -> str:
    """相对 app_root 显示路径，便于日志阅读。"""
    base = base or app_root()
    try:
        return str(Path(path).resolve().relative_to(base))
    except Exception:
        return str(path)
