"""主题与样式加载。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from common.logging_setup import get_logger
from common.paths import resource_path

log = get_logger("app.theme")

FALLBACK_QSS = """
QWidget { background-color: #14161a; color: #e6e8ee; }
QPushButton { background-color: #262b35; border: 1px solid #333a48; border-radius: 8px; padding: 6px 12px; }
QPushButton[variant="primary"] { background-color: #3f6ee0; color: #ffffff; font-weight: bold; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: #1a1e25; border: 1px solid #2f3644; border-radius: 8px; padding: 6px;
}
QGroupBox { border: 1px solid #2a2f3a; border-radius: 10px; margin-top: 14px; padding: 12px; }
QTableWidget { background-color: #1a1e25; alternate-background-color: #1e222a; border: 1px solid #2a2f3a; }
"""

# 样式表里的 @UI:name@ 占位符：运行时替换成生成好的小图标路径
_UI_TOKEN_RE = re.compile(r"@UI:([A-Za-z0-9_]+)@")


def _substitute_assets(text: str) -> str:
    if "@UI:" not in text:
        return text
    try:
        from .uikit import asset_paths

        mapping = asset_paths()
    except Exception as exc:  # pragma: no cover - 图标生成失败不应影响主题
        log.debug("生成样式图标失败：%s", exc)
        mapping = {}

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        path = mapping.get(name)
        # 没有拿到图片时退化成「无图片」，至少不会把占位符原样塞进样式表
        return path if path else ""

    return _UI_TOKEN_RE.sub(_replace, text)


def load_qss(name: str = "dark") -> str:
    path = Path(resource_path("styles", "%s.qss" % name))
    if path.exists():
        try:
            return _substitute_assets(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            log.warning("读取样式文件失败 %s: %s", path, exc)
    return FALLBACK_QSS


def apply_theme(app, theme: Optional[str] = None) -> str:
    """给 QApplication 应用样式，返回实际使用的主题名。"""
    name = (theme or "dark").strip().lower()
    if name not in ("dark", "light"):
        name = "dark"
    if name == "light":
        # 目前只提供深色主题，light 回退到 dark 但保持接口兼容
        log.info("暂未提供浅色主题，已回退到深色主题")
        name = "dark"
    app.setStyle("Fusion")
    app.setStyleSheet(load_qss(name))
    return name


__all__ = ["apply_theme", "load_qss"]
