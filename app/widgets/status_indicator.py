"""状态指示灯与统计卡片。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

STATE_COLORS = {
    "ok": "#3ddc84",
    "warn": "#f0b429",
    "bad": "#f2725c",
    "idle": "#8a8f98",
    "info": "#4c7df0",
}


def state_color(state: str) -> QColor:
    return QColor(STATE_COLORS.get(state, STATE_COLORS["idle"]))


class StatusDot(QWidget):
    """小圆点状态灯。"""

    def __init__(self, state: str = "idle", diameter: int = 12, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._state = state
        self._diameter = diameter
        self.setFixedSize(diameter + 4, diameter + 4)

    def set_state(self, state: str) -> None:
        state = state if state in STATE_COLORS else "idle"
        if state != self._state:
            self._state = state
            self.update()

    def state(self) -> str:
        return self._state

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = state_color(self._state)
        glow = QColor(color)
        glow.setAlpha(75)
        center_x = self.width() / 2.0
        center_y = self.height() / 2.0
        radius = self._diameter / 2.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QRectF(center_x - radius - 1, center_y - radius - 1, radius * 2 + 2, radius * 2 + 2))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QRectF(center_x - radius + 1, center_y - radius + 1, radius * 2 - 2, radius * 2 - 2))
        painter.end()


class StatusCard(QFrame):
    """仪表盘卡片：标题 + 主值 + 说明 + 状态灯。"""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        value: str = "--",
        hint: str = "",
        state: str = "idle",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(96)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        top.addWidget(self.title_label)
        top.addStretch(1)
        self.dot = StatusDot(state, 12)
        top.addWidget(self.dot, 0, Qt.AlignTop)
        layout.addLayout(top)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("CardValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("CardHint")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    def set_value(self, value: str, hint: Optional[str] = None, state: Optional[str] = None) -> None:
        self.value_label.setText(str(value))
        if hint is not None:
            self.hint_label.setText(str(hint))
        if state is not None:
            self.dot.set_state(state)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class InfoRow(QWidget):
    """一行信息：左标签 + 右值。"""

    def __init__(self, label: str, value: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(10)
        self.name_label = QLabel(label)
        self.name_label.setObjectName("MutedLabel")
        self.name_label.setMinimumWidth(96)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("ValueLabel")
        self.value_label.setWordWrap(True)
        self.value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.name_label)
        layout.addWidget(self.value_label, 1)

    def set_value(self, value: str) -> None:
        self.value_label.setText(str(value))


# 便于测试：纯绘制函数
def dot_pixmap(state: str = "ok", size: int = 12) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(state_color(state)))
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return pixmap


__all__ = ["InfoRow", "StatusCard", "StatusDot", "STATE_COLORS", "dot_pixmap", "state_color"]
