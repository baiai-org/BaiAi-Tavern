"""日志查看页面（G-21）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from common.logging_setup import tail_file
from common.paths import logs_dir

from ..uikit import set_icon
from ..widgets.fields import ghost_button, hint_label
from .base import Page

SOURCES = [("bot", "Bot 日志"), ("gui", "界面日志")]


class LogsPage(Page):
    page_title = "日志"
    page_subtitle = "实时查看 Bot 与界面的运行日志"

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_refresh = ghost_button("刷新")
        self.btn_clear = ghost_button("清空显示")
        self.btn_open = ghost_button("打开日志目录")
        for button, icon_name in (
            (self.btn_refresh, "refresh"),
            (self.btn_clear, "trash"),
            (self.btn_open, "folder"),
        ):
            set_icon(button, icon_name)
            self.add_action(button)
        self.btn_refresh.clicked.connect(self._reload)
        self.btn_clear.clicked.connect(lambda: self.view.setPlainText(""))
        self.btn_open.clicked.connect(self._open_dir)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        toolbar.addWidget(QLabel("来源"))
        self.combo_source = QComboBox()
        for key, label in SOURCES:
            self.combo_source.addItem(label, key)
        self.combo_source.setFixedWidth(120)
        self.combo_source.currentIndexChanged.connect(lambda _i: self._reload())
        toolbar.addWidget(self.combo_source)

        toolbar.addWidget(QLabel("级别"))
        self.combo_level = QComboBox()
        for label in ("全部", "DEBUG", "INFO", "WARNING", "ERROR"):
            self.combo_level.addItem(label, label)
        self.combo_level.setFixedWidth(110)
        self.combo_level.currentIndexChanged.connect(lambda _i: self._reload())
        toolbar.addWidget(self.combo_level)

        toolbar.addWidget(QLabel("行数"))
        self.spin_lines = QSpinBox()
        self.spin_lines.setRange(50, 5000)
        self.spin_lines.setSingleStep(50)
        self.spin_lines.setValue(300)
        self.spin_lines.setFixedWidth(100)
        self.spin_lines.valueChanged.connect(lambda _v: self._reload())
        toolbar.addWidget(self.spin_lines)

        toolbar.addWidget(QLabel("过滤"))
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("关键词，例如 角色名或 QQ 号")
        self.edit_filter.setFixedWidth(220)
        self.edit_filter.textChanged.connect(lambda _t: self._reload())
        toolbar.addWidget(self.edit_filter)

        self.chk_auto = QCheckBox("自动刷新（3 秒）")
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(self._toggle_auto)
        toolbar.addWidget(self.chk_auto)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.view = QPlainTextEdit(self)
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)
        self.view.setMaximumBlockCount(8000)
        layout.addWidget(self.view, 1)

        self.path_label = hint_label("")
        layout.addWidget(self.path_label)

        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._reload)

    # ============================================================== 刷新
    def refresh(self) -> None:
        self._reload()
        if self.chk_auto.isChecked():
            self._timer.start()

    def _toggle_auto(self, checked: bool) -> None:
        if checked:
            self._timer.start()
        else:
            self._timer.stop()

    def _current_path(self) -> Path:
        source = self.combo_source.currentData() or "bot"
        return logs_dir() / ("%s.log" % source)

    def _reload(self) -> None:
        path = self._current_path()
        lines: List[str] = tail_file(path, lines=self.spin_lines.value())
        level = self.combo_level.currentData() or "全部"
        keyword = self.edit_filter.text().strip()

        if level != "全部":
            lines = [line for line in lines if "[%s]" % level in line]
        if keyword:
            lines = [line for line in lines if keyword in line]

        scrollbar = self.view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self.view.setPlainText("\n".join(lines))
        if at_bottom:
            self.view.verticalScrollBar().setValue(scrollbar.maximum())
        self.path_label.setText(
            "日志文件：%s　·　显示 %d 行%s"
            % (path, len(lines), "（文件不存在，等待产生日志）" if not path.exists() else "")
        )

    def _open_dir(self) -> None:
        path = logs_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["explorer", str(path)])

    def on_event(self, event: Dict[str, Any]) -> None:
        if not self.chk_auto.isChecked() and event.get("type") in ("proactive_sent", "reply_sent", "error"):
            self._reload()


__all__ = ["LogsPage"]
