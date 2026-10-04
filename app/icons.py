"""图标与头像绘制。

程序图标与托盘图标全部由代码绘制，因此打包时不需要额外资源文件；
若 ``resources/icons/`` 下存在同名 ``.ico/.png``，则优先使用外部文件。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from common.paths import resource_path

_ICON_CACHE: Dict[str, QIcon] = {}
_PIXMAP_CACHE: Dict[str, QPixmap] = {}

PALETTE = [
    "#5B8DEF",
    "#F2725C",
    "#48B884",
    "#B87BE0",
    "#F0B429",
    "#3FB6C8",
    "#E76F9C",
    "#7C8CA8",
]


def color_from_text(text: str) -> QColor:
    digest = hashlib.md5((text or "?").encode("utf-8")).hexdigest()
    return QColor(PALETTE[int(digest[:4], 16) % len(PALETTE)])


def _external_icon(name: str) -> Optional[QIcon]:
    for suffix in (".ico", ".png"):
        path = Path(resource_path("icons", name + suffix))
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return None


# ============================================================== 应用图标 ====
def _draw_app_pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    margin = size * 0.06
    rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    radius = size * 0.24

    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#4C7DF0"))
    gradient.setColorAt(1.0, QColor("#7B4CF0"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(rect, radius, radius)

    # 对话气泡
    bubble = QRectF(size * 0.22, size * 0.24, size * 0.56, size * 0.40)
    painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
    painter.drawRoundedRect(bubble, size * 0.10, size * 0.10)
    tail = QPainterPath()
    tail.moveTo(size * 0.34, size * 0.62)
    tail.lineTo(size * 0.30, size * 0.78)
    tail.lineTo(size * 0.46, size * 0.63)
    tail.closeSubpath()
    painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
    painter.drawPath(tail)

    # 三个点
    painter.setBrush(QBrush(QColor("#4C7DF0")))
    dot_size = size * 0.075
    for index in range(3):
        center_x = size * (0.33 + index * 0.17)
        painter.drawEllipse(
            QPointF(center_x, size * 0.44), dot_size / 2, dot_size / 2
        )
    painter.end()
    return pixmap


def app_pixmap(size: int = 256) -> QPixmap:
    key = "app:%d" % size
    if key not in _PIXMAP_CACHE:
        _PIXMAP_CACHE[key] = _draw_app_pixmap(size)
    return _PIXMAP_CACHE[key]


def app_icon() -> QIcon:
    if "app" in _ICON_CACHE:
        return _ICON_CACHE["app"]
    external = _external_icon("app")
    if external is not None:
        _ICON_CACHE["app"] = external
        return external
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size))
    _ICON_CACHE["app"] = icon
    return icon


# ============================================================== 托盘图标 ====
def _draw_tray_pixmap(size: int, running: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    center = QPointF(size / 2.0, size / 2.0)
    outer = size * 0.46
    color = QColor("#3DDC84") if running else QColor("#8A8F98")
    glow = QColor(color)
    glow.setAlpha(70 if running else 40)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(glow))
    painter.drawEllipse(center, outer, outer)

    painter.setBrush(QBrush(color))
    painter.setPen(QPen(QColor(255, 255, 255, 200), max(1.0, size * 0.05)))
    painter.drawEllipse(center, outer * 0.66, outer * 0.66)

    # 内部对话点
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255, 240)))
    dot = size * 0.085
    for index in (-1, 0, 1):
        painter.drawEllipse(
            QPointF(center.x() + index * size * 0.17, center.y()), dot, dot
        )
    painter.end()
    return pixmap


def tray_icon(running: bool) -> QIcon:
    key = "tray:%s" % ("on" if running else "off")
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    external = _external_icon("tray_green" if running else "tray_gray")
    if external is not None:
        _ICON_CACHE[key] = external
        return external
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(_draw_tray_pixmap(size, running))
    _ICON_CACHE[key] = icon
    return icon


# ================================================================ 头像 ======
def placeholder_avatar(name: str, size: int = 64) -> QPixmap:
    key = "avatar:%s:%d" % (name, size)
    if key in _PIXMAP_CACHE:
        return _PIXMAP_CACHE[key]
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    color = color_from_text(name)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, color.lighter(115))
    gradient.setColorAt(1.0, color.darker(115))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.setPen(QColor(255, 255, 255, 240))
    font = QFont("Microsoft YaHei", max(8, int(size * 0.42)))
    font.setBold(True)
    painter.setFont(font)
    initial = (name or "?").strip()[:1].upper()
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, initial)
    painter.end()
    _PIXMAP_CACHE[key] = pixmap
    return pixmap


def rounded_pixmap(source: QPixmap, size: int) -> QPixmap:
    scaled = source.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
    )
    result = QPixmap(size, size)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(path)
    offset_x = (scaled.width() - size) / 2.0
    offset_y = (scaled.height() - size) / 2.0
    painter.drawPixmap(QPointF(-offset_x, -offset_y), scaled)
    painter.end()
    return result


def avatar_pixmap(data: Optional[bytes], name: str = "", size: int = 64) -> QPixmap:
    if data:
        source = QPixmap()
        if source.loadFromData(data) and not source.isNull():
            return rounded_pixmap(source, size)
    return placeholder_avatar(name or "?", size)


def avatar_pixmap_from_file(path: Optional[Path], name: str = "", size: int = 64) -> QPixmap:
    if path:
        source = QPixmap(str(path))
        if not source.isNull():
            return rounded_pixmap(source, size)
    return placeholder_avatar(name or "?", size)


def meter_pixmap(ratio: float, size: int = 96) -> QPixmap:
    """进度环（仪表盘用），ratio 取值 0-1。"""
    ratio = max(0.0, min(1.0, float(ratio)))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    margin = size * 0.12
    rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    painter.setPen(QPen(QColor(255, 255, 255, 40), size * 0.10, Qt.SolidLine, Qt.RoundCap))
    painter.drawArc(rect, 0, 360 * 16)
    color = QColor("#48B884") if ratio < 0.8 else QColor("#F0B429")
    painter.setPen(QPen(color, size * 0.10, Qt.SolidLine, Qt.RoundCap))
    painter.drawArc(rect, 90 * 16, -int(360 * 16 * ratio))
    painter.end()
    return pixmap


__all__ = [
    "app_icon",
    "app_pixmap",
    "avatar_pixmap",
    "avatar_pixmap_from_file",
    "color_from_text",
    "meter_pixmap",
    "placeholder_avatar",
    "rounded_pixmap",
    "tray_icon",
]
