"""安装 · 更新 · 卸载一体界面。

* :class:`InstallUpdateDialog`：应用生命周期一站式窗口——
  更新（检查 / 下载安装 / 进度 / 跳过策略）、安装信息（目录 / 类型 / 快捷方式 / 重装修复）、
  卸载（保留数据 / 删除数据）、关于与开源项目清单。
* :class:`UpdateNoticeDialog`：启动时发现新版本的轻量提醒，
  给「立即更新 / 跳过此版本 / 不再提示 / 稍后再说」四个出口。

更新链路（详见 :mod:`app.updater`）：GitHub Releases 拉最新安装包 →
SHA256 校验（有 SHA256SUMS.txt 时）→ 静默装到当前安装目录 → 自动启动新版本。
"""

from __future__ import annotations

import os
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger

from . import APP_AUTHOR, APP_DISPLAY_NAME, APP_HOMEPAGE, APP_VERSION_DISPLAY
from .about import OPEN_SOURCE_PROJECTS, open_source_text
from .config_store import save_config
from .icons import app_icon, app_pixmap
from . import updater
from .widgets.fields import danger_button, ghost_button, hint_label, primary_button, section_title

log = get_logger("app.lifecycle")


def _mb(num: float) -> str:
    return "%.1fMB" % (num / 1024 / 1024)


def _release_note_excerpt(body: Any, limit: int = 420) -> str:
    """Release 说明转成纯文本摘要（去掉 Markdown 图片 / 链接标签，保留可读文字）。"""
    text = str(body or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`+", "", text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith(("---", "###", "##", "#"))]
    joined = "\n".join(lines)
    if len(joined) > limit:
        joined = joined[:limit].rstrip() + " …"
    return joined.strip()


class _DownloadNotifier(QObject):
    """下载进度从工作线程 → 主线程（跨线程信号自动走队列）。"""

    progress = Signal(int, int)  # 已下载字节, 总字节(0=未知)


class InstallUpdateDialog(QDialog):
    """安装 / 更新 / 卸载 一体窗口（非模态）。"""

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self._latest_info: Optional[Dict[str, Any]] = None
        self._busy = False
        self._install_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))

        self.setWindowTitle("%s · 安装与更新" % APP_DISPLAY_NAME)
        self.setModal(False)
        self.setMinimumSize(700, 660)
        self.resize(740, 800)
        self.setWindowIcon(app_icon())

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        root.addWidget(self._build_header())

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget(scroll)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 0, 10, 4)
        layout.setSpacing(12)
        layout.addWidget(self._build_update_card())
        layout.addWidget(self._build_install_card())
        layout.addWidget(self._build_uninstall_card())
        layout.addWidget(self._build_about_section())
        layout.addStretch(1)
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        self._notifier = _DownloadNotifier(self)
        self._notifier.progress.connect(self._on_download_progress)

        self.refresh_install_info()
        self._sync_update_controls()
        self.check_for_update()

    # ============================================================== 布局
    def _build_header(self) -> QWidget:
        head = QWidget(self)
        layout = QHBoxLayout(head)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        icon = QLabel(head)
        icon.setPixmap(app_pixmap(52))
        layout.addWidget(icon)
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)
        name = QLabel("%s  %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY), head)
        name.setObjectName("PageTitle")
        titles.addWidget(name)
        author = QLabel("作者：%s　·　%s" % (APP_AUTHOR, APP_HOMEPAGE), head)
        author.setObjectName("PageSubtitle")
        author.setTextInteractionFlags(Qt.TextSelectableByMouse)
        titles.addWidget(author)
        layout.addLayout(titles, 1)
        return head

    def _card(self, container: QWidget, title: str) -> QVBoxLayout:
        card = QFrame(container)
        card.setObjectName("OnbCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)
        heading = QLabel(title, card)
        heading.setObjectName("SectionTitle")
        card_layout.addWidget(heading)
        container.addWidget(card)
        return card_layout

    def _build_update_card(self) -> QWidget:
        card = QWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(section_title("更新（从 GitHub Releases 自动获取）"))

        self.lbl_update_state = QLabel("还没检查过。点「检查更新」试试，或等下次启动自动检查。", card)
        self.lbl_update_state.setObjectName("OnbMuted")
        self.lbl_update_state.setWordWrap(True)
        layout.addWidget(self.lbl_update_state)

        self.lbl_release_note = QLabel("", card)
        self.lbl_release_note.setObjectName("OnbText")
        self.lbl_release_note.setWordWrap(True)
        self.lbl_release_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_release_note.setVisible(False)
        layout.addWidget(self.lbl_release_note)

        self.progress = QProgressBar(card)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(14)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.lbl_progress = QLabel("", card)
        self.lbl_progress.setObjectName("OnbMuted")
        self.lbl_progress.setVisible(False)
        layout.addWidget(self.lbl_progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_check = primary_button("检查更新", card)
        self.btn_check.clicked.connect(self.check_for_update)
        self.btn_update = ghost_button("立即更新", card)
        self.btn_update.setEnabled(False)
        self.btn_update.clicked.connect(self.start_update)
        self.btn_releases = ghost_button("打开 Releases 页面", card)
        self.btn_releases.clicked.connect(lambda: webbrowser.open(updater.RELEASES_URL))
        buttons.addWidget(self.btn_check)
        buttons.addWidget(self.btn_update)
        buttons.addWidget(self.btn_releases)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        option_row = QHBoxLayout()
        option_row.setSpacing(8)
        self.chk_auto = QCheckBox("启动时自动检查更新", card)
        self.chk_auto.toggled.connect(self._on_auto_check_toggled)
        option_row.addWidget(self.chk_auto)
        self.lbl_skip = hint_label("", card)
        self.lbl_skip.setVisible(False)
        option_row.addWidget(self.lbl_skip, 1)
        self.btn_unskip = ghost_button("恢复提示", card)
        self.btn_unskip.setVisible(False)
        self.btn_unskip.clicked.connect(self._clear_skipped_version)
        option_row.addWidget(self.btn_unskip)
        layout.addLayout(option_row)

        layout.addWidget(
            hint_label(
                "更新 = 下载最新版安装包并静默装到下面的安装位置，装完自动重启为新版；"
                "配置与聊天数据（data 目录、config.yaml）都会保留。",
                card,
            )
        )
        return card

    def _build_install_card(self) -> QWidget:
        card = QWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(section_title("安装"))

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_installed_version = QLabel("-", card)
        self.lbl_install_type = QLabel("-", card)
        self.lbl_shortcuts = QLabel("-", card)
        form.addRow("当前版本", self.lbl_installed_version)
        form.addRow("安装位置", self.lbl_install_type)
        form.addRow("快捷方式", self.lbl_shortcuts)
        layout.addLayout(form)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        self.edit_target_dir = QLineEdit(card)
        self.edit_target_dir.setPlaceholderText("更新 / 重装的目标位置（默认就是上面的安装位置）")
        btn_browse = ghost_button("选择…", card)
        btn_browse.clicked.connect(self._browse_target_dir)
        dir_row.addWidget(self.edit_target_dir, 1)
        dir_row.addWidget(btn_browse)
        layout.addLayout(dir_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_open_dir = ghost_button("打开安装位置", card)
        self.btn_open_dir.clicked.connect(self.open_install_dir)
        buttons.addWidget(self.btn_open_dir)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card

    def _build_uninstall_card(self) -> QWidget:
        card = QWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(section_title("卸载"))
        layout.addWidget(
            hint_label(
                "卸载会删除程序文件与快捷方式，并从「设置 → 应用」里移除；"
                "配置与聊天数据默认保留（存在安装位置的 data 目录与 config.yaml）。",
                card,
            )
        )
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_uninstall = ghost_button("卸载（保留数据）", card)
        self.btn_uninstall.clicked.connect(lambda: self.uninstall(remove_data=False))
        self.btn_uninstall_wipe = danger_button("卸载并删除数据", card)
        self.btn_uninstall_wipe.clicked.connect(lambda: self.uninstall(remove_data=True))
        buttons.addWidget(self.btn_uninstall)
        buttons.addWidget(self.btn_uninstall_wipe)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card

    def _build_about_section(self) -> QWidget:
        card = QWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(section_title("关于"), 0, Qt.AlignTop)
        btn_copy = ghost_button("复制开源清单", card)
        btn_copy.clicked.connect(self.copy_open_source_list)
        row.addStretch(1)
        row.addWidget(btn_copy)
        layout.addLayout(row)

        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(200)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget(scroll)
        list_layout = QVBoxLayout(container)
        list_layout.setContentsMargins(10, 8, 10, 8)
        list_layout.setSpacing(6)
        for index, (name_text, usage, url) in enumerate(OPEN_SOURCE_PROJECTS, 1):
            item = QLabel(
                '<b>%d. %s</b> —— %s<br/>'
                '<a href="%s" style="color:#6f9bff; text-decoration:none;">%s</a>'
                % (index, name_text, usage, url, url),
                container,
            )
            item.setObjectName("OnbText")
            item.setWordWrap(True)
            item.setTextInteractionFlags(Qt.TextBrowserInteraction)
            item.setOpenExternalLinks(True)
            list_layout.addWidget(item)
        list_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll)
        return card

    # ============================================================== 安装信息
    def refresh_install_info(self) -> None:
        """从注册表 / 进程位置读取安装信息（同步、毫秒级，可在主线程跑）。"""
        from installer import common as ic

        entry = ic.read_uninstall_entry()
        self._install_dir = ic.install_dir_for_self()
        frozen = bool(getattr(sys, "frozen", False))
        installed = bool(entry) and bool((Path(str(entry.get("InstallLocation") or self._install_dir)) / ic.EXE_NAME).exists())

        version = str(entry.get("DisplayVersion") or "")
        self.lbl_installed_version.setText(
            "%s（安装信息登记 %s）" % (APP_VERSION_DISPLAY, version or "无") if installed else APP_VERSION_DISPLAY
        )
        if installed:
            self.lbl_install_type.setText("已安装：%s" % self._install_dir)
        elif frozen:
            self.lbl_install_type.setText("绿色版：%s（未通过安装包安装）" % self._install_dir)
        else:
            self.lbl_install_type.setText("源码运行（%s）" % self._install_dir)

        shortcut_paths = [item for item in str(entry.get("Shortcuts") or "").split(";") if item.strip()]
        alive = sum(1 for item in shortcut_paths if Path(item).exists())
        if shortcut_paths:
            self.lbl_shortcuts.setText("%d 个（%d 个有效）" % (len(shortcut_paths), alive))
        else:
            self.lbl_shortcuts.setText("未创建（绿色版或源码运行）")

        current = self.edit_target_dir.text().strip()
        if not current or current == str(self.edit_target_dir.placeholderText() or ""):
            self.edit_target_dir.setText(str(self._install_dir))

    def open_install_dir(self) -> None:
        try:
            os.startfile(str(self._install_dir))  # type: ignore[attr-defined]
        except Exception as exc:
            QMessageBox.information(self, "打开失败", "找不到安装位置：%s" % exc)

    def _browse_target_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择更新 / 重装的目标位置", str(self.edit_target_dir.text().strip() or self._install_dir)
        )
        if chosen:
            self.edit_target_dir.setText(chosen)

    def _target_dir(self) -> Path:
        text = self.edit_target_dir.text().strip()
        return Path(text) if text else self._install_dir

    # ============================================================== 更新
    def check_for_update(self) -> None:
        self._set_update_state("正在检查更新…", "muted")
        self._set_buttons_busy(True)

        def _work() -> Dict[str, Any]:
            return updater.check_latest(APP_VERSION_DISPLAY)

        self.ctx.run_task(
            _work,
            on_ok=self._on_checked,
            on_error=self._on_check_failed,
            key="lifecycle_check",
            label="检查更新",
        )

    def _on_checked(self, info: Any) -> None:
        info = info if isinstance(info, dict) else {}
        self._latest_info = info or None
        try:
            updater.record_check(self.ctx.config)
        except Exception:
            pass
        if self._latest_info is None:
            self._set_update_state("检查失败：没拿到 Release 信息", "bad")
            self._set_buttons_busy(False)
            return
        note = _release_note_excerpt(self._latest_info.get("release", {}).get("body") if isinstance(self._latest_info.get("release"), dict) else "")
        if note:
            self.lbl_release_note.setText("Release 说明：\n%s" % note)
            self.lbl_release_note.setVisible(True)
        if info.get("newer"):
            if info.get("asset") is None:
                self._set_update_state(
                    "有新版本 %s，但这个 Release 里没有安装包附件（BaiAi-Tavern*.exe）；"
                    "可以点「打开 Releases 页面」手动下载。" % info.get("latest_display"),
                    "warn",
                )
            else:
                self._set_update_state(
                    "有新版本 %s（当前 %s）。点「立即更新」下载安装。"
                    % (info.get("latest_display"), info.get("current_display")),
                    "ok",
                )
        else:
            self._set_update_state("已是最新（%s）。" % info.get("current_display", APP_VERSION_DISPLAY), "ok")
        self._sync_update_controls()
        self._set_buttons_busy(False)

    def _on_check_failed(self, message: str) -> None:
        self._set_update_state("检查失败：%s\n也可以点「打开 Releases 页面」手动下载。" % message, "bad")
        self._sync_update_controls()
        self._set_buttons_busy(False)

    def _sync_update_controls(self) -> None:
        info = self._latest_info or {}
        newer = bool(info.get("newer")) and info.get("asset") is not None
        has_asset = info.get("asset") is not None
        self.btn_update.setEnabled(bool(has_asset) and not self._busy)
        self.btn_update.setText("立即更新" if newer else "重装 / 修复")
        self.chk_auto.setChecked(bool(self.ctx.config.get("app.update_check_enabled", True)))

        skipped = str(self.ctx.config.get("app.update_skipped_version", "") or "")
        latest = str(info.get("latest_display") or "")
        if skipped and latest and updater.normalize_version(skipped) == latest:
            self.lbl_skip.setText("正在跳过版本 %s（启动时不再提示）。" % latest)
            self.lbl_skip.setVisible(True)
            self.btn_unskip.setVisible(True)
        else:
            self.lbl_skip.setVisible(False)
            self.btn_unskip.setVisible(False)

    def _clear_skipped_version(self) -> None:
        save_config(self.ctx.config, {"app": {"update_skipped_version": ""}})
        self._sync_update_controls()

    def _on_auto_check_toggled(self, checked: bool) -> None:
        save_config(self.ctx.config, {"app": {"update_check_enabled": bool(checked)}})

    # ---------------------------------------------------------------- 下载安装
    def start_update(self) -> None:
        if self._busy:
            return
        info = self._latest_info or {}
        if not info.get("asset"):
            QMessageBox.information(self, "没有可安装的文件", "这个 Release 里没有安装包附件，先点「检查更新」或去 Releases 页面下载。")
            return
        newer = bool(info.get("newer"))
        target = self._target_dir()
        confirm = QMessageBox.question(
            self,
            "确认%s" % ("更新" if newer else "重装"),
            "将下载 %s 并安装到：\n%s\n\n配置与聊天数据会保留。继续吗？"
            % (info.get("latest_display", "最新版"), target),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return

        self._busy = True
        self._set_buttons_busy(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_progress.setText("正在下载安装包…")
        self.lbl_progress.setVisible(True)
        self._set_update_state("正在下载安装包（%s）…" % info.get("latest_display", "最新版"), "muted")

        def _work() -> Dict[str, Any]:
            asset = info["asset"]
            path = updater.download(
                str(asset.get("browser_download_url") or ""),
                progress=lambda r, t: self._notifier.progress.emit(int(r or 0), int(t or 0)),
            )
            verified = False
            sums = info.get("sums_asset")
            if isinstance(sums, dict) and sums.get("browser_download_url"):
                try:
                    sums_path = updater.download(str(sums["browser_download_url"]))
                    sums_text = sums_path.read_text(encoding="utf-8", errors="replace")
                    ok = updater.verify_sha256(path, sums_text)
                    if ok is False:
                        raise updater.UpdateError("SHA256 校验失败：下载的文件与官方校验值不一致，请再试一次")
                    verified = ok is not False
                except updater.UpdateError:
                    raise
                except Exception as exc:
                    log.debug("SHA256 校验跳过：%s", exc)
            return {"installer": str(path), "verified": verified, "size": path.stat().st_size}

        self.ctx.run_task(_work, on_ok=self._on_download_done, on_error=self._on_download_failed, key="update_download", label="下载安装包")

    def _on_download_progress(self, received: int, total: int) -> None:
        if total and total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(received * 100 / total)))
            self.lbl_progress.setText("正在下载… %s / %s" % (_mb(received), _mb(total)))
        else:
            self.progress.setRange(0, 0)
            self.lbl_progress.setText("正在下载… %s" % _mb(received))

    def _on_download_done(self, result: Any) -> None:
        result = result if isinstance(result, dict) else {}
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._begin_install(result)

    def _on_download_failed(self, message: str) -> None:
        self._busy = False
        self.progress.setRange(0, 100)
        self.lbl_progress.setText("")
        self._set_update_state("下载失败：%s\n可以稍后再试，或去 Releases 页面手动下载。" % message, "bad")
        self._set_buttons_busy(False)

    def _begin_install(self, result: Dict[str, Any]) -> None:
        installer = Path(str(result.get("installer") or ""))
        target = self._target_dir()
        extra = "（SHA256 已校验）" if result.get("verified") else ""
        self._set_update_state("正在安装%s… 程序会先停 Bot、随后自动重启为新版。" % extra, "ok")
        self.lbl_progress.setText("正在静默安装…")

        def _work() -> bool:
            try:
                self.ctx.api.shutdown()
            except Exception:
                pass
            for _ in range(12):
                if not self.ctx.bot.is_running():
                    break
                time.sleep(0.3)
            updater.launch_installer(installer, target, launch_after=True)
            return True

        self.ctx.run_task(
            _work,
            on_ok=lambda _r: QTimer.singleShot(1500, self._quit_app),
            on_error=lambda message: self._on_install_failed(message),
            key="update_install",
            label="静默安装新版本",
        )

    def _on_install_failed(self, message: str) -> None:
        self._busy = False
        self.lbl_progress.setText("")
        self._set_update_state("安装启动失败：%s\n旧版本还在运行，可以重试或去 Releases 页面手动下载。" % message, "bad")
        self._set_buttons_busy(False)

    # ---------------------------------------------------------------- 卸载
    def uninstall(self, remove_data: bool) -> None:
        if self._busy:
            return
        confirm = QMessageBox.question(
            self,
            "确认卸载",
            "确定要卸载 %s %s 吗？\n\n%s"
            % (
                APP_DISPLAY_NAME,
                APP_VERSION_DISPLAY,
                "配置与聊天数据（data 目录、config.yaml）会**删除**，且无法恢复。"
                if remove_data
                else "配置与聊天数据（data 目录、config.yaml）会保留。",
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._busy = True
        self._set_buttons_busy(True)
        self._set_update_state("正在卸载…", "muted")

        def _work() -> Dict[str, Any]:
            from installer import common as ic

            return ic.uninstall(
                install_dir=self._install_dir,
                remove_data=remove_data,
                self_exe=ic.self_exe_path(),
            )

        self.ctx.run_task(
            _work,
            on_ok=lambda _r: QTimer.singleShot(600, self._quit_app),
            on_error=lambda message: self._on_uninstall_failed(message),
            key="uninstall",
            label="卸载",
        )

    def _on_uninstall_failed(self, message: str) -> None:
        self._busy = False
        self._set_update_state("卸载失败：%s" % message, "bad")
        self._set_buttons_busy(False)
        QMessageBox.warning(self, "卸载失败", message)

    # ---------------------------------------------------------------- 杂项
    def copy_open_source_list(self) -> None:
        try:
            from PySide6.QtGui import QGuiApplication

            QGuiApplication.clipboard().setText(open_source_text())
        except Exception as exc:  # pragma: no cover
            log.debug("复制开源清单失败：%s", exc)

    def _set_update_state(self, text: str, level: str) -> None:
        name = {"ok": "OnbOk", "warn": "OnbWarn", "bad": "OnbBad"}.get(level, "OnbMuted")
        if self.lbl_update_state.objectName() != name:
            self.lbl_update_state.setObjectName(name)
            self.lbl_update_state.style().unpolish(self.lbl_update_state)
            self.lbl_update_state.style().polish(self.lbl_update_state)
        self.lbl_update_state.setText(text)

    def _set_buttons_busy(self, busy: bool) -> None:
        for button in (self.btn_check, self.btn_update, self.btn_open_dir, self.btn_uninstall, self.btn_uninstall_wipe, self.btn_unskip):
            if busy:
                button.setEnabled(False)
            elif button is self.btn_update:
                self._sync_update_controls()
            else:
                button.setEnabled(True)
        self.chk_auto.setEnabled(not busy)

    def _quit_app(self) -> None:
        """更新 / 卸载完成后退出：优先走主窗口的收尾（停轮询、收托盘）。"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "_finalize_quit"):
            parent = parent.parent()
        if parent is not None:
            try:
                parent._finalize_quit(stop_bot=False)
                return
            except Exception:
                pass
        try:
            self.ctx.shutdown()
        except Exception:
            pass
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()


class UpdateNoticeDialog(QDialog):
    """启动检查发现新版本时的提醒：立即更新 / 跳过此版本 / 不再提示 / 稍后再说。"""

    open_update_requested = Signal()

    def __init__(self, ctx, info: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.info = info or {}
        self.setWindowTitle("发现新版本")
        self.setModal(False)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.resize(560, 360)
        self.setWindowIcon(app_icon())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("有新版本：%s" % self.info.get("latest_display", ""), self)
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        sub = QLabel(
            "当前版本 %s。从 GitHub Releases 下载安装包并静默安装，配置与数据都会保留。"
            % self.info.get("current_display", APP_VERSION_DISPLAY),
            self,
        )
        sub.setObjectName("OnbText")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        note = _release_note_excerpt(self.info.get("release", {}).get("body") if isinstance(self.info.get("release"), dict) else "", limit=260)
        if note:
            note_label = QLabel("本版本更新内容：\n%s" % note, self)
            note_label.setObjectName("OnbMuted")
            note_label.setWordWrap(True)
            layout.addWidget(note_label)

        layout.addWidget(hint_label("点「打开 Releases 页面」可以看完整更新说明。", self))

        buttons = QDialogButtonBox(self)
        self.btn_update = buttons.addButton("立即更新", QDialogButtonBox.AcceptRole)
        self.btn_skip = buttons.addButton("跳过此版本", QDialogButtonBox.RejectRole)
        self.btn_never = buttons.addButton("不再提示", QDialogButtonBox.RejectRole)
        self.btn_later = buttons.addButton("稍后再说", QDialogButtonBox.RejectRole)
        buttons.clicked.connect(self._on_clicked)
        layout.addWidget(buttons)

    def _on_clicked(self, button: Optional[QDialogButtonBox]) -> None:
        if button is self.btn_update:
            self.open_update_requested.emit()
            self.accept()
        elif button is self.btn_skip:
            save_config(self.ctx.config, {"app": {"update_skipped_version": str(self.info.get("latest_tag") or "")}})
            log.info("用户跳过版本 %s 的更新提示", self.info.get("latest_display"))
            self.accept()
        elif button is self.btn_never:
            save_config(self.ctx.config, {"app": {"update_check_enabled": False}})
            log.info("用户选择「不再提示」：启动时不再自动检查更新")
            self.accept()
        else:
            self.accept()


__all__ = ["InstallUpdateDialog", "UpdateNoticeDialog"]
