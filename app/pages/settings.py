"""系统设置页面（G-19 ~ G-22）：LLM、QQ 官方机器人、应用行为与数据目录。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger
from common.paths import data_dir, logs_dir

from .. import autostart
from ..uikit import set_icon
from ..widgets.fields import add_form_row, ghost_button, make_group, primary_button
from ..widgets.llm_form import LLMConfigForm
from ..widgets.qq_form import QQConfigForm
from .base import Page

log = get_logger("app.pages.settings")


class SettingsPage(Page):
    page_title = "系统设置"
    page_subtitle = "LLM 接口、QQ 官方机器人凭据、程序行为与数据目录"
    scrollable = True

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_save = primary_button("保存全部设置")
        self.btn_onboarding = ghost_button("配置引导")
        self.btn_reload = ghost_button("重新载入")
        self.btn_open_config = ghost_button("打开配置文件")
        for button, icon_name in (
            (self.btn_save, "check"),
            (self.btn_onboarding, "wand"),
            (self.btn_reload, "reload"),
            (self.btn_open_config, "file"),
        ):
            set_icon(button, icon_name)
        for button in (self.btn_save, self.btn_onboarding, self.btn_reload, self.btn_open_config):
            self.add_action(button)
        self.btn_save.clicked.connect(self._save_all)
        self.btn_onboarding.clicked.connect(lambda: self._open_onboarding())
        self.btn_reload.clicked.connect(self.refresh)
        self.btn_open_config.clicked.connect(self._open_config)

        self._build_llm_group(layout)
        self._build_qq_group(layout)
        self._build_app_group(layout)
        self._build_paths_group(layout)
        layout.addStretch(1)

    # ================================================================ LLM
    def _build_llm_group(self, layout: QVBoxLayout) -> None:
        group = make_group("LLM 接口（兼容 OpenAI 协议）")
        form = QFormLayout(group)
        form.setContentsMargins(14, 18, 14, 14)

        self.llm_form = LLMConfigForm(self.ctx, group)
        form.addRow(self.llm_form)

        row = QWidget()
        # 生成参数拆两行：单行放不下时，宁可增高也不能把页面撑宽裁掉右侧
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        param_line1 = QHBoxLayout()
        param_line1.setContentsMargins(0, 0, 0, 0)
        param_line1.setSpacing(10)
        self.spin_max_tokens = QSpinBox()
        self.spin_max_tokens.setRange(16, 8192)
        self.spin_max_tokens.setFixedWidth(120)
        self.dspin_temperature = QDoubleSpinBox()
        self.dspin_temperature.setRange(0.0, 2.0)
        self.dspin_temperature.setSingleStep(0.05)
        self.dspin_temperature.setDecimals(2)
        self.dspin_temperature.setFixedWidth(110)
        param_line1.addWidget(QLabel("max_tokens"))
        param_line1.addWidget(self.spin_max_tokens)
        param_line1.addWidget(QLabel("temperature"))
        param_line1.addWidget(self.dspin_temperature)
        param_line1.addStretch(1)
        row_layout.addLayout(param_line1)
        param_line2 = QHBoxLayout()
        param_line2.setContentsMargins(0, 0, 0, 0)
        param_line2.setSpacing(10)
        self.dspin_top_p = QDoubleSpinBox()
        self.dspin_top_p.setRange(0.0, 1.0)
        self.dspin_top_p.setSingleStep(0.05)
        self.dspin_top_p.setDecimals(2)
        self.dspin_top_p.setFixedWidth(100)
        param_line2.addWidget(QLabel("top_p"))
        param_line2.addWidget(self.dspin_top_p)
        param_line2.addStretch(1)
        row_layout.addLayout(param_line2)
        add_form_row(form, "生成参数", row, "temperature 越高越活泼，0.7~1.0 比较自然")

        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.setSpacing(10)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(5, 600)
        self.spin_timeout.setSuffix(" 秒")
        self.spin_timeout.setFixedWidth(120)
        self.spin_retries = QSpinBox()
        self.spin_retries.setRange(0, 6)
        self.spin_retries.setSuffix(" 次")
        self.spin_retries.setFixedWidth(90)
        row2_layout.addWidget(QLabel("超时"))
        row2_layout.addWidget(self.spin_timeout)
        row2_layout.addWidget(QLabel("失败重试"))
        row2_layout.addWidget(self.spin_retries)
        row2_layout.addStretch(1)
        add_form_row(form, "网络参数", row2, "超时或报错时会自动重试，全部失败时使用兜底话术")

        self.edit_fallback = QPlainTextEdit()
        self.edit_fallback.setMaximumHeight(88)
        self.edit_fallback.setPlaceholderText("每行一条兜底话术")
        add_form_row(form, "兜底话术", self.edit_fallback, "LLM 不可用时用于主动消息的备用文本")
        layout.addWidget(group)

    # ================================================================= QQ
    def _build_qq_group(self, layout: QVBoxLayout) -> None:
        group = make_group("QQ 官方机器人凭据与消息行为（第 1 个机器人）")
        form = QFormLayout(group)
        form.setContentsMargins(14, 18, 14, 14)
        # 官方机器人的凭据与消息行为，与配置引导 / 机器人管理共用同一个控件
        self.qq_form = QQConfigForm(self.ctx, group, prefix="qq", title="第 1 个机器人")
        form.addRow(self.qq_form)

        self.lbl_bot_identity = QLabel("")
        self.lbl_bot_identity.setObjectName("ValueLabel")
        self.lbl_bot_identity.setWordWrap(True)
        add_form_row(form, "机器人身份", self.lbl_bot_identity)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        self.btn_bots_page = ghost_button("打开「机器人」页面")
        self.btn_bots_page.clicked.connect(self._open_bots_page)
        row_layout.addWidget(self.btn_bots_page)
        row_layout.addStretch(1)
        add_form_row(
            form,
            "多机器人",
            row,
            "本页只配置第 1 个机器人的凭据；新增 QQ 机器人、给机器人指定角色请到「机器人」页面",
        )
        layout.addWidget(group)

    def _open_bots_page(self) -> None:
        window = self.window()
        jump = getattr(window, "show_page_by_key", None)
        if callable(jump):
            jump("bots")

    # ============================================================ 应用行为
    def _build_app_group(self, layout: QVBoxLayout) -> None:
        group = make_group("程序行为")
        form = QFormLayout(group)
        form.setContentsMargins(14, 18, 14, 14)

        self.chk_start_bot = QCheckBox("启动程序时自动启动 Bot 进程")
        self.chk_close_tray = QCheckBox("点击关闭按钮时最小化到托盘")
        self.chk_notify = QCheckBox("主动消息发送后弹出托盘通知")
        self.chk_start_minimized = QCheckBox("启动时直接最小化到托盘")
        self.chk_autostart = QCheckBox("开机自动启动（写入注册表）")
        self.chk_onboarding = QCheckBox("启动时显示配置引导")

        for box in (
            self.chk_start_bot,
            self.chk_close_tray,
            self.chk_notify,
            self.chk_start_minimized,
            self.chk_onboarding,
        ):
            form.addRow(box)
        add_form_row(form, "开机自启", self.chk_autostart, "勾选后保存设置时写入 HKCU\\...\\Run")
        layout.addWidget(group)

    # ============================================================ 数据目录
    def _build_paths_group(self, layout: QVBoxLayout) -> None:
        group = make_group("数据与维护")
        form = QFormLayout(group)
        form.setContentsMargins(14, 18, 14, 14)

        self.label_data_dir = QLabel(str(data_dir()))
        self.label_data_dir.setObjectName("ValueLabel")
        self.label_data_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label_data_dir.setWordWrap(True)
        add_form_row(form, "数据目录", self.label_data_dir)

        self.label_config_path = QLabel(str(self.ctx.config.path))
        self.label_config_path.setObjectName("ValueLabel")
        self.label_config_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 与「数据目录」一致：长路径必须换行，否则按单行最小宽度把页面撑宽裁掉右侧
        self.label_config_path.setWordWrap(True)
        add_form_row(form, "配置文件", self.label_config_path)

        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        for text, target in (
            ("打开数据目录", data_dir()),
            ("打开日志目录", logs_dir()),
            ("打开角色卡目录", self.ctx.config.effective_characters_path()),
        ):
            button = ghost_button(text)
            button.clicked.connect(lambda _checked=False, path=target: self._open_path(path))
            buttons_layout.addWidget(button)
        buttons_layout.addStretch(1)
        add_form_row(form, "快速打开", buttons)
        layout.addWidget(group)

    def _update_bot_identity(self) -> None:
        """显示第 1 个机器人的名称与绑定角色（只读，编辑在「机器人」页面）。"""
        config = self.ctx.config
        name = str(config.get("qq.name", "") or "机器人 1")
        bound = str(config.get("qq.character_name", "") or "")
        if not bound:
            character_id = str(config.get("qq.character_id", "") or "")
            bound = character_id or "未绑定（按“上次发言 / 随机”选择角色）"
        total = 1 + len(list(config.get("bots", []) or []))
        self.lbl_bot_identity.setText(
            "名称：%s　·　绑定角色：%s　·　共 %d 个机器人" % (name, bound, total)
        )

    # ============================================================== 载入/保存
    def refresh(self) -> None:
        config = self.ctx.config
        config.load(force=True)

        # LLM
        self.llm_form.set_values(
            base_url=str(config.get("llm.base_url", "") or ""),
            api_key=str(config.get("llm.api_key", "") or ""),
            model=str(config.get("llm.model", "") or ""),
        )
        self.spin_max_tokens.setValue(int(config.get("llm.max_tokens", 500) or 500))
        self.dspin_temperature.setValue(float(config.get("llm.temperature", 0.85) or 0.85))
        self.dspin_top_p.setValue(float(config.get("llm.top_p", 1.0) or 1.0))
        self.spin_timeout.setValue(int(config.get("llm.timeout", 60) or 60))
        self.spin_retries.setValue(int(config.get("llm.max_retries", 2) or 0))
        fallbacks = config.get("llm.fallback_messages", []) or []
        if isinstance(fallbacks, str):
            fallbacks = [fallbacks]
        self.edit_fallback.setPlainText("\n".join(str(item) for item in fallbacks))

        # QQ 官方机器人（凭据 + 消息行为，全部由共享控件负责）
        self.qq_form.set_values(config)
        self.qq_form.refresh_status()
        self._update_bot_identity()

        # 行为
        self.chk_start_bot.setChecked(bool(config.get("app.start_bot_on_launch", True)))
        self.chk_close_tray.setChecked(bool(config.get("app.close_to_tray", True)))
        self.chk_notify.setChecked(bool(config.get("app.notify_on_proactive", True)))
        self.chk_start_minimized.setChecked(bool(config.get("app.start_minimized", False)))
        self.chk_onboarding.setChecked(not bool(config.get("app.onboarding_done", False)))
        try:
            self.chk_autostart.setChecked(autostart.is_enabled())
        except Exception:
            self.chk_autostart.setChecked(False)

    # ============================================================== 保存
    def _collect(self) -> Dict[str, Any]:
        fallbacks = [
            line.strip() for line in self.edit_fallback.toPlainText().splitlines() if line.strip()
        ]
        return {
            "llm": {
                **self.llm_form.values(),
                "max_tokens": self.spin_max_tokens.value(),
                "temperature": round(self.dspin_temperature.value(), 2),
                "top_p": round(self.dspin_top_p.value(), 2),
                "timeout": self.spin_timeout.value(),
                "max_retries": self.spin_retries.value(),
                "fallback_messages": fallbacks or ["在忙吗？突然有点想你了。"],
            },
            "qq": self.qq_form.values(),
            "app": {
                "start_bot_on_launch": self.chk_start_bot.isChecked(),
                "close_to_tray": self.chk_close_tray.isChecked(),
                "notify_on_proactive": self.chk_notify.isChecked(),
                "start_minimized": self.chk_start_minimized.isChecked(),
                "onboarding_done": not self.chk_onboarding.isChecked(),
            },
        }

    def _save_all(self) -> None:
        patch = self._collect()
        official = (patch.get("qq") or {}).get("official") or {}
        has_target = bool(
            str(official.get("target_openid", "") or "").strip()
            or str(official.get("group_openid", "") or "").strip()
        )
        if not has_target:
            # openid 会在用户第一次给机器人发消息时自动记住，所以这里只提示、不拦保存
            self.toast(
                "还没有记住主动消息对象：让别人先给机器人发一条消息，会自动记住该 openid",
                "warn",
            )

        def _ok(_result: Any) -> None:
            self.ctx.reload_config()
            self.ctx.sync_endpoints()
            try:
                autostart.set_enabled(self.chk_autostart.isChecked())
            except Exception as exc:
                self.toast("开机自启设置失败：%s" % exc, "warn")
            self.toast("设置已保存并同步到 Bot 进程", "info")
            self._update_bot_identity()
            self.ctx.request_status_refresh()

        self.run_task(self.api().put_config, patch, on_ok=_ok, key="save_settings", label="保存设置")

    # ============================================================== 操作
    def _test_llm(self) -> None:  # 兼容旧调用点：交给 LLMConfigForm 处理
        self.llm_form.test_connection()

    def _open_onboarding(self) -> None:
        window = self.window()
        runner = getattr(window, "run_onboarding", None)
        if callable(runner):
            runner(force=True)
        else:  # pragma: no cover - 兜底
            self.toast("请从主窗口打开配置引导", "warn")

    def _open_config(self) -> None:
        path = Path(self.ctx.config.path)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            self._open_path(path.parent)

    def _open_path(self, path: Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["explorer", str(path)])

    def on_status(self, snapshot: Dict[str, Any]) -> None:
        # QQ 官方机器人的连接状态随快照刷新
        try:
            self.qq_form.refresh_status(snapshot)
        except Exception as exc:  # pragma: no cover
            log.debug("刷新 QQ 状态失败：%s", exc)

__all__ = ["SettingsPage"]
