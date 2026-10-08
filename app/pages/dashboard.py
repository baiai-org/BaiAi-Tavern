"""仪表盘页面：运行状态、今日统计与快捷操作。"""

from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from common.utils import truncate

from ..uikit import set_icon
from ..widgets.fields import ghost_button, hint_label, make_group, primary_button
from ..widgets.status_indicator import StatusCard
from .base import Page


class DashboardPage(Page):
    page_title = "仪表盘"
    page_subtitle = "Bot 运行状态、各机器人连接情况、今日统计与快捷操作"
    scrollable = True

    def build(self, layout: QVBoxLayout) -> None:
        # ---------------------------------------------------------- 状态卡片
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_bot = StatusCard("Bot 状态", "未运行", "等待启动", "idle")
        self.card_qq = StatusCard("QQ 连接", "未知", "QQ 官方机器人", "idle")
        self.card_identity = StatusCard("登录身份", "未登录", "填写 AppID / AppSecret 后显示", "idle")
        self.card_today = StatusCard("今日主动消息", "0", "尚未发送", "idle")
        for card in (self.card_bot, self.card_qq, self.card_identity, self.card_today):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        # ---------------------------------------------------------- 机器人列表
        bots_group = make_group("机器人（每个机器人绑定一个角色）")
        bots_layout = QVBoxLayout(bots_group)
        bots_layout.setContentsMargins(14, 18, 14, 14)
        bots_layout.setSpacing(8)
        self.bots_table = QTableWidget(0, 5)
        self.bots_table.setHorizontalHeaderLabels(["机器人", "AppID", "状态", "绑定角色", "主动消息目标"])
        self.bots_table.verticalHeader().setVisible(False)
        self.bots_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.bots_table.setSelectionMode(QTableWidget.NoSelection)
        self.bots_table.setWordWrap(False)
        header = self.bots_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.bots_table.setMinimumHeight(120)
        self.bots_table.setMaximumHeight(220)
        bots_layout.addWidget(self.bots_table)
        self.bots_hint = hint_label("提示：「机器人」页面可以新增 QQ 官方机器人并绑定角色。")
        bots_layout.addWidget(self.bots_hint)
        layout.addWidget(bots_group)
        # ---------------------------------------------------------- 快捷操作
        actions_group = make_group("快捷操作")
        actions_layout = QGridLayout(actions_group)
        actions_layout.setContentsMargins(14, 18, 14, 14)
        actions_layout.setHorizontalSpacing(10)
        actions_layout.setVerticalSpacing(8)

        self.btn_start = primary_button("启动 Bot")
        self.btn_stop = ghost_button("停止 Bot")
        self.btn_restart = ghost_button("重启 Bot")
        self.combo_trigger_bot = QComboBox()
        self.combo_trigger_bot.setMinimumWidth(200)
        self.btn_trigger = primary_button("立即触发主动消息")
        self.btn_reload = ghost_button("重载配置")
        self.btn_open_data = ghost_button("打开数据目录")

        for button, icon_name in (
            (self.btn_start, "play"),
            (self.btn_stop, "stop"),
            (self.btn_restart, "restart"),
            (self.btn_trigger, "send"),
            (self.btn_reload, "reload"),
            (self.btn_open_data, "folder"),
        ):
            set_icon(button, icon_name)

        self.btn_start.clicked.connect(self._start_bot)
        self.btn_stop.clicked.connect(self._stop_bot)
        self.btn_restart.clicked.connect(self._restart_bot)
        self.btn_trigger.clicked.connect(self._trigger_proactive)
        self.btn_reload.clicked.connect(self._reload_config)
        self.btn_open_data.clicked.connect(self._open_data_dir)

        actions_layout.addWidget(self.btn_start, 0, 0)
        actions_layout.addWidget(self.btn_stop, 0, 1)
        actions_layout.addWidget(self.btn_restart, 0, 2)
        actions_layout.addWidget(QLabel("触发机器人"), 0, 3)
        actions_layout.addWidget(self.combo_trigger_bot, 1, 3)
        actions_layout.addWidget(self.btn_trigger, 0, 4)
        actions_layout.addWidget(self.btn_reload, 1, 0)
        actions_layout.addWidget(self.btn_open_data, 1, 1)
        for column in range(5):
            actions_layout.setColumnStretch(column, 1)
        layout.addWidget(actions_group)

        # ------------------------------------------------------ 使用量 + 调度
        columns = QHBoxLayout()
        columns.setSpacing(12)

        usage_group = make_group("今日使用情况")
        usage_layout = QVBoxLayout(usage_group)
        usage_layout.setContentsMargins(14, 18, 14, 14)
        usage_layout.setSpacing(8)
        self.quota_label = QLabel("--")
        self.quota_label.setObjectName("ValueLabel")
        usage_layout.addWidget(self.quota_label)
        self.quota_bar = QProgressBar()
        self.quota_bar.setRange(0, 100)
        self.quota_bar.setValue(0)
        self.quota_bar.setTextVisible(False)
        usage_layout.addWidget(self.quota_bar)
        self.quota_hint = hint_label("上限可在“消息设置”页面调整")
        usage_layout.addWidget(self.quota_hint)

        self.breakdown = QTableWidget(0, 2)
        self.breakdown.setHorizontalHeaderLabels(["角色", "今日条数"])
        self.breakdown.verticalHeader().setVisible(False)
        self.breakdown.setEditTriggers(QTableWidget.NoEditTriggers)
        self.breakdown.setSelectionMode(QTableWidget.NoSelection)
        self.breakdown.horizontalHeader().setStretchLastSection(False)
        self.breakdown.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.breakdown.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.breakdown.setMaximumHeight(180)
        usage_layout.addWidget(self.breakdown)

        schedule_group = make_group("调度任务")
        schedule_layout = QVBoxLayout(schedule_group)
        schedule_layout.setContentsMargins(14, 18, 14, 14)
        schedule_layout.setSpacing(8)
        self.schedule_label = QLabel("--")
        self.schedule_label.setObjectName("ValueLabel")
        self.schedule_label.setWordWrap(True)
        schedule_layout.addWidget(self.schedule_label)

        self.last_skip = hint_label("")
        schedule_layout.addWidget(self.last_skip)

        self.last_message = QLabel("（还没有发送过主动消息）")
        self.last_message.setObjectName("ValueLabel")
        self.last_message.setWordWrap(True)
        self.last_message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        schedule_layout.addWidget(self.last_message)

        self.llm_label = hint_label("")
        schedule_layout.addWidget(self.llm_label)
        schedule_layout.addStretch(1)

        columns.addWidget(usage_group, 1)
        columns.addWidget(schedule_group, 1)
        layout.addLayout(columns)
        layout.addStretch(1)

    # ============================================================== 机器人列表
    def _render_bots(self, snapshot: Dict[str, Any]) -> None:
        bots: List[Dict[str, Any]] = list(snapshot.get("bots") or [])
        if not bots:
            qq = snapshot.get("qq") or {}
            bots = [
                {
                    "id": "bot1",
                    "name": "机器人 1",
                    "app_id": qq.get("app_id") or "",
                    "connected": qq.get("connected"),
                    "configured": qq.get("configured", True),
                    "error": qq.get("error") or "",
                    "enabled": True,
                    "character_name": "",
                    "target": "",
                }
            ]

        self.bots_table.setRowCount(len(bots))
        for row, bot in enumerate(bots):
            name = str(bot.get("name") or "机器人 %d" % (row + 1))
            if not bot.get("enabled", True):
                name += "（已停用）"
            self.bots_table.setItem(row, 0, QTableWidgetItem(name))
            self.bots_table.setItem(row, 1, QTableWidgetItem(str(bot.get("app_id") or "—")))

            if bot.get("connected"):
                state_text, state_color = "已连接", "#3ddc84"
            elif not bot.get("configured", True):
                state_text, state_color = "未配置", "#f0ab29"
            elif not bot.get("enabled", True):
                state_text, state_color = "已停用", "#8b94a6"
            else:
                state_text, state_color = "未连接", "#f2725c"
            if bot.get("error") and not bot.get("connected"):
                state_text = "%s（%s）" % (state_text, truncate(str(bot["error"]), 40))
            item = QTableWidgetItem(state_text)
            item.setForeground(QColor(state_color))
            self.bots_table.setItem(row, 2, item)

            bound = str(bot.get("character_name") or "")
            self.bots_table.setItem(row, 3, QTableWidgetItem(bound or "未绑定（随机）"))
            self.bots_table.setItem(row, 4, QTableWidgetItem(str(bot.get("target") or "")))

        # 「触发机器人」下拉：跟随状态刷新，但保留用户当前选择
        current = str(self.combo_trigger_bot.currentData() or "")
        items = [(str(bot.get("id") or ""), str(bot.get("name") or "")) for bot in bots if bot.get("enabled", True)]
        if len(items) > 1:
            items.append(("all", "全部启用的机器人"))
        signature = "|".join("%s:%s" % item for item in items)
        if getattr(self, "_combo_signature", "") != signature:
            self._combo_signature = signature
            self.combo_trigger_bot.blockSignals(True)
            self.combo_trigger_bot.clear()
            for bot_id, bot_name in items:
                self.combo_trigger_bot.addItem(bot_name, bot_id)
            position = self.combo_trigger_bot.findData(current)
            self.combo_trigger_bot.setCurrentIndex(position if position >= 0 else 0)
            self.combo_trigger_bot.blockSignals(False)
        self.combo_trigger_bot.setEnabled(bool(items))
        self.btn_trigger.setEnabled(bool(items))

        if len(bots) > 1:
            self.bots_hint.setText(
                "共 %d 个机器人：每个机器人用自己的账号与绑定角色发消息；定时/空闲/随机触发时它们各自发言。"
                % len(bots)
            )
        else:
            self.bots_hint.setText(
                "当前只有 1 个机器人；「机器人」页面可以新增 QQ 账号并绑定不同角色。"
            )

    def on_status(self, snapshot: Dict[str, Any]) -> None:
        offline = bool(snapshot.get("offline"))
        online = bool(snapshot.get("online"))
        today = snapshot.get("today") or {}
        characters = snapshot.get("characters") or {}
        proactive = snapshot.get("proactive") or {}
        llm = snapshot.get("llm") or {}

        self._render_bots(snapshot)

        # Bot
        if online:
            self.card_bot.set_value("运行中", "启动于 %s" % self._short_time(snapshot.get("started_at")), "ok")
        elif offline:
            self.card_bot.set_value("未运行", str(snapshot.get("error") or "Bot 进程未启动"), "bad")
        else:
            self.card_bot.set_value("启动中", "正在等待 Bot 就绪", "warn")

        # QQ 连接（只有官方机器人一种方式）
        qq = snapshot.get("qq") or {}
        mode_label = str(snapshot.get("qq_mode_label") or "QQ 官方机器人")
        nickname = qq.get("nickname") or ""
        if qq.get("connected"):
            self.card_qq.set_value(
                "已连接",
                "%s%s" % (mode_label, ("（%s）" % nickname) if nickname else ""),
                "ok",
            )
        elif not qq.get("configured", True):
            self.card_qq.set_value(
                "未配置",
                "%s：请到「系统设置 → QQ」填写凭据" % (mode_label or "QQ"),
                "bad" if online else "idle",
            )
        else:
            self.card_qq.set_value(
                "未连接",
                truncate(qq.get("error") or ("%s 正在连接…" % mode_label), 60),
                "warn" if online else "idle",
            )

        # 登录身份（官方机器人：AppID / 机器人昵称）
        app_id = str(qq.get("app_id") or "")
        if nickname or app_id:
            self.card_identity.set_value(
                nickname or app_id,
                "官方机器人　·　AppID：%s" % (app_id or "未填"),
                "ok" if qq.get("connected") else "warn",
            )
        else:
            self.card_identity.set_value(
                "未配置", "填写 AppID / AppSecret 后自动连接", "warn" if online else "idle"
            )

        # 今日统计
        total = int(today.get("proactive_total") or 0)
        limit = int(self.ctx.config.get("proactive.global_daily_limit", 10) or 0)
        per_limit = int(self.ctx.config.get("proactive.per_character_daily_limit", 3) or 0)
        state = "ok"
        hint = "今日已发送 %d 条" % total
        if limit > 0:
            ratio = min(1.0, total / float(limit))
            state = "warn" if ratio >= 1.0 else ("info" if ratio >= 0.7 else "ok")
            hint = "全局上限 %d 条 / 单角色上限 %d 条" % (limit, per_limit)
            self.quota_bar.setValue(int(ratio * 100))
            self.quota_label.setText("已用 %d / %d 条（%.0f%%）" % (total, limit, ratio * 100))
        else:
            self.quota_bar.setValue(0)
            self.quota_label.setText("已发送 %d 条（未设置上限）" % total)
        self.card_today.set_value(str(total), hint, state)

        # 角色明细
        breakdown = today.get("proactive_by_character") or []
        self.breakdown.setRowCount(len(breakdown))
        for row, item in enumerate(breakdown):
            self.breakdown.setItem(row, 0, QTableWidgetItem(str(item.get("name") or "未知角色")))
            count_item = QTableWidgetItem(str(item.get("count") or 0))
            count_item.setTextAlignment(Qt.AlignCenter)
            self.breakdown.setItem(row, 1, count_item)
        if not breakdown:
            self.breakdown.setRowCount(1)
            self.breakdown.setItem(0, 0, QTableWidgetItem("今日还没有主动消息"))
            self.breakdown.setItem(0, 1, QTableWidgetItem("0"))

        # 调度
        jobs = proactive.get("jobs") or []
        if not proactive.get("running"):
            self.schedule_label.setText("调度器未运行（点击“启动 Bot”开启主动消息）")
        elif not jobs:
            self.schedule_label.setText("调度器运行中，但没有已注册的任务")
        else:
            lines = []
            for job in jobs[:6]:
                lines.append("· %s → %s" % (job.get("name"), job.get("next_run") or "待定"))
            self.schedule_label.setText("\n".join(lines))

        skip_reason = proactive.get("last_skip_reason") or ""
        self.last_skip.setText("最近一次跳过原因：%s" % skip_reason if skip_reason else "")

        last = snapshot.get("last_proactive") or {}
        if last:
            self.last_message.setText(
                "最近一条（%s · %s）\n%s：%s"
                % (
                    last.get("sent_at") or "",
                    last.get("trigger_type") or "",
                    last.get("character_name") or "角色",
                    truncate(last.get("content") or "", 120),
                )
            )
        else:
            self.last_message.setText("（还没有发送过主动消息）")

        self.llm_label.setText(
            "LLM：%s　·　%s　·　角色 %s/%s 启用　·　连接：QQ 官方机器人"
            % (
                llm.get("model") or "未设置",
                "已配置" if llm.get("configured") else "未配置 API Key",
                characters.get("enabled", 0),
                characters.get("total", 0),
            )
        )

        self.btn_stop.setEnabled(True if self.ctx.bot_process_running() or online else True)
        self.btn_start.setEnabled(not self.ctx.bot_process_running())

    @staticmethod
    def _short_time(value: Any) -> str:
        text = str(value or "")
        return text.replace("T", " ")[:19] or "--"

    # ============================================================== 操作实现
    def _start_bot(self) -> None:
        if not self.ctx.bot_process_running():
            self.ctx.start_bot_process()
        self._api_call(self.api().bot_start, "启动 Bot", on_ok=lambda _r: self.toast("Bot 已启动", "info"))

    def _stop_bot(self) -> None:
        def _ok(result: Any) -> None:
            message = (result or {}).get("message") or "Bot 已停止"
            self.toast(message, "info")

        self.run_task(self.api().bot_stop, on_ok=_ok, label="停止 Bot")

    def _restart_bot(self) -> None:
        self.ctx.restart_bot_process()

    def _trigger_proactive(self) -> None:
        self.btn_trigger.setEnabled(False)
        bot_id = str(self.combo_trigger_bot.currentData() or "")

        def _ok(result: Any) -> None:
            self.btn_trigger.setEnabled(True)
            if not isinstance(result, dict):
                self.toast("触发完成", "info")
                return
            if result.get("skipped"):
                self.toast("未发送：%s" % result.get("reason", "条件不满足"), "warn")
            else:
                self.last_message.setText(
                    "刚刚发送（%s）\n%s：%s"
                    % (
                        result.get("trigger_label") or "手动触发",
                        result.get("character") or "角色",
                        result.get("content") or "",
                    )
                )
                prefix = "%s " % result.get("bot_name") if result.get("bot_name") else ""
                if result.get("bots"):
                    sent = [item for item in (result.get("bots") or []) if item.get("ok")]
                    self.toast("已由 %d 个机器人各发一条" % len(sent), "info")
                else:
                    self.toast(
                        "%s「%s」已发送：%s" % (prefix, result.get("character"), truncate(result.get("content"), 40)),
                        "info",
                    )
            self.ctx.request_status_refresh()

        def _error(message: str) -> None:
            self.btn_trigger.setEnabled(True)
            self.toast("触发失败：%s" % message, "error")

        self.run_task(
            self.api().proactive_trigger_for_bot,
            bot_id,
            None,
            True,
            on_ok=_ok,
            on_error=_error,
            key="manual_trigger",
            label="触发主动消息",
        )

    def _reload_config(self) -> None:
        def _ok(result: Any) -> None:
            self.ctx.reload_config()
            self.toast("配置已重载", "info")
            self.ctx.request_status_refresh()

        self.run_task(self.api().reload_config, on_ok=_ok, label="重载配置")

    def _open_data_dir(self) -> None:
        import os
        import subprocess

        from common.paths import data_dir

        path = str(data_dir())
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["explorer", path])

    def _api_call(self, fn, label: str, on_ok=None) -> None:
        self.run_task(
            fn,
            on_ok=on_ok,
            on_error=lambda message: self.toast("%s失败：%s" % (label, message), "error"),
            label=label,
        )

    def refresh(self) -> None:
        self.ctx.request_status_refresh()
