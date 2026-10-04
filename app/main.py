"""GUI 入口。

开发环境::

    python -m app.main

打包后为 ``BaiAi-Tavern.exe``。同一个 EXE 也负责卸载（不需要单独的卸载程序）::

    BaiAi-Tavern.exe --uninstall            # 图形确认后卸载
    BaiAi-Tavern.exe --uninstall --silent   # 静默卸载（「设置 → 应用」里的快速卸载）
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import List, Optional

from common.logging_setup import get_logger, setup_logging
from common.paths import ensure_dirs

from . import APP_AUTHOR, APP_DISPLAY_NAME, APP_HOMEPAGE, APP_NAME, APP_VERSION_DISPLAY
from .config_store import gui_config
from .context import AppContext
from .icons import app_icon
from .main_window import MainWindow
from .theme import apply_theme

log = get_logger("app.main")

_shared_memory = None  # 单实例锁必须保持引用，否则会被回收


def _install_excepthook(logger) -> None:
    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("未捕获异常:\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                _show_crash_dialog(text)
                return
            QMessageBox.critical(
                None,
                "BaiAi-Tavern 出现异常",
                "程序遇到未处理的错误，已记录到日志：\n\n%s" % text[-1200:],
            )
        except Exception:
            pass

    sys.excepthook = _hook


def _show_crash_dialog(text: str) -> None:
    """未处理异常对话框：完整堆栈 + 日志路径 + 一键复制。

    内测阶段用户常常只截到一行 ``OverflowError: ...``，看不出出事的位置；
    这里把堆栈放进可滚动、可复制的文本框，反馈就能直接定位。
    """
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QPlainTextEdit,
        QVBoxLayout,
    )

    from common.paths import logs_dir

    log_path = logs_dir() / "gui.log"
    dialog = QDialog()
    dialog.setWindowTitle("%s 出现异常" % APP_DISPLAY_NAME)
    dialog.resize(780, 470)
    layout = QVBoxLayout(dialog)
    hint = QLabel(
        "程序遇到未处理的错误。界面会继续运行，但相关功能可能不正常。\n"
        "把下面这段内容（或日志文件）发给开发者可以快速定位：%s" % log_path,
        dialog,
    )
    hint.setWordWrap(True)
    layout.addWidget(hint)
    view = QPlainTextEdit(dialog)
    view.setReadOnly(True)
    view.setPlainText("%s\n日志文件：%s" % (text.strip(), log_path))
    layout.addWidget(view, 1)
    buttons = QDialogButtonBox(dialog)
    copy_button = buttons.addButton("复制详情", QDialogButtonBox.ActionRole)
    buttons.addButton("关闭", QDialogButtonBox.AcceptRole)
    copy_button.clicked.connect(lambda: QApplication.clipboard().setText(view.toPlainText()))
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


def run_uninstall(silent: bool = False, remove_data: bool = False) -> int:
    """卸载自己（安装目录里的主程序被调用时走这里）。

    正在运行的程序删不掉自己：真正删除由 ``installer.common`` 安排一个延迟批处理完成，
    本函数负责确认、清理并尽快退出进程。
    """
    from installer import common as ic

    install_dir = ic.install_dir_for_self()
    if not silent:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            QApplication(sys.argv[:1])
        ask = QMessageBox(None)
        ask.setWindowTitle("卸载 %s" % APP_DISPLAY_NAME)
        ask.setIcon(QMessageBox.Question)
        ask.setText("要卸载 %s %s 吗？" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY))
        ask.setInformativeText(
            "将删除程序文件与快捷方式，并从「设置 → 应用」里移除。\n\n"
            "用户数据（data 目录、config.yaml）会保留。"
        )
        keep_box = ask.addButton("卸载（保留数据）", QMessageBox.AcceptRole)
        wipe_box = ask.addButton("卸载并删除数据", QMessageBox.DestructiveRole)
        cancel_box = ask.addButton("取消", QMessageBox.RejectRole)
        ask.setDefaultButton(keep_box)
        ask.exec()
        clicked = ask.clickedButton()
        if clicked is cancel_box:
            return 0
        remove_data = clicked is wipe_box

    result = ic.uninstall(
        install_dir=install_dir,
        remove_data=remove_data,
        self_exe=ic.self_exe_path(),
    )
    if not result.get("ok"):
        log.error("卸载失败：%s", result.get("error"))
        if not silent:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, "卸载失败", str(result.get("error")))
        return 1
    log.info(
        "已卸载（数据%s），程序文件将由延迟任务清理",
        "已删除" if remove_data else "保留",
    )
    if not silent:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.setWindowTitle("卸载完成")
        box.setIcon(QMessageBox.Information)
        box.setText("%s 已卸载。" % APP_DISPLAY_NAME)
        box.setInformativeText(
            "程序文件与快捷方式已清理。\n%s"
            % ("用户数据（data 目录、config.yaml）已保留。" if result.get("data_kept", True) else "用户数据也已删除。")
        )
        box.exec()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BaiAi-Tavern 桌面客户端")
    parser.add_argument("--minimized", action="store_true", help="启动后直接最小化到托盘")
    parser.add_argument("--no-autostart", action="store_true", help="本次启动不自动拉起 Bot")
    parser.add_argument("--uninstall", action="store_true", help="卸载本程序（也可由「设置 → 应用」调用）")
    parser.add_argument("--silent", action="store_true", help="配合 --uninstall：不弹确认框")
    parser.add_argument("--remove-data", action="store_true", help="配合 --uninstall：同时删除配置与数据")
    args = parser.parse_args(argv)

    ensure_dirs()
    config = gui_config()
    setup_logging(
        name="gui",
        level=str(config.get("logging.level", "INFO") or "INFO"),
        console=True,
        max_bytes=int(config.get("logging.max_bytes", 2097152) or 2097152),
        backup_count=int(config.get("logging.backup_count", 3) or 3),
    )
    log.info("=" * 72)
    log.info("%s %s（GUI）启动中… 作者：%s %s", APP_DISPLAY_NAME, APP_VERSION_DISPLAY, APP_AUTHOR, APP_HOMEPAGE)

    if args.uninstall:
        # 卸载走独立的轻量路径：不启动 Bot、不建主窗口、不抢单实例锁
        return run_uninstall(silent=args.silent, remove_data=args.remove_data)

    from PySide6.QtCore import QSharedMemory
    from PySide6.QtWidgets import QApplication, QMessageBox

    global _shared_memory
    _shared_memory = QSharedMemory("BaiAi-Tavern-SingleInstance")

    app = QApplication(sys.argv[:1] if argv is None else [sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("%s %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY))
    app.setApplicationVersion(APP_VERSION_DISPLAY)
    app.setOrganizationName(APP_AUTHOR)
    app.setOrganizationDomain(APP_HOMEPAGE)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    if not _shared_memory.create(1):
        QMessageBox.information(
            None,
            "%s %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY),
            "%s 已经在运行了。\n\n如果没有看到窗口，请检查系统托盘图标。" % APP_DISPLAY_NAME,
        )
        return 0

    _install_excepthook(log)

    theme = apply_theme(app, str(config.get("app.theme", "dark") or "dark"))
    log.info("主题：%s　数据目录：%s", theme, config.path.parent)

    context = AppContext(config)
    if args.no_autostart:
        config.set("app.start_bot_on_launch", False)

    window = MainWindow(context, allow_onboarding=not args.minimized)
    start_minimized = bool(args.minimized) or bool(config.get("app.start_minimized", False))
    if start_minimized:
        window.hide()
        window.tray.notify(APP_DISPLAY_NAME, "已在后台启动，双击托盘图标可打开主窗口。", "message")
    else:
        window.show()

    exit_code = app.exec()
    log.info("%s 已退出（code=%s）", APP_DISPLAY_NAME, exit_code)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
