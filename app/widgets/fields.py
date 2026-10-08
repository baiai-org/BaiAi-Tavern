"""通用表单控件与小组件。"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from common.utils import format_hhmm


def set_variant(widget: QWidget, variant: str) -> QWidget:
    widget.setProperty("variant", variant)
    return widget


def primary_button(text: str, parent: Optional[QWidget] = None) -> QPushButton:
    button = QPushButton(text, parent)
    return set_variant(button, "primary")  # type: ignore[return-value]


def ghost_button(text: str, parent: Optional[QWidget] = None) -> QPushButton:
    button = QPushButton(text, parent)
    return set_variant(button, "ghost")  # type: ignore[return-value]


def danger_button(text: str, parent: Optional[QWidget] = None) -> QPushButton:
    button = QPushButton(text, parent)
    return set_variant(button, "danger")  # type: ignore[return-value]


def hint_label(text: str, parent: Optional[QWidget] = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("HintLabel")
    label.setWordWrap(True)
    return label


def section_title(text: str, parent: Optional[QWidget] = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("SectionTitle")
    return label


def make_group(title: str, parent: Optional[QWidget] = None) -> QGroupBox:
    group = QGroupBox(title, parent)
    group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return group


def add_form_row(
    form: QFormLayout,
    label: str,
    widget: QWidget,
    hint: str = "",
    label_width: int = 132,
) -> QWidget:
    """向 QFormLayout 添加一行，可选在下方显示灰色说明。"""
    name = QLabel(label)
    name.setObjectName("ValueLabel")
    name.setMinimumWidth(label_width)
    name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    if hint:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(widget)
        layout.addWidget(hint_label(hint))
        form.addRow(name, container)
        return container
    form.addRow(name, widget)
    return widget


def switch_row(
    text: str,
    checked: bool = False,
    hint: str = "",
    parent: Optional[QWidget] = None,
) -> QWidget:
    """带说明文字的开关行。"""
    container = QWidget(parent)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    box = QCheckBox(text, container)
    box.setChecked(bool(checked))
    layout.addWidget(box)
    if hint:
        layout.addWidget(hint_label(hint))
    container.checkbox = box  # type: ignore[attr-defined]
    container.setProperty("switchRow", True)
    return container


def switch_checkbox(container: QWidget) -> QCheckBox:
    return container.checkbox  # type: ignore[attr-defined]


class TimeListEdit(QWidget):
    """时间点列表编辑器（用于“每天固定触发时间”）。"""

    changed = Signal()

    def __init__(self, times: Optional[List[str]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: List[QLineEdit] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._row_container = QWidget(self)
        self._rows_layout = QVBoxLayout(self._row_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(6)
        self._layout.addWidget(self._row_container)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        add_button = ghost_button("添加时间", self)
        add_button.setProperty("chip", True)
        try:
            from ..uikit import set_icon

            set_icon(add_button, "plus")
        except Exception:  # pragma: no cover - 图标失败不影响功能
            pass
        add_button.clicked.connect(lambda: self.add_time())
        self.hint = hint_label("格式 HH:MM，例如 09:00 / 21:30", self)
        controls.addWidget(add_button)
        controls.addWidget(self.hint)
        controls.addStretch(1)
        self._layout.addLayout(controls)

        for value in times or []:
            self._add_row(format_hhmm(value, "09:00"))
        if not times:
            self._add_row("09:00")

    # ---------------------------------------------------------------- 行操作
    def _add_row(self, value: str) -> QLineEdit:
        row = QWidget(self._row_container)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        editor = QLineEdit(value, row)
        editor.setPlaceholderText("HH:MM")
        editor.setFixedWidth(96)
        editor.setAlignment(Qt.AlignCenter)
        editor.textChanged.connect(self.changed.emit)
        remove = ghost_button("删除", row)
        remove.setProperty("chip", True)
        remove.clicked.connect(lambda: self._remove_row(editor))
        layout.addWidget(editor)
        layout.addWidget(remove)
        layout.addStretch(1)
        self._rows_layout.addWidget(row)
        self._rows.append(editor)
        editor._row = row  # type: ignore[attr-defined]
        return editor

    def _remove_row(self, editor: QLineEdit) -> None:
        if editor in self._rows:
            self._rows.remove(editor)
        row = getattr(editor, "_row", None)
        if row is not None:
            self._rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self.changed.emit()

    def add_time(self, value: str = "12:00") -> None:
        editor = self._add_row(format_hhmm(value, "12:00"))
        editor.setFocus()
        self.changed.emit()

    # ---------------------------------------------------------------- 取值
    def times(self) -> List[str]:
        result: List[str] = []
        for editor in self._rows:
            text = format_hhmm(editor.text(), "")
            if text and text not in result:
                result.append(text)
        return result

    def set_times(self, times: List[str]) -> None:
        for editor in list(self._rows):
            self._remove_row(editor)
        for value in times or ["09:00"]:
            self._add_row(format_hhmm(value, "09:00"))
        self.changed.emit()


class ProbabilityRow(QWidget):
    """0–100 的滑动条 + 百分比显示（语音回复概率等场景）。"""

    changed = Signal()

    def __init__(self, value: float = 0.3, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(value * 100)))
        self.slider.valueChanged.connect(self._on_changed)
        self.label = QLabel(self)
        self.label.setObjectName("ValueLabel")
        self.label.setFixedWidth(48)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._sync_label()
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)

    def _sync_label(self) -> None:
        self.label.setText("%d%%" % self.slider.value())

    def _on_changed(self, _value: int) -> None:
        self._sync_label()
        self.changed.emit()

    def value(self) -> float:
        return self.slider.value() / 100.0

    def set_value(self, value: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(max(0.0, min(1.0, float(value))) * 100)))
        self.slider.blockSignals(False)
        self._sync_label()


__all__ = [
    "ProbabilityRow",
    "TimeListEdit",
    "add_form_row",
    "danger_button",
    "ghost_button",
    "hint_label",
    "make_group",
    "primary_button",
    "section_title",
    "set_variant",
    "switch_checkbox",
    "switch_row",
]
