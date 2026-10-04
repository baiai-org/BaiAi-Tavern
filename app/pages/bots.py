"""机器人管理页面：多机器人（多个 QQ 官方机器人）+ 每个机器人绑定一个角色。

一个「机器人」= 一个 QQ 开放平台机器人应用（自己的 AppID / AppSecret）。
每个机器人有自己的凭据、发送目标，并且可以**绑定一个角色**：
绑定之后，这个机器人的所有回复与主动消息都由该角色发出。

第 1 个机器人的配置保存在 ``qq:`` 段（与旧版本完全兼容），
第 2..N 个机器人保存在 ``bots:`` 段；本页面统一编辑两者。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from common.bots import MODE_OFFICIAL, mode_label

from ..uikit import set_icon
from ..widgets.fields import add_form_row, ghost_button, hint_label, make_group, primary_button
from ..widgets.qq_form import QQConfigForm
from .base import Page

UNBOUND = "（不绑定，按「上次发言 / 随机」选择）"


class BotsPage(Page):
    page_title = "机器人"
    page_subtitle = "每个机器人对应一个 QQ 官方机器人应用；给机器人绑定角色后，它就用那个角色说话"

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_add = primary_button("新增机器人")
        self.btn_remove = ghost_button("删除机器人")
        self.btn_save = primary_button("保存并应用")
        self.btn_refresh = ghost_button("刷新")
        for button, icon_name in (
            (self.btn_add, "plus"),
            (self.btn_remove, "trash"),
            (self.btn_save, "check"),
            (self.btn_refresh, "refresh"),
        ):
            set_icon(button, icon_name)
        for button in (self.btn_add, self.btn_remove, self.btn_save, self.btn_refresh):
            self.add_action(button)
        self.btn_add.clicked.connect(self._add_bot)
        self.btn_remove.clicked.connect(self._remove_bot)
        self.btn_save.clicked.connect(self._save)
        self.btn_refresh.clicked.connect(self.refresh)

        self._bots: List[Dict[str, Any]] = []
        self._characters: List[Dict[str, str]] = []
        self.current_index: int = 0
        self.qq_form: Optional[QQConfigForm] = None

        body = QHBoxLayout()
        body.setSpacing(12)

        # ------------------------------------------------------------ 左侧列表
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("机器人列表"))
        self.list = QListWidget(left)
        self.list.setObjectName("OnbList")
        self.list.setMinimumWidth(260)
        self.list.currentRowChanged.connect(self._on_select)
        left_layout.addWidget(self.list, 1)
        self.lbl_summary = hint_label("正在读取机器人…")
        left_layout.addWidget(self.lbl_summary)
        body.addWidget(left, 0)

        # ------------------------------------------------------------ 右侧表单
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.form_host = QWidget(self.scroll)
        self.form_layout = QVBoxLayout(self.form_host)
        self.form_layout.setContentsMargins(0, 0, 8, 0)
        self.form_layout.setSpacing(12)
        self.scroll.setWidget(self.form_host)
        body.addWidget(self.scroll, 1)

        layout.addLayout(body, 1)

    # ============================================================== 数据加载
    def refresh(self) -> None:
        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            self._bots = list(data.get("bots") or [])
            self._characters = list(data.get("characters") or [])
            self._render_list()
            self._render_detail()

        self.run_task(self.api().bots, on_ok=_ok, key="load_bots", label="加载机器人")

    def _render_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for index, bot in enumerate(self._bots):
            label = "%s%s\n    %s%s" % (
                "● " if bot.get("connected") else "○ ",
                bot.get("name") or ("机器人 %d" % (index + 1)),
                mode_label(bot.get("mode") or MODE_OFFICIAL),
                "（已停用）" if not bot.get("enabled", True) else "",
            )
            bound = bot.get("character_name") or bot.get("character_id") or ""
            label += "　·　角色：%s" % (bound or "未绑定")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, bot.get("id"))
            self.list.addItem(item)
        self.list.blockSignals(False)

        self.current_index = max(0, min(self.current_index, len(self._bots) - 1))
        if self._bots:
            self.list.setCurrentRow(self.current_index)
        enabled = len([item for item in self._bots if item.get("enabled", True)])
        connected = len([item for item in self._bots if item.get("connected")])
        self.lbl_summary.setText(
            "共 %d 个机器人，启用 %d 个，当前已连接 %d 个。\n"
            "提示：每个机器人对应一个 QQ 开放平台应用，填自己的 AppID / AppSecret。" % (len(self._bots), enabled, connected)
        )
        if hasattr(self, "btn_remove"):
            self.btn_remove.setEnabled(len(self._bots) > 1 and self.current_index > 0)

    def _current_bot(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.current_index < len(self._bots):
            return self._bots[self.current_index]
        return None

    def _render_detail(self) -> None:
        # 清空旧表单
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        self.qq_form = None

        bot = self._current_bot()
        if bot is None:
            self.form_layout.addWidget(hint_label("还没有机器人。点「＋ 新增机器人」添加一个 QQ 账号。"))
            self.form_layout.addStretch(1)
            return

        index = self.current_index
        prefix = "qq" if index == 0 else "bots.%d" % (index - 1)
        config = self.ctx.config

        # ---------------------------------------------------------- 身份
        identity = make_group("机器人身份")
        form = QFormLayout(identity)
        form.setContentsMargins(14, 18, 14, 14)

        self.edit_name = QLineEdit(str(bot.get("name") or ""))
        self.edit_name.setMinimumWidth(240)
        add_form_row(form, "名称", self.edit_name, "只用于界面显示，例如「官方机器人」「主号 BOT」")

        self.chk_enabled = QCheckBox("启用这个机器人（停用后不收发消息）")
        self.chk_enabled.setChecked(bool(bot.get("enabled", True)))
        form.addRow(self.chk_enabled)

        character_row = QWidget(identity)
        character_layout = QHBoxLayout(character_row)
        character_layout.setContentsMargins(0, 0, 0, 0)
        character_layout.setSpacing(8)
        self.combo_character = QComboBox(character_row)
        self.combo_character.setMinimumWidth(260)
        self.combo_character.addItem(UNBOUND, "")
        for item in self._characters:
            self.combo_character.addItem(str(item.get("name") or ""), str(item.get("id") or ""))
        bound_id = str(bot.get("character_id") or "")
        position = self.combo_character.findData(bound_id)
        self.combo_character.setCurrentIndex(position if position >= 0 else 0)
        self.btn_reload_characters = ghost_button("刷新角色", character_row)
        self.btn_reload_characters.setProperty("chip", True)
        self.btn_reload_characters.clicked.connect(self.refresh)
        character_layout.addWidget(self.combo_character)
        character_layout.addWidget(self.btn_reload_characters)
        character_layout.addStretch(1)
        add_form_row(
            form,
            "绑定角色",
            character_row,
            "这个机器人的回复与主动消息都由该角色发出（未绑定时按“上次发言/随机”选择）",
        )

        self.lbl_identity_hint = hint_label(
            "第 1 个机器人：%s" % ("配置在系统设置的 qq: 段（与旧版本兼容）" if index == 0 else "配置在 config.yaml 的 bots: 段")
        )
        form.addRow(self.lbl_identity_hint)
        self.form_layout.addWidget(identity)

        # ---------------------------------------------------------- 连接
        connection = make_group("凭据与消息行为（%s）" % mode_label(bot.get("mode") or MODE_OFFICIAL))
        connection_form = QFormLayout(connection)
        connection_form.setContentsMargins(14, 18, 14, 14)
        self.qq_form = QQConfigForm(self.ctx, connection, prefix=prefix, bot_id=str(bot.get("id") or ""))
        self.qq_form.set_values(config)
        self.qq_form.refresh_status(self.ctx.last_status or {})
        connection_form.addRow(self.qq_form)
        self.form_layout.addWidget(connection)

        # ---------------------------------------------------------- 动作
        actions = QWidget(self.form_host)
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        self.btn_trigger = ghost_button("让这个机器人现在发一条", actions)
        self.btn_trigger.clicked.connect(self._trigger_current)
        self.btn_openid = ghost_button("忘记已记住的对象", actions)
        self.btn_openid.clicked.connect(self._forget_openid)
        actions_layout.addWidget(self.btn_trigger)
        actions_layout.addWidget(self.btn_openid)
        actions_layout.addStretch(1)
        self.form_layout.addWidget(actions)

        self.lbl_detail_hint = hint_label("")
        self.form_layout.addWidget(self.lbl_detail_hint)
        self.form_layout.addStretch(1)
        self._update_detail_hint()

    def _clear_layout(self, layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _update_detail_hint(self) -> None:
        bot = self._current_bot()
        if bot is None or not hasattr(self, "lbl_detail_hint"):
            return
        parts = [
            "状态：%s" % ("已连接" if bot.get("connected") else ("未配置" if not bot.get("configured", True) else "未连接")),
            "目标：%s" % (bot.get("target") or "未设置"),
        ]
        if bot.get("user_id"):
            parts.append("账号：%s" % bot["user_id"])
        if bot.get("error"):
            parts.append("错误：%s" % bot["error"])
        self.lbl_detail_hint.setText("　·　".join(parts))

    # ============================================================== 选择切换
    def _on_select(self, row: int) -> None:
        if row < 0 or row == self.current_index:
            if row >= 0:
                self.current_index = row
            return
        self.current_index = row
        self._render_detail()
        self._render_list()
        self._update_detail_hint()

    # ============================================================== 增删改
    def _add_bot(self) -> None:
        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            name = str((data.get("bot") or {}).get("name") or "新机器人")
            self.toast("已新增机器人「%s」，请填写凭据并绑定角色" % name, "info")
            self.current_index = len(self._bots)
            self.refresh()

        self.run_task(self.api().create_bot, {}, on_ok=_ok, key="create_bot", label="新增机器人")

    def _remove_bot(self) -> None:
        bot = self._current_bot()
        if bot is None:
            return
        if self.current_index == 0:
            QMessageBox.information(self, "无法删除", "第 1 个机器人不能删除，可以把它停用。")
            return
        confirm = QMessageBox.question(
            self,
            "删除机器人",
            "确定要删除机器人「%s」吗？它的连接配置会被移除（角色卡与对话记录不受影响）。" % (bot.get("name") or ""),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        def _ok(_result: Any) -> None:
            self.toast("机器人已删除", "info")
            self.current_index = max(0, self.current_index - 1)
            self.refresh()
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().delete_bot,
            str(bot.get("id") or ""),
            on_ok=_ok,
            key="delete_bot",
            label="删除机器人",
        )

    def _collect(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        if self.qq_form is not None:
            values.update(self.qq_form.values())
        values["name"] = self.edit_name.text().strip()
        values["enabled"] = self.chk_enabled.isChecked()
        character_id = str(self.combo_character.currentData() or "")
        values["character_id"] = character_id
        values["character_name"] = (
            str(self.combo_character.currentText()) if character_id else ""
        )
        return values

    def _save(self) -> None:
        bot = self._current_bot()
        if bot is None:
            return
        values = self._collect()

        def _ok(_result: Any) -> None:
            self.ctx.reload_config()
            self.toast("机器人「%s」的配置已保存并应用" % (values.get("name") or ""), "info")
            self.refresh()
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().update_bot,
            str(bot.get("id") or ""),
            values,
            on_ok=_ok,
            key="save_bot",
            label="保存机器人配置",
        )

    def _trigger_current(self) -> None:
        bot = self._current_bot()
        if bot is None:
            return

        def _ok(result: Any) -> None:
            if not isinstance(result, dict):
                self.toast("触发完成", "info")
                return
            if result.get("skipped"):
                self.toast("未发送：%s" % (result.get("reason") or "条件不满足"), "warn")
            else:
                self.toast(
                    "「%s」已发送：%s" % (result.get("character"), str(result.get("content") or "")[:40]),
                    "info",
                )
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().proactive_trigger_for_bot,
            str(bot.get("id") or ""),
            None,
            True,
            on_ok=_ok,
            key="trigger_%s" % bot.get("id"),
            label="机器人主动消息",
        )

    def _forget_openid(self) -> None:
        bot = self._current_bot()
        if bot is None:
            return
        confirm = QMessageBox.question(
            self,
            "忘记已记住的对象",
            "清除后，官方机器人的主动消息会发给下一个给它发消息的人。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.run_task(
            self.api().forget_bot_openid,
            str(bot.get("id") or ""),
            on_ok=lambda _r: (self.toast("已清除记住的对象", "info"), self.ctx.request_status_refresh()),
            key="forget_openid",
            label="清除记住的对象",
        )

    # ============================================================== 状态刷新
    def on_status(self, snapshot: Dict[str, Any]) -> None:
        bots = snapshot.get("bots")
        if not isinstance(bots, list) or not bots:
            return
        # 合并状态（保留列表顺序），仅在内容变化时重绘
        changed = False
        for index, state in enumerate(bots):
            if index >= len(self._bots):
                changed = True
                break
            for key in ("connected", "configured", "error", "user_id", "mode", "character_name", "target", "enabled", "name"):
                if self._bots[index].get(key) != state.get(key):
                    changed = True
                    break
            if changed:
                break
        if changed:
            self._bots = list(bots)
            self._render_list()
        self._update_detail_hint()

    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") in ("bots_changed", "config_reloaded", "qq_connected", "qq_disconnected"):
            self.refresh()


__all__ = ["BotsPage", "UNBOUND"]
