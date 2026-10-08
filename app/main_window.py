"""主窗口：侧边栏导航 + 页面堆叠 + 系统托盘（G-23 ~ G-26）。"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger
from common.utils import truncate

from . import APP_AUTHOR, APP_DISPLAY_NAME, APP_VERSION_DISPLAY
from .about import show_about
from .context import AppContext
from .icons import app_icon
from .onboarding import OnboardingWizard
from .pages import (
    BotsPage,
    CharactersPage,
    ConversationsPage,
    DashboardPage,
    LogsPage,
    ModelsPage,
    Page,
    ProactivePage,
    SettingsPage,
)
from .tray import TrayIcon
from .uikit import icon, set_icon
from .widgets.fields import ghost_button
from .widgets.status_indicator import StatusDot

log = get_logger("app.window")

NAV_ITEMS = [
    ("仪表盘", DashboardPage),
    ("机器人", BotsPage),
    ("角色管理", CharactersPage),
    ("模型路由", ModelsPage),
    ("消息设置", ProactivePage),
    ("对话查看", ConversationsPage),
    ("系统设置", SettingsPage),
    ("日志", LogsPage),
]

# 导航项的关键字（托盘菜单等地方按名字跳转，避免写死下标）
NAV_KEYS = [
    "dashboard",
    "bots",
    "characters",
    "models",
    "proactive",
    "conversations",
    "settings",
    "logs",
]

# 导航图标（用 QPainter 画，不用 emoji：避免系统缺字形时显示成方块）
NAV_ICONS = {
    "dashboard": "chart",
    "bots": "robot",
    "characters": "user",
    "models": "chip",
    "proactive": "message",
    "conversations": "message",
    "settings": "gear",
    "logs": "file",
}

# 默认窗口尺寸：按屏幕大小自适应，但保证足够大（用户反馈过窗口太小导致排版异常）
DEFAULT_WINDOW = (1400, 900)
MIN_WINDOW = (1160, 760)


def preferred_window_size() -> tuple:
    """返回适合当前屏幕的 (宽, 高)。"""
    width, height = DEFAULT_WINDOW
    try:
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = max(MIN_WINDOW[0], min(1720, int(available.width() * 0.86)))
            height = max(MIN_WINDOW[1], min(1080, int(available.height() * 0.88)))
    except Exception:  # pragma: no cover - 无显示环境
        pass
    return int(width), int(height)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, allow_onboarding: bool = True):
        super().__init__()
        self.ctx = ctx
        self.allow_onboarding = allow_onboarding
        self.setWindowTitle("%s %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY))
        self.setWindowIcon(app_icon())
        width, height = preferred_window_size()
        self.resize(width, height)
        self.setMinimumSize(*MIN_WINDOW)

        self._tray_tip_shown = False
        self._started_at = time.time()
        self.onboarding: Optional[OnboardingWizard] = None
        self._install_update_dialog = None
        self._update_notice = None

        self._build_ui()
        self._build_tray()
        self._connect_signals()

        self.ctx.start()
        QTimer.singleShot(900, self._auto_start)
        if allow_onboarding:
            QTimer.singleShot(1200, self.maybe_show_onboarding)
        # 启动后后台检查一次 GitHub 更新（限流 6 小时，「不再提示」时跳过）
        QTimer.singleShot(6000, self.maybe_check_update)

    # ============================================================== UI 构建
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ------------------------------------------------------------ 侧边栏
        sidebar = QWidget(central)
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(212)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        brand = QLabel(APP_DISPLAY_NAME, sidebar)
        brand.setObjectName("SidebarBrand")
        sidebar_layout.addWidget(brand)
        subtitle = QLabel("QQ 多角色 AI 主动陪伴", sidebar)
        subtitle.setObjectName("SidebarSubtitle")
        sidebar_layout.addWidget(subtitle)

        self.nav = QListWidget(sidebar)
        self.nav.setObjectName("NavList")
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setIconSize(QSize(18, 18))
        for index, (label, _page_class) in enumerate(NAV_ITEMS):
            item = QListWidgetItem(label)
            icon_name = NAV_ICONS.get(NAV_KEYS[index] if index < len(NAV_KEYS) else "")
            if icon_name:
                item.setIcon(icon(icon_name))
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        sidebar_layout.addWidget(self.nav, 1)

        self.sidebar_status = QLabel("正在连接 Bot…", sidebar)
        self.sidebar_status.setObjectName("SidebarFooter")
        self.sidebar_status.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_status)

        # 左下角「安装与更新」+「关于」：生命周期入口（版本 / 更新 / 卸载 / 开源项目）
        about_row = QHBoxLayout()
        about_row.setContentsMargins(12, 2, 12, 8)
        about_row.setSpacing(6)
        self.btn_install_update = ghost_button("安装与更新", sidebar)
        set_icon(self.btn_install_update, "refresh")
        self.btn_install_update.setToolTip("版本 / 检查更新 / 重装 / 卸载")
        self.btn_install_update.clicked.connect(self.show_install_update)
        self.btn_about = ghost_button("关于", sidebar)
        set_icon(self.btn_about, "wand")
        self.btn_about.setToolTip("%s %s · %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY, APP_AUTHOR))
        self.btn_about.clicked.connect(lambda: show_about(self))
        about_row.addWidget(self.btn_install_update)
        about_row.addWidget(self.btn_about)
        about_row.addStretch(1)
        sidebar_layout.addLayout(about_row)

        layout.addWidget(sidebar)

        # ------------------------------------------------------------ 页面栈
        self.stack = QStackedWidget(central)
        self.pages: List[Page] = []
        for _label, page_class in NAV_ITEMS:
            page = page_class(self.ctx)
            self.pages.append(page)
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.stack, 1)

        self.setCentralWidget(central)

        # ------------------------------------------------------------ 状态栏
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)
        self.status_dot = StatusDot("idle", 12, status_bar)
        status_bar.addPermanentWidget(self.status_dot)
        self.status_text = QLabel("Bot 未连接")
        self.status_text.setObjectName("MutedLabel")
        status_bar.addPermanentWidget(self.status_text)
        self.qq_text = QLabel("QQ：未登录")
        self.qq_text.setObjectName("MutedLabel")
        status_bar.addPermanentWidget(self.qq_text)

    def _build_tray(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        self.tray = TrayIcon(self)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        else:
            log.warning("系统托盘不可用，关闭窗口将直接退出程序")
        self.tray.show_requested.connect(self.show_window)
        self.tray.start_bot_requested.connect(self._start_bot)
        self.tray.stop_bot_requested.connect(self._stop_bot)
        self.tray.trigger_requested.connect(self._trigger_proactive)
        self.tray.logs_requested.connect(lambda: (self.show_window(), self.show_page_by_key("logs")))
        self.tray.quit_requested.connect(self.quit_app)

    def _connect_signals(self) -> None:
        self.ctx.status_updated.connect(self._on_status)
        self.ctx.notify.connect(self.notify_user)
        self.ctx.event_received.connect(self._on_event)
        self.ctx.bot_state_changed.connect(lambda running: self.tray.set_running(running))
        self.ctx.process_event.connect(self._on_process_event)

    # ============================================================== 页面切换
    def _on_nav_changed(self, index: int) -> None:
        if 0 <= index < len(self.pages):
            self.stack.setCurrentIndex(index)
            page = self.pages[index]
            try:
                page.ensure_loaded()
            except Exception as exc:  # pragma: no cover
                log.warning("页面初始化失败: %s", exc)

    def show_page(self, index: int) -> None:
        if 0 <= index < self.nav.count():
            self.nav.setCurrentRow(index)

    def show_page_by_key(self, key: str) -> None:
        """按关键字跳转页面（托盘菜单用，避免写死下标）。"""
        if key in NAV_KEYS:
            self.show_page(NAV_KEYS.index(key))

    # ============================================================== 状态更新
    def _on_status(self, snapshot: Dict) -> None:
        online = bool(snapshot.get("online"))
        offline = bool(snapshot.get("offline"))
        qq = snapshot.get("qq") or {}

        if online:
            state, text = "ok", "Bot 运行中"
        elif offline:
            state, text = "bad", "Bot 未运行"
        else:
            state, text = "warn", "Bot 启动中"

        self.status_dot.set_state(state)
        self.status_text.setText(text)

        # QQ 连接只有官方机器人一种方式：状态栏直接显示机器人身份
        identity = (
            qq.get("nickname")
            or str(qq.get("app_id") or "")
            or ("未配置" if not qq.get("configured", True) else "")
        )
        if qq.get("connected"):
            self.qq_text.setText("官方机器人：%s" % (identity or "已连接"))
        elif identity:
            self.qq_text.setText("官方机器人：%s（未连接）" % identity)
        else:
            self.qq_text.setText("官方机器人：未配置")

        self.tray.set_running(online)

        uptime = ""
        if online and snapshot.get("started_at"):
            start = str(snapshot["started_at"]).replace("T", " ")
            uptime = "\n启动于 %s" % start[11:19]
        today = (snapshot.get("today") or {}).get("proactive_total", 0)
        target_desc = "主动消息 openid：%s" % (qq.get("target_openid") or "未记录（自动学习）")
        self.sidebar_status.setText(
            "%s\n今日主动消息 %s 条\n%s%s" % (text, today, target_desc, uptime)
        )

    def _on_event(self, event: Dict) -> None:
        kind = str(event.get("type") or "")
        if kind == "chat_activity":
            activity = event.get("kind")
            character = str(event.get("character") or "角色")
            content = str(event.get("content") or "")
            if activity == "proactive" and bool(self.ctx.config.get("app.notify_on_proactive", True)):
                self.tray.notify("「%s」给你发了一条消息" % character, truncate(content, 80), "message")
        elif kind == "proactive_skipped":
            log.info("主动消息被跳过：%s", event.get("reason"))

        for page in self.pages:
            try:
                page.on_event(event)
            except Exception as exc:  # pragma: no cover
                log.debug("页面事件处理失败: %s", exc)

    def _on_process_event(self, name: str, state: str) -> None:
        if state == "running":
            self.statusBar().showMessage("%s 已启动" % name, 4000)
            self.ctx.request_status_refresh()
        elif state == "stopped":
            self.statusBar().showMessage("%s 已停止" % name, 4000)
            self.ctx.request_status_refresh()
        elif state.startswith("failed:"):
            message = state.split(":", 1)[1]
            self.notify_user(name, message, "error")

    # ============================================================== 通知
    def notify_user(self, title: str, message: str, level: str = "info") -> None:
        self.statusBar().showMessage("%s：%s" % (title, message) if title else message, 6000)
        if level in ("error", "warn", "message"):
            self.tray.notify(title, message, level)
        if level == "error":
            log.error("%s: %s", title, message)

    # ============================================================== 配置引导
    def should_onboard(self) -> bool:
        """未配置过（或明确要求）时显示引导。"""
        config = self.ctx.config
        if bool(config.get("app.onboarding_done", False)):
            return False
        if str(config.get("llm.api_key", "") or "").strip() and str(
            config.get("qq.official.app_id", "") or ""
        ).strip():
            # 已经配置好了（例如老用户升级），不再打扰，并补上标记
            try:
                from .config_store import save_config

                save_config(config, {"app": {"onboarding_done": True}})
            except Exception as exc:  # pragma: no cover
                log.debug("写入 onboarding_done 失败：%s", exc)
            return False
        return True

    def maybe_show_onboarding(self) -> None:
        if self.should_onboard():
            self.run_onboarding()
        else:
            self.ctx.request_status_refresh()

    def run_onboarding(self, force: bool = False) -> Optional[OnboardingWizard]:
        """打开配置引导（非阻塞模态对话框）。"""
        if self.onboarding is not None:
            self.onboarding.showNormal()
            self.onboarding.raise_()
            self.onboarding.activateWindow()
            return self.onboarding
        if not force and not (self.allow_onboarding or True):
            return None

        wizard = OnboardingWizard(self.ctx, self)
        wizard.finished_setup.connect(self._on_onboarding_finished)
        wizard.finished.connect(self._on_onboarding_closed)
        self.onboarding = wizard
        wizard.show()
        wizard.raise_()
        wizard.activateWindow()
        log.info("已打开配置引导")
        return wizard

    def _on_onboarding_finished(self, patch: Dict) -> None:
        self.notify_user(
            "配置引导",
            "配置已保存并生效；随时可以在「系统设置」里修改，或点“配置引导”重新运行。",
            "info",
        )
        self.refresh_pages()
        self.ctx.request_status_refresh()

    def _on_onboarding_closed(self, result: int) -> None:
        wizard = self.onboarding
        self.onboarding = None
        if wizard is not None:
            if wizard.skipped:
                self.notify_user(
                    "配置引导",
                    "已跳过引导；可在「系统设置」里手动配置，或点“配置引导”重新打开。",
                    "info",
                )
            wizard.deleteLater()
        self.refresh_pages()
        self.ctx.request_status_refresh()

    def refresh_pages(self) -> None:
        for page in self.pages:
            try:
                page.reload_if_loaded()
            except Exception as exc:  # pragma: no cover
                log.debug("刷新页面失败：%s", exc)

    # ============================================================== 自动启动
    def _auto_start(self) -> None:
        config = self.ctx.config
        if bool(config.get("app.start_bot_on_launch", True)):
            self._start_bot()

    def _start_bot(self) -> None:
        if self.ctx.bot_process_running():
            self.run_api(self.ctx.api.bot_start, "启动 Bot")
            return
        if self.ctx.start_bot_process():
            self.statusBar().showMessage("正在启动 Bot 进程…", 5000)
        else:
            self.notify_user("Bot", "启动失败，请检查是否缺少 bot.exe 或 Python 环境", "error")

    def _stop_bot(self) -> None:
        self.run_api(self.ctx.api.bot_stop, "停止 Bot")

    def _trigger_proactive(self) -> None:
        def _ok(result) -> None:
            if isinstance(result, dict) and not result.get("skipped") and result.get("ok"):
                self.notify_user(
                    "主动消息",
                    "「%s」已发送：%s" % (result.get("character"), truncate(result.get("content"), 40)),
                    "message",
                )
            elif isinstance(result, dict):
                self.notify_user("主动消息", "未发送：%s" % result.get("reason", ""), "warn")
            self.ctx.request_status_refresh()

        self.ctx.run_task(
            self.ctx.api.proactive_trigger,
            None,
            True,
            on_ok=_ok,
            on_error=lambda message: self.notify_user("主动消息", "触发失败：%s" % message, "error"),
            key="manual_trigger",
            label="触发主动消息",
        )

    def run_api(self, fn, label: str) -> None:
        self.ctx.run_task(
            fn,
            on_ok=lambda _result: self.ctx.request_status_refresh(),
            on_error=lambda message: self.notify_user(label, "%s失败：%s" % (label, message), "error"),
            label=label,
        )

    # ============================================================== 更新检查
    def maybe_check_update(self) -> None:
        """启动时后台检查 GitHub Releases 是否有新版本（安静失败，不打扰）。"""
        from . import updater

        config = self.ctx.config
        if not bool(config.get("app.update_check_enabled", True)):
            return  # 用户选了「不再提示」
        if not updater.should_auto_check(config.get):
            return  # 6 小时内查过，不重复查

        def _work():
            return updater.check_latest(APP_VERSION_DISPLAY)

        self.ctx.run_task(
            _work,
            on_ok=self._on_startup_update_check,
            on_error=lambda message: log.debug("启动更新检查失败（忽略）：%s", message),
            key="startup_update_check",
            label="启动时检查更新",
        )

    def _on_startup_update_check(self, info: Dict) -> None:
        from . import updater

        try:
            updater.record_check(self.ctx.config)
        except Exception:
            pass
        if not isinstance(info, dict) or not info.get("newer"):
            return
        skipped = str(self.ctx.config.get("app.update_skipped_version", "") or "")
        if skipped and updater.normalize_version(skipped) == str(info.get("latest_display") or ""):
            return  # 用户跳过的是这个版本
        if self.onboarding is not None:
            return  # 配置引导还没走完，不叠加弹窗；下次启动再提示

        self._show_update_notice(info)

    def _show_update_notice(self, info: Dict) -> None:
        from .lifecycle import UpdateNoticeDialog

        notice = UpdateNoticeDialog(self.ctx, info, self)
        notice.open_update_requested.connect(self.show_install_update)
        notice.finished.connect(lambda _r: notice.deleteLater())
        self._update_notice = notice
        notice.show()
        notice.raise_()
        notice.activateWindow()

    def show_install_update(self) -> None:
        """打开「安装与更新」一体窗口（重复调用时复用同一个窗口）。"""
        from .lifecycle import InstallUpdateDialog

        existing = self._install_update_dialog
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = InstallUpdateDialog(self.ctx, self)
        dialog.finished.connect(self._on_install_update_closed)
        self._install_update_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_install_update_closed(self, _result: int) -> None:
        dialog = self._install_update_dialog
        self._install_update_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    # ============================================================== 窗口行为
    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self) -> None:
        self.hide()
        if not self._tray_tip_shown:
            self._tray_tip_shown = True
            self.tray.notify(
                APP_DISPLAY_NAME,
                "已最小化到系统托盘，Bot 会继续在后台运行。双击托盘图标可以重新打开窗口。",
                "message",
            )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if bool(self.ctx.config.get("app.close_to_tray", True)) and self.tray.isVisible():
            event.ignore()
            self.hide_to_tray()
            return
        event.ignore()
        self.quit_app()

    def changeEvent(self, event) -> None:  # noqa: N802
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and bool(self.ctx.config.get("app.minimize_to_tray", True))
            and self.tray.isVisible()
        ):
            QTimer.singleShot(0, self.hide_to_tray)
        super().changeEvent(event)

    # ============================================================== 退出
    def quit_app(self) -> None:
        if self.ctx.bot.is_running():
            box = QMessageBox(self)
            box.setWindowTitle("退出 BaiAi-Tavern")
            box.setIcon(QMessageBox.Question)
            box.setText("Bot 进程仍在运行，退出时如何处理？")
            stop_button = box.addButton("停止 Bot 并退出", QMessageBox.AcceptRole)
            keep_button = box.addButton("仅退出界面（Bot 继续运行）", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(stop_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked is stop_button:
                self._stop_then_quit()
                return
            if clicked is keep_button:
                self._finalize_quit(stop_bot=False)
                return
            return
        self._finalize_quit(stop_bot=False)

    def _stop_then_quit(self) -> None:
        self.statusBar().showMessage("正在停止 Bot 进程…")

        def _work() -> bool:
            try:
                self.ctx.api.shutdown()
            except Exception:
                pass
            for _ in range(20):
                if not self.ctx.bot.is_running():
                    break
                time.sleep(0.3)
            self.ctx.bot.stop(graceful_timeout=6.0)
            return True

        self.ctx.runner.run(
            _work,
            on_ok=lambda _r: QTimer.singleShot(0, lambda: self._finalize_quit(stop_bot=False)),
            on_error=lambda message: QTimer.singleShot(
                0, lambda: self._finalize_quit(stop_bot=False)
            ),
            label="quit_stop_bot",
        )

    def _finalize_quit(self, stop_bot: bool = False) -> None:
        if stop_bot:
            try:
                self.ctx.bot.stop(graceful_timeout=3.0)
            except Exception:
                pass
        try:
            self.tray.hide()
        except Exception:
            pass
        try:
            self.ctx.shutdown()
        except Exception:
            pass
        log.info("界面退出，累计运行 %.1f 分钟", (time.time() - self._started_at) / 60.0)
        QApplication.instance().quit()


__all__ = ["MainWindow", "NAV_ITEMS", "NAV_KEYS", "preferred_window_size"]
