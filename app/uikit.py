"""界面用的矢量小图标（样式表子控件图标 + 按钮图标）。

为什么需要它
------------
* Qt 的样式表一旦给子控件（``QSpinBox::up-button``、``QComboBox::drop-down``、
  ``QCheckBox::indicator`` …）设置了背景或边框，Qt 就不再绘制默认的箭头/对勾，
  界面上只剩「一坨灰色方块」。解决办法是给这些子控件提供 ``image:``：
  图片在运行时用 QPainter 画出来，写入数据目录，再由
  :func:`app.theme.load_qss` 把 ``url(@UI:xxx@)`` 占位符替换成真实路径
  （打包后同样有效，不依赖外部资源文件）。
* 按钮上的符号（▶ ■ ⟳ ✉）依赖字体，系统缺字形时会变成方块，
  因此按钮图标也在这里用 QPainter 画：:func:`icon` / :func:`set_icon`。

图标分辨率：``ASSETS_VERSION`` 变化时会写到新的子目录，保证升级后不会继续用旧图标。
"""

from __future__ import annotations

from math import cos, radians, sin
from pathlib import Path
from typing import Dict, Tuple

from common.logging_setup import get_logger
from common.paths import data_dir

log = get_logger("app.uikit")

# 图标几何或配色发生变化时 +1：目录随之变化，旧的缓存不会被继续使用
ASSETS_VERSION = 3
ASSET_DIR_NAME = "cache/ui/v%d" % ASSETS_VERSION

FG = "#c8d0dd"
FG_STRONG = "#e8edf6"
FG_HOVER = "#ffffff"

ASSETS: Dict[str, Tuple[str, str]] = {
    "arrow_up": ("arrow_up", FG_STRONG),
    "arrow_up_hover": ("arrow_up", FG_HOVER),
    "arrow_down": ("arrow_down", FG_STRONG),
    "arrow_down_hover": ("arrow_down", FG_HOVER),
    "chevron": ("chevron", FG_STRONG),
    "chevron_hover": ("chevron", FG_HOVER),
    "check": ("check", "#ffffff"),
    "dot": ("dot", "#ffffff"),
}

_asset_cache: Dict[str, str] = {}
_icon_cache: Dict[str, object] = {}


# ============================================================== 绘制基础 ====
def _painter(pixmap):
    from PySide6.QtGui import QPainter

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    return painter


def _triangle(painter, size: float, direction: str, color, width: float = 12.0, height: float = 7.0) -> None:
    """在 ``size×size`` 的方块中央画一个实心三角（单位是 16 基准下的比例）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QBrush, QPolygonF

    unit = size / 16.0
    w, h = width * unit, height * unit
    cx = cy = size / 2.0
    if direction == "up":
        points = [QPointF(cx, cy - h / 2.0), QPointF(cx - w / 2.0, cy + h / 2.0), QPointF(cx + w / 2.0, cy + h / 2.0)]
    else:
        points = [QPointF(cx, cy + h / 2.0), QPointF(cx - w / 2.0, cy - h / 2.0), QPointF(cx + w / 2.0, cy - h / 2.0)]
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPolygon(QPolygonF(points))


def _check_mark(painter, size: float, color, pen_width: float = 2.6) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPen

    unit = size / 16.0
    painter.setPen(QPen(color, max(1.5, pen_width * unit), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    points = [QPointF(3.4 * unit, 8.6 * unit), QPointF(6.8 * unit, 12.0 * unit), QPointF(12.8 * unit, 4.4 * unit)]
    for index in range(len(points) - 1):
        painter.drawLine(points[index], points[index + 1])


def _draw_asset_pixmap(kind: str, size: int, color: str):
    """样式表子控件用的小图标。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QBrush, QColor, QPixmap

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = _painter(pixmap)
    col = QColor(color)

    if kind in ("arrow_up", "arrow_down"):
        # 画满整个方块，避免在 20×16 的按钮里看起来只是一个小点
        _triangle(painter, size, "up" if kind == "arrow_up" else "down", col, width=12.0, height=7.0)
    elif kind == "chevron":
        _triangle(painter, size, "down", col, width=11.0, height=6.0)
    elif kind == "check":
        _check_mark(painter, size, col)
    elif kind == "dot":
        from PySide6.QtCore import QPointF

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        radius = 4.0 * (size / 16.0)
        painter.drawEllipse(QPointF(size / 2.0, size / 2.0), radius, radius)
    painter.end()
    return pixmap


def _assets_dir() -> Path:
    return data_dir() / ASSET_DIR_NAME


def asset_paths(force: bool = False) -> Dict[str, str]:
    """生成（或复用）样式表图标文件，返回 ``名称 → 绝对路径``。"""
    global _asset_cache
    if _asset_cache and not force:
        return dict(_asset_cache)

    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:  # 还没有 QApplication：跳过
            return {}
    except Exception:  # pragma: no cover
        return {}

    directory = _assets_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover
        log.debug("创建样式图标目录失败：%s", exc)
        return {}

    result: Dict[str, str] = {}
    for name, (kind, color) in ASSETS.items():
        try:
            target = directory / ("%s.png" % name)
            if not target.exists() or force:
                _draw_asset_pixmap(kind, 16, color).save(str(target), "PNG")
            # 高分辨率屏用（Qt 会在文件名带 @2x 时优先加载）
            target2x = directory / ("%s@2x.png" % name)
            if not target2x.exists() or force:
                _draw_asset_pixmap(kind, 32, color).save(str(target2x), "PNG")
            result[name] = target.as_posix()
        except Exception as exc:  # pragma: no cover
            log.debug("生成样式图标 %s 失败：%s", name, exc)
    _asset_cache = dict(result)
    return result


# ============================================================== 按钮图标 ====
def _draw_icon_pixmap(name: str, size: int, color: str):
    """按钮用的图标（32px 设计基准，按 size 缩放）。"""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QPixmap, QPolygonF

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = _painter(pixmap)
    col = QColor(color)
    painter.setPen(QPen(col, max(1.6, size * 0.11), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    unit = size / 32.0

    def rect(x, y, w, h, radius=3.0, fill=True, stroke=False):
        painter.setBrush(QBrush(col) if fill else Qt.NoBrush)
        painter.setPen(QPen(col, max(1.4, size * 0.09), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin) if stroke else Qt.NoPen)
        painter.drawRoundedRect(QRectF(x * unit, y * unit, w * unit, h * unit), radius * unit, radius * unit)

    if name == "play":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawPolygon(
            QPolygonF([QPointF(9 * unit, 6 * unit), QPointF(26 * unit, 16 * unit), QPointF(9 * unit, 26 * unit)])
        )
    elif name == "stop":
        rect(8, 8, 16, 16, 3.0)
    elif name in ("restart", "reload"):
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawArc(QRectF(7 * unit, 7 * unit, 18 * unit, 18 * unit), 40 * 16, 280 * 16)
        head = QPolygonF(
            [QPointF(22.5 * unit, 4.5 * unit), QPointF(25.5 * unit, 11.5 * unit), QPointF(18.0 * unit, 10.0 * unit)]
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawPolygon(head)
    elif name == "send":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(5 * unit, 15 * unit),
                    QPointF(27 * unit, 5 * unit),
                    QPointF(19 * unit, 27 * unit),
                    QPointF(14.5 * unit, 17.5 * unit),
                ]
            )
        )
    elif name == "folder":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawRoundedRect(QRectF(4 * unit, 9 * unit, 24 * unit, 16 * unit), 3 * unit, 3 * unit)
        painter.drawRoundedRect(QRectF(4 * unit, 6 * unit, 11 * unit, 7 * unit), 2.5 * unit, 2.5 * unit)
    elif name == "file":
        path = QPainterPath()
        path.moveTo(8 * unit, 4 * unit)
        path.lineTo(19 * unit, 4 * unit)
        path.lineTo(25 * unit, 10 * unit)
        path.lineTo(25 * unit, 28 * unit)
        path.lineTo(8 * unit, 28 * unit)
        path.closeSubpath()
        painter.setPen(QPen(col, max(1.4, size * 0.09)))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
    elif name == "plus":
        painter.setPen(QPen(col, max(1.8, size * 0.12), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(16 * unit, 7 * unit), QPointF(16 * unit, 25 * unit))
        painter.drawLine(QPointF(7 * unit, 16 * unit), QPointF(25 * unit, 16 * unit))
    elif name == "trash":
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(8 * unit, 10 * unit), QPointF(24 * unit, 10 * unit))
        painter.drawLine(QPointF(13 * unit, 7 * unit), QPointF(19 * unit, 7 * unit))
        rect(10, 12, 12, 14, 2.5, fill=False, stroke=True)
    elif name == "refresh":
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawArc(QRectF(7 * unit, 7 * unit, 18 * unit, 18 * unit), 200 * 16, 250 * 16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawPolygon(
            QPolygonF(
                [QPointF(10 * unit, 3 * unit), QPointF(16 * unit, 9 * unit), QPointF(8.5 * unit, 10.5 * unit)]
            )
        )
    elif name == "check":
        _check_mark(painter, size, col, pen_width=3.2)
    elif name == "link":
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap))
        painter.drawRoundedRect(QRectF(5 * unit, 12 * unit, 13 * unit, 9 * unit), 4.5 * unit, 4.5 * unit)
        painter.drawRoundedRect(QRectF(14 * unit, 11 * unit, 13 * unit, 9 * unit), 4.5 * unit, 4.5 * unit)
    elif name == "scan":
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap))
        painter.drawEllipse(QRectF(7 * unit, 6 * unit, 14 * unit, 14 * unit))
        painter.drawLine(QPointF(21 * unit, 20 * unit), QPointF(26 * unit, 25 * unit))
    elif name == "wand":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        star = QPainterPath()
        star.moveTo(16 * unit, 4 * unit)
        star.lineTo(19.5 * unit, 12.5 * unit)
        star.lineTo(28 * unit, 16 * unit)
        star.lineTo(19.5 * unit, 19.5 * unit)
        star.lineTo(16 * unit, 28 * unit)
        star.lineTo(12.5 * unit, 19.5 * unit)
        star.lineTo(4 * unit, 16 * unit)
        star.lineTo(12.5 * unit, 12.5 * unit)
        star.closeSubpath()
        painter.drawPath(star)
    elif name == "robot":
        rect(7, 8, 18, 16, 4.0)
        from PySide6.QtGui import QColor as _QColor

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(_QColor("#14161a")))
        painter.drawEllipse(QRectF(11 * unit, 14 * unit, 3.4 * unit, 3.4 * unit))
        painter.drawEllipse(QRectF(17.6 * unit, 14 * unit, 3.4 * unit, 3.4 * unit))
        painter.setPen(QPen(col, max(1.4, size * 0.09), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(16 * unit, 8 * unit), QPointF(16 * unit, 5 * unit))
    elif name == "user":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawEllipse(QRectF(11 * unit, 6 * unit, 10 * unit, 10 * unit))
        path = QPainterPath()
        path.moveTo(6 * unit, 27 * unit)
        path.quadTo(16 * unit, 17 * unit, 26 * unit, 27 * unit)
        painter.drawPath(path)
    elif name == "message":
        rect(5, 8, 22, 14, 4.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawPolygon(
            QPolygonF([QPointF(11 * unit, 21 * unit), QPointF(17 * unit, 21 * unit), QPointF(12 * unit, 27 * unit)])
        )
    elif name == "dot":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawEllipse(QPointF(16 * unit, 16 * unit), 5 * unit, 5 * unit)
    elif name == "chart":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        for index, bar_height in enumerate((9.0, 16.0, 22.0)):
            x = (5.5 + index * 7.5) * unit
            painter.drawRoundedRect(
                QRectF(x, (27.0 - bar_height) * unit, 5.0 * unit, bar_height * unit), 1.6 * unit, 1.6 * unit
            )
        painter.drawRoundedRect(QRectF(5 * unit, 25.5 * unit, 22 * unit, 2.6 * unit), 1.3 * unit, 1.3 * unit)
    elif name == "gear":
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap))
        painter.drawEllipse(QRectF(11 * unit, 11 * unit, 10 * unit, 10 * unit))
        painter.drawEllipse(QRectF(7 * unit, 7 * unit, 18 * unit, 18 * unit))
        for angle in range(0, 360, 45):
            rad = radians(angle)
            painter.drawLine(
                QPointF(16 * unit + 9 * unit * cos(rad), 16 * unit + 9 * unit * sin(rad)),
                QPointF(16 * unit + 12.5 * unit * cos(rad), 16 * unit + 12.5 * unit * sin(rad)),
            )
    elif name == "chip":  # 模型路由：芯片 + 引脚
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(col, max(1.6, size * 0.10), Qt.SolidLine, Qt.RoundCap))
        painter.drawRoundedRect(QRectF(9 * unit, 9 * unit, 14 * unit, 14 * unit), 2.4 * unit, 2.4 * unit)
        painter.setBrush(QBrush(col))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(13 * unit, 13 * unit, 6 * unit, 6 * unit), 1.4 * unit, 1.4 * unit)
        for offset in (12.0, 16.0, 20.0):
            for end in (6.0, 26.0):
                painter.drawLine(QPointF(offset * unit, 7.5 * unit), QPointF(offset * unit, 9 * unit))
                painter.drawLine(QPointF(offset * unit, end * unit), QPointF(offset * unit, (end - 1.5) * unit))
                painter.drawLine(QPointF(7.5 * unit, offset * unit), QPointF(9 * unit, offset * unit))
                painter.drawLine(QPointF(end * unit, offset * unit), QPointF((end - 1.5) * unit, offset * unit))
    else:  # pragma: no cover - 未定义的图标名画一个圆点，避免运行时出错
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(col))
        painter.drawEllipse(QPointF(16 * unit, 16 * unit), 5 * unit, 5 * unit)
    painter.end()
    return pixmap


def icon(name: str, color: str = FG, size: int = 32):
    """返回一个 :class:`QIcon`（按钮用，无需写文件）。"""
    from PySide6.QtGui import QIcon

    key = "%s:%s:%d" % (name, color, size)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:  # pragma: no cover
            return QIcon()
    except Exception:  # pragma: no cover
        return QIcon()

    result = QIcon()
    for target in (16, 20, 24, 32, 48, 64):
        result.addPixmap(_draw_icon_pixmap(name, target, color))
    if size and size != 32:
        result = QIcon(_draw_icon_pixmap(name, size, color))
    _icon_cache[key] = result
    return result


def set_icon(button, name: str, color: str = FG) -> None:
    """给按钮设置图标（按钮文字里的符号可以由它替代，避免依赖字体字形）。"""
    try:
        button.setIcon(icon(name, color))
        button.setIconSize(_icon_size())
    except Exception as exc:  # pragma: no cover
        log.debug("设置按钮图标失败（%s）：%s", name, exc)


def _icon_size():
    from PySide6.QtCore import QSize

    return QSize(16, 16)


__all__ = ["ASSETS", "ASSETS_VERSION", "asset_paths", "icon", "set_icon"]
