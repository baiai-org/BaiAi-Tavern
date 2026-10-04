"""主动消息配置页面（G-10 ~ G-16）。"""

from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
)

from common.utils import format_hhmm, parse_hhmm, truncate

from ..uikit import set_icon
from ..widgets.fields import (
    TimeListEdit,
    add_form_row,
    ghost_button,
    hint_label,
    make_group,
    primary_button,
)
from .base import Page


def _time_edit(value: Any, default: str = "08:00") -> QTimeEdit:
    hour, minute = parse_hhmm(value, parse_hhmm(default, (8, 0)))
    editor = QTimeEdit(QTime(hour, minute))
    editor.setDisplayFormat("HH:mm")
    editor.setFixedWidth(96)
    return editor


def _safe_int(value: Any, minimum: int, maximum: int, default: int = 0) -> int:
    """把配置/接口里的值夹到 [minimum, maximum]。

    Qt 的 ``setValue`` 只接受 C ``int``：外部数据里万一出现超大整数（或 NaN/inf）
    会直接抛 ``OverflowError: int too big to convert`` —— 这里统一兜住。
    """
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    return max(int(minimum), min(int(maximum), number))


def _safe_float(value: Any, minimum: float, maximum: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(default)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        number = float(default)
    return max(float(minimum), min(float(maximum), number))


def _spin(value: Any, minimum: int, maximum: int, suffix: str = "", step: int = 1) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setValue(_safe_int(value, minimum, maximum))
    if suffix:
        box.setSuffix(suffix)
    box.setFixedWidth(140)
    return box


def _dspin(value: Any, minimum: float, maximum: float, step: float = 0.05) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setDecimals(2)
    box.setValue(_safe_float(value, minimum, maximum))
    box.setFixedWidth(140)
    return box


class ProactivePage(Page):
    page_title = "主动消息"
    page_subtitle = "定时 / 空闲 / 随机三种触发方式，含频率限制、免打扰与概率控制"
    scrollable = True

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_save = primary_button("保存并应用")
        self.btn_trigger = ghost_button("立即触发一次")
        self.btn_reload = ghost_button("重新载入")
        for button, icon_name in (
            (self.btn_save, "check"),
            (self.btn_trigger, "send"),
            (self.btn_reload, "reload"),
        ):
            set_icon(button, icon_name)
        self.combo_bot = QComboBox()
        self.combo_bot.setMinimumWidth(190)
        for widget in (self.btn_save, self.btn_trigger, self.combo_bot, self.btn_reload):
            self.add_action(widget)
        self.btn_save.clicked.connect(self._save)
        self.btn_trigger.clicked.connect(self._trigger)
        self.btn_reload.clicked.connect(self.refresh)
        self._combo_signature = ""

        config = self.ctx.config

        # ------------------------------------------------------------ 总开关
        master = make_group("总开关")
        master_form = QFormLayout(master)
        master_form.setContentsMargins(14, 18, 14, 14)
        self.chk_enabled = QCheckBox("启用主动消息")
        self.chk_enabled.setChecked(bool(config.get("proactive.enabled", True)))
        add_form_row(master_form, "启用", self.chk_enabled, "关闭后定时/空闲/随机触发都不会发送消息（手动触发仍然可用）")
        layout.addWidget(master)

        # ------------------------------------------------------------ 定时触发
        scheduled = make_group("定时触发（每天固定时刻）")
        scheduled_form = QFormLayout(scheduled)
        scheduled_form.setContentsMargins(14, 18, 14, 14)
        self.chk_scheduled = QCheckBox("启用定时触发")
        self.chk_scheduled.setChecked(bool(config.get("proactive.scheduled_enabled", True)))
        add_form_row(scheduled_form, "启用", self.chk_scheduled)
        self.times_edit = TimeListEdit(config.get("proactive.scheduled_times", []) or [])
        add_form_row(scheduled_form, "触发时间", self.times_edit, "可以添加多个时间点，例如早上 09:00 与晚上 21:00")
        layout.addWidget(scheduled)

        # ------------------------------------------------------------ 空闲触发
        idle = make_group("空闲触发（用户长时间没说话）")
        idle_form = QFormLayout(idle)
        idle_form.setContentsMargins(14, 18, 14, 14)
        self.chk_idle = QCheckBox("启用空闲触发")
        self.chk_idle.setChecked(bool(config.get("proactive.idle_enabled", True)))
        add_form_row(idle_form, "启用", self.chk_idle)
        self.spin_idle_hours = _spin(config.get("proactive.idle_hours", 6), 1, 72, " 小时")
        add_form_row(idle_form, "空闲阈值", self.spin_idle_hours, "用户超过这段时间没有发言，就认为可以主动关心一下")
        self.spin_idle_check = _spin(config.get("proactive.idle_check_interval_minutes", 15), 1, 240, " 分钟")
        add_form_row(idle_form, "检查频率", self.spin_idle_check, "调度器多久检查一次空闲状态")
        layout.addWidget(idle)

        # ------------------------------------------------------------ 随机触发
        random_group = make_group("随机间隔触发")
        random_form = QFormLayout(random_group)
        random_form.setContentsMargins(14, 18, 14, 14)
        self.chk_random = QCheckBox("启用随机触发")
        self.chk_random.setChecked(bool(config.get("proactive.random_enabled", False)))
        add_form_row(random_form, "启用", self.chk_random)
        self.spin_random_min = _spin(config.get("proactive.random_min_interval_minutes", 120), 5, 1440, " 分钟")
        self.spin_random_max = _spin(config.get("proactive.random_max_interval_minutes", 300), 5, 2880, " 分钟")
        add_form_row(random_form, "最小间隔", self.spin_random_min)
        add_form_row(random_form, "最大间隔", self.spin_random_max, "在活跃时段内按这个区间随机安排下一次发言")
        layout.addWidget(random_group)

        # -------------------------------------------------------- 时段控制
        windows = QHBoxLayout()
        windows.setSpacing(12)

        active = make_group("活跃时段")
        active_form = QFormLayout(active)
        active_form.setContentsMargins(14, 18, 14, 14)
        self.chk_active = QCheckBox("限制在活跃时段内")
        self.chk_active.setChecked(bool((config.get("proactive.active_hours", {}) or {}).get("enabled", True)))
        self.time_active_start = _time_edit((config.get("proactive.active_hours", {}) or {}).get("start", "08:00"), "08:00")
        self.time_active_end = _time_edit((config.get("proactive.active_hours", {}) or {}).get("end", "23:00"), "23:00")
        active_form.addRow(self.chk_active)
        add_form_row(active_form, "开始", self.time_active_start)
        add_form_row(active_form, "结束", self.time_active_end, "只有在这个时间段内才会触发主动消息")

        dnd = make_group("免打扰时段")
        dnd_form = QFormLayout(dnd)
        dnd_form.setContentsMargins(14, 18, 14, 14)
        self.chk_dnd = QCheckBox("启用免打扰")
        self.chk_dnd.setChecked(bool((config.get("proactive.dnd_hours", {}) or {}).get("enabled", True)))
        self.time_dnd_start = _time_edit((config.get("proactive.dnd_hours", {}) or {}).get("start", "23:00"), "23:00")
        self.time_dnd_end = _time_edit((config.get("proactive.dnd_hours", {}) or {}).get("end", "08:00"), "08:00")
        dnd_form.addRow(self.chk_dnd)
        add_form_row(dnd_form, "开始", self.time_dnd_start)
        add_form_row(dnd_form, "结束", self.time_dnd_end, "跨越零点也可以，例如 23:00 → 08:00")

        windows.addWidget(active, 1)
        windows.addWidget(dnd, 1)
        layout.addLayout(windows)

        # ------------------------------------------------------------ 频率限制
        limits = make_group("频率限制与概率")
        limits_form = QFormLayout(limits)
        limits_form.setContentsMargins(14, 18, 14, 14)
        self.spin_global = _spin(config.get("proactive.global_daily_limit", 10), 0, 200, " 条/天")
        self.spin_per_character = _spin(config.get("proactive.per_character_daily_limit", 3), 0, 100, " 条/天")
        self.spin_min_interval = _spin(config.get("proactive.min_interval_minutes", 30), 0, 1440, " 分钟")
        self.dspin_probability = _dspin(config.get("proactive.probability", 0.7), 0.0, 1.0, 0.05)
        add_form_row(limits_form, "全局每日上限", self.spin_global, "0 表示不限制")
        add_form_row(limits_form, "单角色每日上限", self.spin_per_character, "0 表示不限制")
        add_form_row(limits_form, "最小间隔", self.spin_min_interval, "两次主动消息之间至少间隔多久")
        add_form_row(limits_form, "触发概率", self.dspin_probability, "满足条件后真正发送的概率，0.7 表示 70%")
        layout.addWidget(limits)

        # ------------------------------------------------------------ 消息行为
        behavior = make_group("消息与角色选择")
        behavior_form = QFormLayout(behavior)
        behavior_form.setContentsMargins(14, 18, 14, 14)
        self.chk_avoid_repeat = QCheckBox("避免连续由同一角色发言")
        self.chk_avoid_repeat.setChecked(bool(config.get("proactive.avoid_repeat", True)))
        self.chk_include_memory = QCheckBox("生成时携带长期记忆")
        self.chk_include_memory.setChecked(bool(config.get("proactive.include_memory", True)))
        self.spin_max_chars = _spin(config.get("proactive.max_message_chars", 120), 20, 1000, " 字")
        self.spin_context = _spin(config.get("proactive.context_messages", 8), 0, 60, " 条")
        behavior_form.addRow(self.chk_avoid_repeat)
        behavior_form.addRow(self.chk_include_memory)
        add_form_row(behavior_form, "消息长度上限", self.spin_max_chars)
        add_form_row(behavior_form, "上下文条数", self.spin_context, "生成主动消息时参考的最近对话条数")
        layout.addWidget(behavior)

        # ------------------------------------------------------------ 运行状态
        status = make_group("调度状态")
        status_layout = QVBoxLayout(status)
        status_layout.setContentsMargins(14, 18, 14, 14)
        status_layout.setSpacing(6)
        self.status_label = QLabel("正在读取状态…")
        self.status_label.setObjectName("ValueLabel")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.bots_label = hint_label("")
        status_layout.addWidget(self.bots_label)
        self.skip_label = hint_label("")
        status_layout.addWidget(self.skip_label)
        self.last_label = hint_label("")
        status_layout.addWidget(self.last_label)
        layout.addWidget(status)
        layout.addStretch(1)

    # ============================================================== 状态显示
    def on_status(self, snapshot: Dict[str, Any]) -> None:
        proactive = snapshot.get("proactive") or {}
        jobs = proactive.get("jobs") or []
        if not snapshot.get("online"):
            self.status_label.setText("Bot 未运行，主动消息不会触发")
        elif not proactive.get("running"):
            self.status_label.setText("Bot 在线，但主动消息调度未启动")
        elif jobs:
            lines = ["· %s → %s" % (job.get("name"), job.get("next_run") or "待定") for job in jobs[:8]]
            self.status_label.setText("已注册 %d 个调度任务：\n%s" % (len(jobs), "\n".join(lines)))
        else:
            self.status_label.setText("调度器运行中，但没有任何任务（请检查是否已启用触发方式）")

        bots = proactive.get("bots") or snapshot.get("bots") or []
        if bots:
            lines = []
            for bot in bots:
                if not bot.get("enabled", True):
                    continue
                lines.append(
                    "· %s：%s　·　角色：%s　·　目标：%s"
                    % (
                        bot.get("name") or "机器人",
                        bot.get("mode_label") or bot.get("mode") or "",
                        bot.get("character_name") or "未绑定（随机）",
                        bot.get("target") or "未设置",
                    )
                )
            self.bots_label.setText(
                "定时/空闲/随机触发时，以下机器人各自用绑定角色发言（「立即触发一次」只发本页所选机器人）：\n%s"
                % ("\n".join(lines) if lines else "（没有启用的机器人）")
            )
            self.btn_trigger.setText("立即触发一次（%s）" % (self.combo_bot.currentText() or ""))
        else:
            self.bots_label.setText("")

        reason = proactive.get("last_skip_reason") or ""
        self.skip_label.setText("最近跳过原因：%s" % reason if reason else "")

        last = snapshot.get("last_proactive") or {}
        if last:
            bot_part = "　·　%s" % last.get("bot_name") if last.get("bot_name") else ""
            self.last_label.setText(
                "最近发送：%s（%s）%s%s"
                % (
                    last.get("sent_at") or "",
                    last.get("trigger_type") or "",
                    bot_part,
                    truncate(last.get("content") or "", 40),
                )
            )
        else:
            self.last_label.setText("")
        self._sync_bot_combo(snapshot)

    def _sync_bot_combo(self, snapshot: Dict[str, Any]) -> None:
        """「立即触发一次」旁边的机器人下拉（手动触发只影响选中的机器人）。"""
        bots = [item for item in (snapshot.get("bots") or []) if item.get("enabled", True)]
        if not bots:
            qq = snapshot.get("qq") or {}
            bots = [
                {
                    "id": "bot1",
                    "name": "机器人 1",
                    "mode_label": snapshot.get("qq_mode_label") or qq.get("mode") or "",
                }
            ]
        signature = "|".join("%s:%s" % (item.get("id"), item.get("name")) for item in bots)
        if len(bots) > 1:
            bots = list(bots) + [{"id": "all", "name": "全部启用的机器人", "mode_label": "各发一条"}]
            signature += "|all"
        if signature == self._combo_signature:
            return
        self._combo_signature = signature
        current = str(self.combo_bot.currentData() or "")
        self.combo_bot.blockSignals(True)
        self.combo_bot.clear()
        for item in bots:
            label = "%s（%s）" % (item.get("name") or "机器人", item.get("mode_label") or item.get("mode") or "")
            self.combo_bot.addItem(label, str(item.get("id") or ""))
        position = self.combo_bot.findData(current)
        self.combo_bot.setCurrentIndex(position if position >= 0 else 0)
        self.combo_bot.blockSignals(False)

    # ============================================================== 保存
    def _collect(self) -> Dict[str, Any]:
        return {
            "enabled": self.chk_enabled.isChecked(),
            "scheduled_enabled": self.chk_scheduled.isChecked(),
            "scheduled_times": self.times_edit.times(),
            "idle_enabled": self.chk_idle.isChecked(),
            "idle_hours": self.spin_idle_hours.value(),
            "idle_check_interval_minutes": self.spin_idle_check.value(),
            "random_enabled": self.chk_random.isChecked(),
            "random_min_interval_minutes": self.spin_random_min.value(),
            "random_max_interval_minutes": self.spin_random_max.value(),
            "active_hours": {
                "enabled": self.chk_active.isChecked(),
                "start": self.time_active_start.time().toString("HH:mm"),
                "end": self.time_active_end.time().toString("HH:mm"),
            },
            "dnd_hours": {
                "enabled": self.chk_dnd.isChecked(),
                "start": self.time_dnd_start.time().toString("HH:mm"),
                "end": self.time_dnd_end.time().toString("HH:mm"),
            },
            "global_daily_limit": self.spin_global.value(),
            "per_character_daily_limit": self.spin_per_character.value(),
            "min_interval_minutes": self.spin_min_interval.value(),
            "probability": round(self.dspin_probability.value(), 2),
            "avoid_repeat": self.chk_avoid_repeat.isChecked(),
            "include_memory": self.chk_include_memory.isChecked(),
            "max_message_chars": self.spin_max_chars.value(),
            "context_messages": self.spin_context.value(),
        }

    def _save(self) -> None:
        patch = {"proactive": self._collect()}

        def _ok(result: Any) -> None:
            self.ctx.reload_config()
            self.toast("主动消息配置已保存并应用到 Bot", "info")
            self.ctx.request_status_refresh()

        self.run_task(self.api().put_config, patch, on_ok=_ok, key="save_proactive", label="保存主动消息配置")

    def _trigger(self) -> None:
        bot_id = str(self.combo_bot.currentData() or "")

        def _ok(result: Any) -> None:
            if not isinstance(result, dict):
                return
            if result.get("skipped"):
                self.toast("未发送：%s" % result.get("reason", "条件不满足"), "warn")
            else:
                self.toast(
                    "%s已由「%s」发送：%s"
                    % (
                        ("%s " % result.get("bot_name")) if result.get("bot_name") else "",
                        result.get("character"),
                        truncate(result.get("content"), 40),
                    ),
                    "info",
                )
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().proactive_trigger_for_bot,
            bot_id,
            None,
            True,
            on_ok=_ok,
            key="manual_trigger",
            label="触发主动消息",
        )

    # ============================================================== 载入
    def refresh(self) -> None:
        config = self.ctx.config
        config.load(force=True)

        self.chk_enabled.setChecked(bool(config.get("proactive.enabled", True)))
        self.chk_scheduled.setChecked(bool(config.get("proactive.scheduled_enabled", True)))
        self.times_edit.set_times(config.get("proactive.scheduled_times", []) or [])
        self.chk_idle.setChecked(bool(config.get("proactive.idle_enabled", True)))
        self.spin_idle_hours.setValue(int(config.get("proactive.idle_hours", 6) or 6))
        self.spin_idle_check.setValue(int(config.get("proactive.idle_check_interval_minutes", 15) or 15))
        self.chk_random.setChecked(bool(config.get("proactive.random_enabled", False)))
        self.spin_random_min.setValue(int(config.get("proactive.random_min_interval_minutes", 120) or 120))
        self.spin_random_max.setValue(int(config.get("proactive.random_max_interval_minutes", 300) or 300))

        active = config.get("proactive.active_hours", {}) or {}
        self.chk_active.setChecked(bool(active.get("enabled", True)))
        hour, minute = parse_hhmm(active.get("start", "08:00"), (8, 0))
        self.time_active_start.setTime(QTime(hour, minute))
        hour, minute = parse_hhmm(active.get("end", "23:00"), (23, 0))
        self.time_active_end.setTime(QTime(hour, minute))

        dnd = config.get("proactive.dnd_hours", {}) or {}
        self.chk_dnd.setChecked(bool(dnd.get("enabled", True)))
        hour, minute = parse_hhmm(dnd.get("start", "23:00"), (23, 0))
        self.time_dnd_start.setTime(QTime(hour, minute))
        hour, minute = parse_hhmm(dnd.get("end", "08:00"), (8, 0))
        self.time_dnd_end.setTime(QTime(hour, minute))

        self.spin_global.setValue(int(config.get("proactive.global_daily_limit", 10) or 0))
        self.spin_per_character.setValue(int(config.get("proactive.per_character_daily_limit", 3) or 0))
        self.spin_min_interval.setValue(int(config.get("proactive.min_interval_minutes", 30) or 0))
        self.dspin_probability.setValue(float(config.get("proactive.probability", 0.7) or 0.7))
        self.chk_avoid_repeat.setChecked(bool(config.get("proactive.avoid_repeat", True)))
        self.chk_include_memory.setChecked(bool(config.get("proactive.include_memory", True)))
        self.spin_max_chars.setValue(int(config.get("proactive.max_message_chars", 120) or 120))
        self.spin_context.setValue(int(config.get("proactive.context_messages", 8) or 8))

        self.ctx.request_status_refresh()

    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") in ("proactive_sent", "proactive_skipped", "config_reloaded"):
            self.ctx.request_status_refresh()


__all__ = ["ProactivePage", "format_hhmm"]
