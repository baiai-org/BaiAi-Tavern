"""QQ 官方机器人连接设置控件（「官方机器人连接设置」）。

「配置引导」「系统设置」和「机器人管理」共用，保证几处一致：
上半部分显示实时连接状态，下半部分是开放平台需要的全部字段。

控件支持**配置前缀**（``prefix``）与**机器人 id**（``bot_id``）：

* 系统设置 / 配置引导用默认的 ``prefix="qq"``，编辑第 1 个机器人；
* 「机器人管理」对第 2..N 个机器人使用 ``prefix="bots.<下标>"``，
  测试连接 / 重新连接也会带上 ``bot_id`` 调用对应机器人自己的接口。

连接的**唯一方式**是 QQ 开放平台（官方机器人）：只需要 AppID / AppSecret，
合规、不用小号扫码；主动消息按 openid 发送，别人先给机器人发一条消息即可自动记住。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger

from .fields import add_form_row, ghost_button, hint_label

log = get_logger("app.qq_form")

# 连接方式只有官方机器人一种；保留常量是为了让其它模块继续按名字引用
#: 「测试连接 / 重新连接」的结果在这么多秒内不被状态轮询覆盖（用户能看清反馈）
STICKY_SECONDS = 8.0

MODE_OFFICIAL = "official"


class QQConfigForm(QWidget):
    """官方机器人字段 + 连接状态。"""

    def __init__(
        self,
        ctx,
        parent: Optional[QWidget] = None,
        prefix: str = "qq",
        bot_id: str = "",
        title: str = "",
    ):
        super().__init__(parent)
        self.ctx = ctx
        self.prefix = str(prefix or "qq")
        self.bot_id = str(bot_id or "")
        self._key_tag = str(id(self))
        self.last_test: Optional[Dict[str, Any]] = None
        self._sticky_until: float = 0.0
        self._title = title

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ------------------------------------------------------------ 状态
        head = QFormLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)

        status_row = QWidget(self)
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        self.lbl_status = QLabel("尚未检测")
        self.lbl_status.setObjectName("MutedLabel")
        self.lbl_status.setWordWrap(True)
        self.btn_test = ghost_button("测试连接", status_row)
        self.btn_reconnect = ghost_button("重新连接", status_row)
        self.btn_test.clicked.connect(self.test_connection)
        self.btn_reconnect.clicked.connect(self.reconnect)
        status_layout.addWidget(self.btn_test)
        status_layout.addWidget(self.btn_reconnect)
        status_layout.addWidget(self.lbl_status, 1)
        add_form_row(
            head,
            "连接状态",
            status_row,
            "走 QQ 开放平台（官方机器人）：合规、不用扫码，也不会封号",
        )
        layout.addLayout(head)

        # ------------------------------------------------------------ 字段
        layout.addWidget(self._build_official_page())

        self.refresh_status()

    # ============================================================== 官方机器人
    def _build_official_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        self.in_app_id = QLineEdit(page)
        self.in_app_id.setPlaceholderText("在 https://q.qq.com 机器人页面可以看到")
        self.in_app_id.setFixedWidth(280)
        add_form_row(form, "AppID", self.in_app_id, "QQ 开放平台 → 你的机器人 → 开发设置")

        key_row = QWidget(page)
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.in_app_secret = QLineEdit(key_row)
        self.in_app_secret.setEchoMode(QLineEdit.Password)
        self.in_app_secret.setFixedWidth(240)
        self.btn_show_secret = ghost_button("显示", key_row)
        self.btn_show_secret.setProperty("chip", True)
        self.btn_show_secret.setCheckable(True)
        self.btn_show_secret.toggled.connect(
            lambda checked: self.in_app_secret.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        key_layout.addWidget(self.in_app_secret)
        key_layout.addWidget(self.btn_show_secret)
        key_layout.addStretch(1)
        add_form_row(form, "AppSecret", key_row, "只保存在本机 config.yaml；用于换取 access_token")

        self.chk_sandbox = QCheckBox("沙盒模式（只对沙盒测试成员生效）")
        form.addRow(self.chk_sandbox)

        target_row = QWidget(page)
        target_layout = QHBoxLayout(target_row)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(6)
        self.in_target_openid = QLineEdit(target_row)
        self.in_target_openid.setPlaceholderText("留空 = 自动使用最近给机器人发消息的用户")
        self.in_target_openid.setFixedWidth(320)
        self.btn_forget_openid = ghost_button("忘记已记住的", target_row)
        self.btn_forget_openid.clicked.connect(self.forget_openid)
        target_layout.addWidget(self.in_target_openid)
        target_layout.addWidget(self.btn_forget_openid)
        target_layout.addStretch(1)
        add_form_row(
            form,
            "主动消息目标",
            target_row,
            "官方平台只能按 openid 发送：让别人（或你自己的小号）先给机器人发一条消息，就会自动记住",
        )
        self.lbl_learned = hint_label("")
        form.addRow("", self.lbl_learned)

        self.in_group_openid = QLineEdit(page)
        self.in_group_openid.setPlaceholderText("可选：把主动消息发到某个群")
        self.in_group_openid.setFixedWidth(320)
        add_form_row(form, "主动消息群", self.in_group_openid, "填了群 openid 就优先发到群里")

        self.chk_allow_all = QCheckBox("私聊响应所有用户")
        self.chk_allow_all.setChecked(True)
        form.addRow(self.chk_allow_all)
        self.in_allowed_users = QLineEdit(page)
        self.in_allowed_users.setPlaceholderText("openid 白名单，用逗号分隔（仅在上面的开关关闭时生效）")
        add_form_row(form, "用户白名单", self.in_allowed_users)

        self.in_allowed_groups = QLineEdit(page)
        self.in_allowed_groups.setPlaceholderText("群 openid 白名单，留空表示不限")
        add_form_row(form, "群白名单", self.in_allowed_groups, "需要先打开「响应群聊 @」")

        self.chk_official_group = QCheckBox("响应群聊里对我的 @")
        form.addRow(self.chk_official_group)

        self.chk_markdown = QCheckBox("使用 markdown 消息（需要平台权限）")
        form.addRow(self.chk_markdown)

        row = QWidget(page)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(10)
        self.spin_official_len = QSpinBox(row)
        self.spin_official_len.setRange(20, 2000)
        self.spin_official_len.setFixedWidth(100)
        self.spin_official_segments = QSpinBox(row)
        self.spin_official_segments.setRange(1, 10)
        self.spin_official_segments.setFixedWidth(90)
        row_layout.addWidget(QLabel("单条上限"))
        row_layout.addWidget(self.spin_official_len)
        row_layout.addWidget(QLabel("最多条数"))
        row_layout.addWidget(self.spin_official_segments)
        row_layout.addStretch(1)
        add_form_row(form, "长消息拆分", row)

        form.addRow(
            hint_label(
                "说明：官方机器人需要在 https://q.qq.com 创建应用并把机器人加入你的好友/群；\n"
                "被动回复有效期 5 分钟，且发送消息要求本程序保持运行（网关在线）。\n"
                "「主动消息」需要在开放平台申请权限，且平台有每日额度限制。"
            )
        )
        return page

    # ============================================================== 取值/赋值
    def _get(self, key: str, default: Any = None) -> Any:
        """按本控件的前缀读配置（``qq.x`` 或 ``bots.2.x``）。"""
        value = self.ctx.config.get("%s.%s" % (self.prefix, key), default)
        return default if value is None else value

    def values(self) -> Dict[str, Any]:
        official = {
            "app_id": self.in_app_id.text().strip(),
            "app_secret": self.in_app_secret.text().strip(),
            "sandbox": self.chk_sandbox.isChecked(),
            "target_openid": self.in_target_openid.text().strip(),
            "group_openid": self.in_group_openid.text().strip(),
            "allow_all_users": self.chk_allow_all.isChecked(),
            "allowed_users": [item.strip() for item in self.in_allowed_users.text().split(",") if item.strip()],
            "allowed_groups": [item.strip() for item in self.in_allowed_groups.text().split(",") if item.strip()],
            "markdown": self.chk_markdown.isChecked(),
            "max_reply_segments": self.spin_official_segments.value(),
            "reply_segment_max_len": self.spin_official_len.value(),
        }
        return {
            "official": official,
            "group_reply_enabled": self.chk_official_group.isChecked(),
        }

    def patch(self) -> Dict[str, Any]:
        """兼容旧调用点：主机器人的连接配置写在 ``qq:`` 段。"""
        if self.prefix == "qq":
            return {"qq": self.values()}
        return {"bots": self.values()}

    def set_values(self, config) -> None:
        official = self._get("official", {}) or {}

        self.in_app_id.setText(str(official.get("app_id", "") or ""))
        self.in_app_secret.setText(str(official.get("app_secret", "") or ""))
        self.chk_sandbox.setChecked(bool(official.get("sandbox", False)))
        self.in_target_openid.setText(str(official.get("target_openid", "") or ""))
        self.in_group_openid.setText(str(official.get("group_openid", "") or ""))
        self.chk_allow_all.setChecked(bool(official.get("allow_all_users", True)))
        self.in_allowed_users.setText(", ".join(str(item) for item in (official.get("allowed_users") or [])))
        self.in_allowed_groups.setText(", ".join(str(item) for item in (official.get("allowed_groups") or [])))
        self.chk_markdown.setChecked(bool(official.get("markdown", False)))
        self.spin_official_segments.setValue(int(official.get("max_reply_segments", 3) or 3))
        self.spin_official_len.setValue(int(official.get("reply_segment_max_len", 200) or 200))
        self.chk_official_group.setChecked(bool(self._get("group_reply_enabled", False)))

    # ============================================================== 状态
    def refresh_status(self, status: Optional[Dict[str, Any]] = None) -> None:
        # 「测试连接 / 重新连接」的结果是瞬时反馈，几秒内不要被轮询刷掉，
        # 否则用户点了按钮几乎看不到结果。
        if self._sticky_until and time.monotonic() < self._sticky_until:
            qq = self._bot_state(status)
            self._refresh_learned(qq, status)
            return

        status = status or (self.ctx.last_status or {})
        qq = self._bot_state(status)

        connected = bool(qq.get("connected"))
        app_id = str(qq.get("app_id") or self.in_app_id.text().strip())
        if not app_id or not qq.get("configured", True):
            text, level = "未配置：请填写 AppID / AppSecret", "warn"
        elif connected:
            name = qq.get("nickname") or ""
            text = "已连接官方网关%s" % ("（%s）" % name if name else "")
            level = "ok"
        else:
            text = "未连接：%s" % (qq.get("error") or "网关正在连接…")
            level = "warn"

        self._refresh_learned(qq, status)
        self._set_status(text, level)

    def _refresh_learned(self, qq: Dict[str, Any], status: Optional[Dict[str, Any]] = None) -> None:
        status = status or (self.ctx.last_status or {})
        learned = str(
            qq.get("last_user_openid")
            or qq.get("target_openid")
            or (status.get("last_user_openid") if self.prefix == "qq" else "")
            or ""
        )
        self.lbl_learned.setText(
            "已记住的私聊对象：%s" % learned if learned else "还没有记住任何用户：先给别人发个消息试试"
        )

    def _bot_state(self, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """从状态快照里取出本控件对应机器人的连接状态。"""
        status = status or (self.ctx.last_status or {})
        bots = status.get("bots")
        if isinstance(bots, list):
            if self.bot_id:
                for item in bots:
                    if str(item.get("id") or "") == self.bot_id:
                        return item
            elif self.prefix == "qq" and bots:
                return bots[0]
        return status.get("qq") or {}

    def _set_status(self, text: str, level: str = "muted") -> None:
        name = {"ok": "StatusOk", "warn": "StatusWarn", "bad": "StatusBad"}.get(level, "MutedLabel")
        if self.lbl_status.objectName() != name:
            self.lbl_status.setObjectName(name)
        self.lbl_status.setText(text)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self._sticky_until = 0.0

    def _show_result(self, text: str, level: str = "muted") -> None:
        """显示操作结果（测试连接 / 重新连接）：``STICKY_SECONDS`` 秒内不被轮询覆盖。"""
        self._set_status(text, level)
        self._sticky_until = time.monotonic() + STICKY_SECONDS

    # ============================================================== 动作
    def test_connection(self) -> None:
        """先保存当前填写内容，再让 Bot 去测试（凭证 + 机器人信息 + 网关）。"""
        self._set_status("正在保存配置并测试…", "muted")
        self.btn_test.setEnabled(False)
        bot_id = self.bot_id

        def _work() -> Dict[str, Any]:
            if bot_id:
                self.ctx.api.update_bot(bot_id, self.values())
                return self.ctx.api.test_bot(bot_id)
            self.ctx.api.put_config(self.patch())
            return self.ctx.api.qq_test()

        def _ok(result: Any) -> None:
            self.btn_test.setEnabled(True)
            data = result if isinstance(result, dict) else {}
            self.last_test = data
            if data.get("available"):
                name = data.get("nickname") or ""
                self._show_result("✓ 连接成功%s" % ("（%s）" % name if name else ""), "ok")
            else:
                self._show_result("✗ %s" % (data.get("error") or "测试失败"), "bad")
            self.ctx.reload_config()
            self.ctx.request_status_refresh()

        def _error(message: str) -> None:
            self.btn_test.setEnabled(True)
            self._show_result("✗ %s" % message, "bad")

        self.ctx.run_task(_work, on_ok=_ok, on_error=_error, key="qq_test_%s" % self._key_tag, label="测试 QQ 连接")

    def reconnect(self) -> None:
        self._set_status("正在重新连接…", "muted")
        bot_id = self.bot_id

        def _work() -> Dict[str, Any]:
            if bot_id:
                self.ctx.api.update_bot(bot_id, self.values())
                return self.ctx.api.reconnect_bot(bot_id)
            self.ctx.api.put_config(self.patch())
            return self.ctx.api.qq_reconnect()

        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            self.ctx.reload_config()
            self.ctx.request_status_refresh()
            self._show_result(str(data.get("message") or "已重新连接"), "ok" if data.get("ok") else "warn")

        def _error(message: str) -> None:
            self._show_result("重新连接失败：%s" % message, "bad")

        self.ctx.run_task(_work, on_ok=_ok, on_error=_error, key="qq_reconnect_%s" % self._key_tag, label="重新连接 QQ")

    def forget_openid(self) -> None:
        answer = QMessageBox.question(
            self,
            "忘记已记住的 openid",
            "确定要清除自动记住的 openid 吗？清除后主动消息会发给下一个给机器人发消息的人。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.in_target_openid.setText("")
        if self.bot_id:
            self.ctx.run_task(
                self.ctx.api.forget_bot_openid,
                self.bot_id,
                on_ok=lambda _r: self.ctx.request_status_refresh(),
                label="清除 openid",
            )
            return
        self.ctx.run_task(
            self.ctx.api.qq_forget_openid,
            on_ok=lambda _r: (self.ctx.request_status_refresh(), self._set_status("已清除记住的 openid", "muted")),
            label="清除 openid",
        )


__all__ = ["MODE_OFFICIAL", "QQConfigForm"]
