"""系统托盘（G-23 ~ G-26）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from common.logging_setup import get_logger

from .icons import tray_icon
from .uikit import icon

log = get_logger("app.tray")


class TrayIcon(QSystemTrayIcon):
    show_requested = Signal()
    start_bot_requested = Signal()
    stop_bot_requested = Signal()
    trigger_requested = Signal()
    logs_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.running = False
        self.setIcon(tray_icon(False))
        self.setToolTip("BaiAi-Tavern · Bot 已停止")

        menu = QMenu()
        self.action_show = QAction(icon("chart"), "显示主窗口", menu)
        self.action_show.triggered.connect(self.show_requested.emit)
        menu.addAction(self.action_show)

        menu.addSeparator()
        self.action_start = QAction(icon("play"), "启动 Bot", menu)
        self.action_start.triggered.connect(self.start_bot_requested.emit)
        menu.addAction(self.action_start)
        self.action_stop = QAction(icon("stop"), "停止 Bot", menu)
        self.action_stop.triggered.connect(self.stop_bot_requested.emit)
        menu.addAction(self.action_stop)
        self.action_trigger = QAction(icon("send"), "立即触发主动消息", menu)
        self.action_trigger.triggered.connect(self.trigger_requested.emit)
        menu.addAction(self.action_trigger)

        menu.addSeparator()
        self.action_logs = QAction(icon("file"), "查看日志", menu)
        self.action_logs.triggered.connect(self.logs_requested.emit)
        menu.addAction(self.action_logs)
        self.action_quit = QAction("退出", menu)
        self.action_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(self.action_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self._menu = menu

    # ---------------------------------------------------------------- 状态
    def set_running(self, running: bool) -> None:
        if running == self.running:
            return
        self.running = bool(running)
        self.setIcon(tray_icon(self.running))
        self.setToolTip("BaiAi-Tavern · Bot %s" % ("运行中" if self.running else "已停止"))
        self.action_start.setEnabled(not self.running)
        self.action_stop.setEnabled(self.running)

    def notify(self, title: str, message: str, level: str = "info") -> None:
        icon = {
            "error": QSystemTrayIcon.Critical,
            "warn": QSystemTrayIcon.Warning,
            "message": QSystemTrayIcon.Information,
        }.get(level, QSystemTrayIcon.Information)
        if self.supportsMessages():
            self.showMessage(title, message, icon, 6000)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_requested.emit()


__all__ = ["TrayIcon"]
