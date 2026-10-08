"""首次运行配置引导（固定六步向导）。

设计要点
--------
* **固定步骤**：欢迎 → LLM 接口 → QQ 配置 → 角色卡 → 主动消息 → 完成，
  一共 6 步，不再随连接方式增减（连接方式只有 QQ 官方机器人一种）。
* **不依赖 Bot 进程**：LLM 连通性用 :mod:`app.llm_check` 直接测试；
  配置通过 :func:`app.config_store.save_config` 直接写 ``config.yaml``，
  最后由 Bot 热重载。这样即使 Bot 还没启动完成，引导也能走完。
* **可跳过**：底部始终有"取消引导（我自己配置）"，跳过时会记下
  ``app.onboarding_done = true``，不再自动弹出；之后可在「系统设置」里
  点"配置引导"重新打开。
* **可测试**：所有控件都作为属性暴露，关键动作（保存、导入、切换步骤）
  都是普通方法，便于自动化验证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger
from common.utils import truncate

from .config_store import save_config
from .icons import app_icon, app_pixmap
from .widgets.fields import add_form_row, ghost_button, primary_button
from .widgets.llm_form import LLMConfigForm
from .widgets.qq_form import QQConfigForm

log = get_logger("app.onboarding")

CARD_FILTER = (
    "SillyTavern 角色卡 (*.png *.json *.yaml *.yml);;"
    "PNG (*.png);;JSON (*.json);;YAML (*.yaml *.yml);;所有文件 (*)"
)

# 固定的 6 步：标题与稳定标识一一对应，编号与页面标题「第 N 步」一致
STEP_TITLES = ["欢迎", "LLM 接口", "QQ 配置", "角色卡", "主动消息", "完成"]

# 每一步的稳定标识：用于按 key 取标题、切页与刷新状态
STEP_KEYS = ["welcome", "llm", "qq", "characters", "proactive", "finish"]


class _StepItem(QFrame):
    """左侧步骤条目。"""

    def __init__(self, index: int, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("OnbStepItem")
        self.setProperty("state", "todo")
        self._number_text = str(index + 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)
        self.number = QLabel(self._number_text, self)
        self.number.setObjectName("OnbStepNumber")
        self.number.setFixedWidth(16)
        self.title = QLabel(title, self)
        self.title.setObjectName("OnbStepTitle")
        layout.addWidget(self.number)
        layout.addWidget(self.title, 1)

    def set_state(self, state: str) -> None:
        if self.get_state() == state:
            return
        self.setProperty("state", state)
        self.number.setText("✓" if state == "done" else self._number_text)
        for widget in (self, self.number, self.title):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def get_state(self) -> str:
        return str(self.property("state") or "todo")


class OnboardingWizard(QDialog):
    """分步配置向导。"""

    finished_setup = Signal(dict)  # 完成时携带保存的配置片段

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.index = 0
        self.skipped = False
        self._characters: List[Dict[str, Any]] = []
        self._builders: List[Callable[[QVBoxLayout], None]] = []

        self.setWindowTitle("BaiAi-Tavern 配置引导")
        self.setModal(True)
        width, height = 1120, 790
        try:
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                width = max(940, min(1280, int(available.width() * 0.92)))
                height = max(700, min(940, int(available.height() * 0.92)))
        except Exception:  # pragma: no cover - 无显示环境
            pass
        self.resize(width, height)
        self.setMinimumSize(960, 700)
        self.setWindowIcon(app_icon())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())
        self.stack = QStackedWidget(self)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        root.addWidget(self._build_footer())

        self._build_pages()
        self.goto_step(0)
        self.ctx.status_updated.connect(self._on_status)
        self.ctx.event_received.connect(self._on_event)
        self._restyle_all()

    # ============================================================== 布局骨架
    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setObjectName("OnbHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        icon = QLabel(header)
        icon.setPixmap(app_pixmap(44))
        layout.addWidget(icon)
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)
        title = QLabel("欢迎使用 BaiAi-Tavern", header)
        title.setObjectName("OnbTitle")
        titles.addWidget(title)
        subtitle = QLabel(
            "按提示走一遍就能用；中途可以跳过，之后在「系统设置」里随时改。",
            header,
        )
        subtitle.setObjectName("OnbSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(subtitle)
        layout.addLayout(titles, 1)
        return header

    def _build_rail(self) -> QWidget:
        rail = QWidget(self)
        rail.setObjectName("OnbRail")
        rail.setFixedWidth(196)
        self.rail_layout = QVBoxLayout(rail)
        self.rail_layout.setContentsMargins(12, 14, 12, 14)
        self.rail_layout.setSpacing(6)
        self.step_items: List[_StepItem] = []
        for index, title in enumerate(STEP_TITLES):
            item = _StepItem(index, title, self)
            self.step_items.append(item)
            self.rail_layout.addWidget(item)
        self.rail_layout.addStretch(1)
        # 提示标签直接加进布局，避免出现没有布局、默认停在 (0,0) 的重影标签
        self._rail_hint = self._make_rail_hint()
        self.rail_layout.addWidget(self._rail_hint)
        return rail

    def sequence(self) -> List[str]:
        """固定步骤序列（连接方式只有官方机器人，不再增减步骤）。"""
        return list(STEP_KEYS)

    def _make_rail_hint(self) -> QLabel:
        """左侧导航底部的"可以跳过"提示（固定宽度内自动换行）。"""
        label = QLabel("每一步都能先跳过，\n配置随时可以再改。", self)
        label.setObjectName("OnbRailHint")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        label.setMinimumWidth(120)
        return label

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        footer.setObjectName("OnbFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(10)
        self.lbl_footer_hint = QLabel("", footer)
        self.lbl_footer_hint.setObjectName("OnbMuted")
        self.lbl_footer_hint.setWordWrap(True)
        layout.addWidget(self.lbl_footer_hint, 1)
        self.btn_cancel = ghost_button("取消引导（我自己配置）", footer)
        self.btn_back = ghost_button("上一步", footer)
        self.btn_next = primary_button("下一步", footer)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_back.clicked.connect(self.prev_step)
        self.btn_next.clicked.connect(self.next_step)
        layout.addWidget(self.btn_cancel)
        layout.addWidget(self.btn_back)
        layout.addWidget(self.btn_next)
        return footer

    # ============================================================== 页面构建
    def _build_pages(self) -> None:
        self._builders = [
            self._build_welcome,
            self._build_llm,
            self._build_qq,
            self._build_characters,
            self._build_proactive,
            self._build_finish,
        ]
        for builder in self._builders:
            page = QWidget(self)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(22, 18, 22, 18)
            layout.setSpacing(12)
            builder(layout)
            layout.addStretch(1)
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            # 与主页面一致：内容可收缩，防止不换行的长文本把步骤页撑宽裁掉右侧
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            page.setMinimumWidth(0)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)

    def _page_head(self, layout: QVBoxLayout, title: str, subtitle: str) -> QLabel:
        """页面标题 + 说明。

        步骤编号统一由调用方写进标题里（``第 N 步：…``），N 与左侧步骤列表一一对应，
        6 步连续编号、不会跳号。
        """
        head = QLabel(title, self)
        head.setObjectName("OnbTitle")
        layout.addWidget(head)
        sub = QLabel(subtitle, self)
        sub.setObjectName("OnbSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        return head

    def _card(self, layout: QVBoxLayout, title: str = "") -> QVBoxLayout:
        card = QFrame(self)
        card.setObjectName("OnbCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)
        if title:
            label = QLabel(title, card)
            label.setObjectName("OnbCardTitle")
            card_layout.addWidget(label)
        layout.addWidget(card)
        return card_layout

    def _text(self, layout: QVBoxLayout, text: str, object_name: str = "OnbText") -> QLabel:
        label = QLabel(text, self)
        label.setObjectName(object_name)
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    # ---------------------------------------------------------------- 欢迎
    def _build_welcome(self, layout: QVBoxLayout) -> None:
        self._page_head(
            layout,
            "第 1 步：用 QQ 官方机器人就能玩，不需要小号",
            "只走 QQ 开放平台（官方机器人）：填两个凭据就能用，角色也已经内置好；"
            "点「下一步」按提示走一遍即可，任何一步都能跳过。",
        )
        card = self._card(layout, "① LLM 接口（角色的大脑）")
        self._text(
            card,
            "需要一个兼容 OpenAI 协议的地址和 API Key，例如 DeepSeek、OpenAI 或自建网关：\n"
            "下一个步骤可以下拉选服务商、点「获取模型列表」拉取模型，并当场「测试连接」。",
        )
        card = self._card(layout, "② QQ 官方机器人（唯一连接方式）")
        self._text(
            card,
            "在 q.qq.com 创建机器人，把 AppID / AppSecret 填进来即可，"
            "合规、不用扫码、不怕封号；\n"
            "主动消息按 openid 发送：让别人（或你自己的小号）先给机器人发一条消息，程序会自动记住。",
        )
        card = self._card(layout, "③ 角色（已内置 3 个，可直接用）")
        self._text(
            card,
            "程序自带「小栖 / 阿元 / 苏苏」三个默认角色，一键就能导入；"
            "也可以导入 SillyTavern 角色卡（PNG / JSON / YAML），或在「角色管理 → 新建角色」里自己写人设。",
        )
        self._text(
            layout,
            "说明：所有配置都保存在程序目录的 data\\config.yaml，界面里随时可改。"
            "官方机器人受平台规则保护，程序不依赖任何第三方 QQ 协议。",
            "OnbWarn",
        )

    # ------------------------------------------------------------- LLM
    def _build_llm(self, layout: QVBoxLayout) -> None:
        config = self.ctx.config
        self._page_head(
            layout,
            "第 2 步：选择服务商与模型",
            "先选一个常用服务商（会自动填好 Base URL），填入 API Key 后点「获取模型列表」从上游拉取可用模型；"
            "想用别的地址就选「自定义 / 其他」。",
        )

        card = self._card(layout)
        self.llm_form = LLMConfigForm(self.ctx, self)
        self.llm_form.set_values(
            base_url=str(config.get("llm.base_url", "") or ""),
            api_key=str(config.get("llm.api_key", "") or ""),
            model=str(config.get("llm.model", "") or ""),
        )
        card.addWidget(self.llm_form)

        self._text(
            layout,
            "「测试连接」会真的发一次最小请求：成功说明 Key 与模型都可用，失败会给出具体原因"
            "（Key 不正确 / 缺少 /v1 / 余额不足 / 超时）。API Key 只保存在本机 config.yaml 里。",
            "OnbMuted",
        )

    # ------------------------------------------------------------- QQ
    def _build_qq(self, layout: QVBoxLayout) -> None:
        config = self.ctx.config
        self._page_head(
            layout,
            "第 3 步：填写 QQ 官方机器人凭据",
            "只走 QQ 开放平台：在 q.qq.com 创建机器人并填 AppID / AppSecret，"
            "合规、不用小号，也不会封号。",
        )

        card = self._card(layout)
        self.qq_form = QQConfigForm(self.ctx, self)
        self.qq_form.set_values(config)
        card.addWidget(self.qq_form)

        # ------------------------------------------------------ 绑定角色
        bind_card = self._card(layout, "这个机器人由哪个角色说话")
        bind_form = QFormLayout()
        bind_form.setContentsMargins(0, 0, 0, 0)
        bind_form.setSpacing(8)
        bind_row = QWidget(self)
        bind_layout = QHBoxLayout(bind_row)
        bind_layout.setContentsMargins(0, 0, 0, 0)
        bind_layout.setSpacing(8)
        self.combo_character = QComboBox(bind_row)
        self.combo_character.setMinimumWidth(260)
        self.combo_character.addItem("（不绑定：按“上次发言 / 随机”选择）", "")
        self.btn_refresh_roles = ghost_button("刷新角色", bind_row)
        self.btn_refresh_roles.clicked.connect(self._refresh_characters)
        bind_layout.addWidget(self.combo_character)
        bind_layout.addWidget(self.btn_refresh_roles)
        bind_layout.addStretch(1)
        add_form_row(
            bind_form,
            "绑定角色",
            bind_row,
            "绑定后这个机器人的回复与主动消息都用该角色的人设（#角色名 仍可临时换人）；"
            "多个机器人各绑不同角色，就能同时扮演不同人格",
        )
        bind_card.addLayout(bind_form)

        self._text(
            layout,
            "官方机器人：在 https://q.qq.com 创建应用 → 记下 AppID / AppSecret → 把机器人加为好友或拉进群。\n"
            "主动消息目标留空即可：让第一个给机器人发消息的人自动成为收信人（也可以手填 openid）。\n"
            "想再挂一个机器人：装好之后在左侧打开「机器人」页面点「＋ 新增机器人」。",
            "OnbMuted",
        )

    # ------------------------------------------------------------- 角色卡
    def _build_characters(self, layout: QVBoxLayout) -> None:
        self._page_head(layout, "第 4 步：导入角色卡", "至少导入一个角色，主动消息才有角色可用。")

        card = self._card(layout, "导入")
        buttons = QHBoxLayout()
        self.btn_import_builtin = primary_button("导入内置角色（推荐）")
        self.btn_import_files = ghost_button("导入角色卡文件")
        self.btn_import_dir = ghost_button("从文件夹导入")
        self.btn_scan = ghost_button("扫描 data\\characters")
        self.btn_import_builtin.clicked.connect(self._import_builtin)
        self.btn_import_files.clicked.connect(self._pick_files)
        self.btn_import_dir.clicked.connect(self._pick_directory)
        self.btn_scan.clicked.connect(lambda: self.scan_directory())
        buttons.addWidget(self.btn_import_builtin)
        buttons.addWidget(self.btn_import_files)
        buttons.addWidget(self.btn_import_dir)
        buttons.addWidget(self.btn_scan)
        buttons.addStretch(1)
        card.addLayout(buttons)
        self.lbl_char_summary = QLabel("正在读取角色列表…")
        self.lbl_char_summary.setObjectName("OnbMuted")
        card.addWidget(self.lbl_char_summary)

        card = self._card(layout, "已导入的角色")
        self.list_characters = QListWidget(self)
        self.list_characters.setObjectName("OnbList")
        self.list_characters.setMinimumHeight(140)
        card.addWidget(self.list_characters)
        self._text(
            layout,
            "「导入内置角色」会用程序自带的 3 个默认角色（温柔陪伴 / 元气搭子 / 吐槽督促），"
            "想自己写人设也可以之后在「角色管理 → 新建角色」里直接创建，不需要角色卡。",
            "OnbMuted",
        )

    # ------------------------------------------------------------- 主动消息
    def _build_proactive(self, layout: QVBoxLayout) -> None:
        config = self.ctx.config
        self._page_head(
            layout,
            "第 5 步：主动消息设置",
            "决定角色在什么时候主动找你说话（默认较保守）。",
        )

        card = self._card(layout)
        self.chk_proactive = QCheckBox("启用主动消息")
        self.chk_proactive.setChecked(bool(config.get("proactive.enabled", True)))
        card.addWidget(self.chk_proactive)

        self.chk_sched = QCheckBox("定时触发：每天 09:00 与 21:00")
        self.chk_sched.setChecked(bool(config.get("proactive.scheduled_enabled", True)))
        card.addWidget(self.chk_sched)
        self.chk_idle = QCheckBox("空闲触发：我超过 6 小时没说话时")
        self.chk_idle.setChecked(bool(config.get("proactive.idle_enabled", True)))
        card.addWidget(self.chk_idle)
        self.chk_random = QCheckBox("随机触发：活跃时段内随机找我（默认关闭）")
        self.chk_random.setChecked(bool(config.get("proactive.random_enabled", False)))
        card.addWidget(self.chk_random)

        form = QFormLayout()
        form.setContentsMargins(0, 6, 0, 0)
        form.setSpacing(10)
        limits = QHBoxLayout()
        self.spin_global = QSpinBox()
        self.spin_global.setRange(0, 100)
        self.spin_global.setFixedWidth(90)
        self.spin_global.setValue(int(config.get("proactive.global_daily_limit", 10) or 10))
        self.spin_per_char = QSpinBox()
        self.spin_per_char.setRange(0, 50)
        self.spin_per_char.setFixedWidth(90)
        self.spin_per_char.setValue(int(config.get("proactive.per_character_daily_limit", 3) or 3))
        limits.addWidget(QLabel("每天最多"))
        limits.addWidget(self.spin_global)
        limits.addWidget(QLabel("条，单个角色最多"))
        limits.addWidget(self.spin_per_char)
        limits.addWidget(QLabel("条"))
        limits.addStretch(1)
        form.addRow("频率上限", limits)
        card.addLayout(form)

        self._text(
            layout,
            "免打扰默认 23:00–08:00，活跃时段默认 08:00–23:00，两次发言至少间隔 30 分钟——"
            "这些都可以在「消息设置」页面里改。",
            "OnbMuted",
        )

    # ---------------------------------------------------------------- 完成
    def _build_finish(self, layout: QVBoxLayout) -> None:
        self._page_head(layout, "第 6 步：确认一下", "点「完成」后配置会立即生效，之后可随时到「系统设置」修改。")
        card = self._card(layout, "配置检查")
        self.summary_layout = QVBoxLayout()
        self.summary_layout.setContentsMargins(0, 0, 0, 0)
        self.summary_layout.setSpacing(6)
        card.addLayout(self.summary_layout)
        self.lbl_finish_hint = self._text(
            layout,
            "完成后：Bot 会继续在后台运行，主动消息按上面的设置执行；"
            "窗口关掉后会最小化到系统托盘。",
            "OnbMuted",
        )

    # ============================================================== 步骤切换
    @property
    def step_count(self) -> int:
        return len(self.sequence())

    @property
    def last_index(self) -> int:
        return self.step_count - 1

    @property
    def current_key(self) -> str:
        keys = self.sequence()
        return keys[max(0, min(self.index, len(keys) - 1))] if keys else ""

    def goto_step(self, index: int) -> None:
        keys = self.sequence()
        index = max(0, min(int(index), len(keys) - 1))
        self.index = index
        self.stack.setCurrentIndex(index)
        for position, item in enumerate(self.step_items):
            if position < index:
                item.set_state("done")
            elif position == index:
                item.set_state("current")
            else:
                item.set_state("todo")
        self.btn_back.setEnabled(index > 0)
        self.btn_next.setText("完成" if index == self.last_index else "下一步")
        # 最后一步是「确认一下」：此时要么完成、要么返回，
        # 「取消引导」留着会让人误以为引导已经结束了，隐藏掉（Esc / 关窗仍等价于跳过）
        self.btn_cancel.setVisible(index != self.last_index)
        self.lbl_footer_hint.setText("第 %d / %d 步" % (index + 1, self.step_count))
        self._on_step_entered(keys[index])

    def next_step(self) -> None:
        if self.index >= self.last_index:
            self.finish()
        else:
            self.goto_step(self.index + 1)

    def prev_step(self) -> None:
        self.goto_step(self.index - 1)

    def _on_step_entered(self, key: str) -> None:
        if key == "characters":
            self._refresh_characters()
        elif key == "finish":
            self._refresh_summary()

    def _on_status(self, snapshot: Dict[str, Any]) -> None:
        if not self.isVisible():
            return
        if self.current_key == "finish":
            self._refresh_summary()

    def _on_event(self, event: Dict[str, Any]) -> None:
        if not self.isVisible():
            return
        if event.get("type") == "characters_changed" and self.current_key == "characters":
            self._refresh_characters()

    # ============================================================== 各步行为
    def _refresh_characters(self) -> None:
        self.lbl_char_summary.setText("正在读取角色列表…")

        def _ok(result: Any) -> None:
            characters = result if isinstance(result, list) else []
            self._apply_characters(characters)

        def _error(message: str) -> None:
            self.lbl_char_summary.setText("暂时无法读取角色列表（%s）；Bot 就绪后可点「扫描」重试。" % truncate(message, 60))
            self._set_status_color(self.lbl_char_summary, "warn")

        self.ctx.run_task(self.ctx.api.characters, on_ok=_ok, on_error=_error, key="onb_characters", label="读取角色列表")

    def _apply_characters(self, characters: List[Dict[str, Any]]) -> None:
        self._characters = characters
        enabled = len([item for item in characters if int(item.get("enabled") or 0) == 1])
        if characters:
            self.lbl_char_summary.setText("已导入 %d 个角色，其中 %d 个参与主动消息。" % (len(characters), enabled))
            self._set_status_color(self.lbl_char_summary, "ok")
        else:
            self.lbl_char_summary.setText("还没有角色，点上面的按钮导入一个吧。")
            self._set_status_color(self.lbl_char_summary, "warn")
        self.list_characters.clear()
        for item in characters:
            label = str(item.get("name") or "未命名")
            if int(item.get("enabled") or 0) != 1:
                label += "（未启用）"
            entry = QListWidgetItem(label, self.list_characters)
            entry.setData(Qt.UserRole, str(item.get("id") or ""))
        self._sync_character_combo()

    def _sync_character_combo(self) -> None:
        """把「绑定角色」下拉与当前角色列表同步（保留用户已选的角色）。"""
        combo = getattr(self, "combo_character", None)
        if combo is None:
            return
        current = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（不绑定：按“上次发言 / 随机”选择）", "")
        for item in self._characters:
            label = str(item.get("name") or "未命名")
            if int(item.get("enabled") or 0) != 1:
                label += "（未启用，不会说话）"
            combo.addItem(label, str(item.get("id") or ""))
        position = combo.findData(current)
        combo.setCurrentIndex(position if position >= 0 else 0)
        combo.blockSignals(False)

    def _import_builtin(self) -> None:
        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            self.lbl_footer_hint.setText("已导入 %d 个内置角色。" % len(imported))
            self._refresh_characters()

        self.ctx.run_task(
            self.ctx.api.import_builtin_characters,
            on_ok=_ok,
            key="onb_import_builtin",
            label="导入内置角色",
        )

    def _pick_files(self) -> None:
        directory = str(self.ctx.config.effective_characters_path())
        files, _ = QFileDialog.getOpenFileNames(self, "选择 SillyTavern 角色卡", directory, CARD_FILTER)
        if files:
            self.import_paths([Path(item) for item in files])

    def _pick_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择包含角色卡的文件夹", str(self.ctx.config.effective_characters_path())
        )
        if directory:
            self.import_directory(Path(directory))

    def import_paths(self, paths: List[Path]) -> None:
        """导入若干角色卡文件（供界面按钮与自检直接调用）。"""
        if not paths:
            return
        self.lbl_char_summary.setText("正在导入 %d 个文件…" % len(paths))

        def _work() -> Dict[str, Any]:
            created, updated, failed = [], [], []
            for path in paths:
                try:
                    result = self.ctx.api.import_character(Path(path))
                    name = (result.get("character") or {}).get("name") or Path(path).name
                    (created if result.get("status") == "created" else updated).append(str(name))
                except Exception as exc:
                    failed.append("%s：%s" % (Path(path).name, exc))
            return {"created": created, "updated": updated, "failed": failed}

        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            parts = []
            if data.get("created"):
                parts.append("新增 %d 个：%s" % (len(data["created"]), "、".join(data["created"])))
            if data.get("updated"):
                parts.append("更新 %d 个：%s" % (len(data["updated"]), "、".join(data["updated"])))
            self.lbl_footer_hint.setText("导入完成。" + "；".join(parts) if parts else "没有导入任何角色。")
            if data.get("failed"):
                QMessageBox.warning(self, "部分角色卡导入失败", "\n".join(data["failed"][:10]))
            self._refresh_characters()

        self.ctx.run_task(_work, on_ok=_ok, label="导入角色卡")

    def import_directory(self, directory: Path) -> None:
        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            self.lbl_footer_hint.setText("目录导入完成：成功 %d 个，失败 %d 个。" % (len(imported), len(failed)))
            if failed:
                QMessageBox.warning(
                    self,
                    "部分角色卡导入失败",
                    "\n".join("%s：%s" % (item["file"], item["error"]) for item in failed[:10]),
                )
            self._refresh_characters()

        self.ctx.run_task(
            self.ctx.api.import_character_path, str(directory), on_ok=_ok, key="onb_import_dir", label="从文件夹导入"
        )

    def scan_directory(self) -> None:
        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            self.lbl_footer_hint.setText("扫描完成：新增/更新 %d 个角色。" % len(imported))
            self._refresh_characters()

        self.ctx.run_task(self.ctx.api.scan_characters, on_ok=_ok, key="onb_scan", label="扫描角色卡目录")

    # ============================================================== 汇总与保存
    def _collect(self) -> Dict[str, Any]:
        llm = {key: value for key, value in self.llm_form.values().items() if value}
        qq = self.qq_form.values()
        # 第 1 个机器人的身份：名称 + 绑定角色（多机器人时在「机器人」页面继续添加）
        character_id = ""
        combo = getattr(self, "combo_character", None)
        if combo is not None:
            character_id = str(combo.currentData() or "")
        existing_name = str(self.ctx.config.get("qq.name", "") or "").strip()
        qq["name"] = existing_name or "机器人 1"
        qq["enabled"] = True
        qq["character_id"] = character_id
        qq["character_name"] = str(combo.currentText()) if (combo is not None and character_id) else ""
        return {
            "llm": llm,
            # QQ 官方机器人的凭据与消息行为由共享控件统一给出
            "qq": qq,
            "proactive": {
                "enabled": self.chk_proactive.isChecked(),
                "scheduled_enabled": self.chk_sched.isChecked(),
                "idle_enabled": self.chk_idle.isChecked(),
                "random_enabled": self.chk_random.isChecked(),
                "global_daily_limit": self.spin_global.value(),
                "per_character_daily_limit": self.spin_per_char.value(),
            },
            "app": {"onboarding_done": True},
        }

    def _refresh_summary(self) -> None:
        while self.summary_layout.count():
            item = self.summary_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        qq_values = self.qq_form.values()
        official = qq_values.get("official") or {}
        app_id = str(official.get("app_id") or "")
        llm_values = self.llm_form.values()
        key_filled = bool(llm_values["api_key"])
        status = self.ctx.last_status or {}
        qq_state = status.get("qq") or {}
        enabled_chars = len([item for item in self._characters if int(item.get("enabled", 0) or 0) == 1])
        character_id = str(self.combo_character.currentData() or "") if hasattr(self, "combo_character") else ""

        target_openid = str(official.get("target_openid") or "")
        group_openid = str(official.get("group_openid") or "")
        if group_openid:
            target_desc = "主动消息群 openid：%s" % group_openid
        elif target_openid:
            target_desc = "主动消息对象 openid：%s" % target_openid
        else:
            target_desc = "主动消息对象：留空，按 openid 自动记住最近给机器人发消息的人"
        qq_desc = (
            "已连接官方网关（%s）" % (qq_state.get("nickname") or app_id)
            if qq_state.get("connected")
            else ("已填 AppID %s，等待连接…" % app_id if app_id else "尚未填写 AppID / AppSecret")
        )
        qq_level = "ok" if qq_state.get("connected") else ("warn" if app_id else "bad")

        rows: List[Tuple[str, str, str]] = [
            (
                "LLM 接口",
                "模型 %s；地址 %s%s"
                % (
                    llm_values["model"] or "未选择",
                    llm_values["base_url"] or "未填写",
                    "（已测试通过）" if self.llm_form.last_result and self.llm_form.last_result.get("ok") else "",
                )
                if key_filled or llm_values["base_url"]
                else "尚未配置（角色无法生成回复）",
                ("ok" if key_filled else ("warn" if llm_values["base_url"] else "bad")),
            ),
            (
                "QQ 官方机器人",
                "唯一连接方式（QQ 开放平台）　·　" + target_desc,
                "ok",
            ),
            ("QQ 状态", qq_desc, qq_level),
            (
                "角色",
                (
                    "已导入 %d 个，其中 %d 个启用" % (len(self._characters), enabled_chars)
                    if self._characters
                    else "还没有角色（可在上一步导入）"
                )
                + (
                    "　·　机器人绑定：%s"
                    % (self.combo_character.currentText() if character_id else "未绑定（随机选择）")
                ),
                ("ok" if enabled_chars else "warn"),
            ),
            (
                "主动消息",
                ("已启用；每天最多 %d 条，单角色 %d 条" % (self.spin_global.value(), self.spin_per_char.value()))
                if self.chk_proactive.isChecked()
                else "已关闭（只被动回复）",
                ("ok" if self.chk_proactive.isChecked() else "muted"),
            ),
        ]
        for name, value, level in rows:
            row = QWidget(self)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            name_label = QLabel(name, row)
            name_label.setObjectName("OnbCardTitle")
            name_label.setFixedWidth(90)
            value_label = QLabel(value, row)
            value_label.setObjectName({"ok": "OnbOk", "warn": "OnbWarn", "bad": "OnbBad"}.get(level, "OnbMuted"))
            value_label.setWordWrap(True)
            layout.addWidget(name_label)
            layout.addWidget(value_label, 1)
            self.summary_layout.addWidget(row)

    def finish(self) -> None:
        patch = self._collect()
        save_config(self.ctx.config, patch)
        self.ctx.reload_config()
        # 让 Bot 立即应用（Bot 未就绪时忽略失败，它会在 30 秒内自动热重载）
        self.ctx.run_task(
            self.ctx.api.reload_config,
            on_ok=lambda _r: None,
            on_error=lambda message: log.debug("通知 Bot 重载配置失败（将自动热重载）：%s", message),
            key="onb_reload",
            label="通知 Bot 重载配置",
        )
        official = (patch.get("qq") or {}).get("official") or {}
        llm_values = patch.get("llm") or {}
        # 注意：_collect() 会过滤掉空值（避免用空串覆盖已有配置），
        # 所以 LLM 一项可能根本没有 api_key 键（用户留空直接完成），这里必须用 .get()
        log.info(
            "配置引导完成：LLM=%s，QQ 官方 AppID=%s，主动消息=%s",
            bool(llm_values.get("api_key")),
            bool(str(official.get("app_id") or "").strip()),
            bool((patch.get("proactive") or {}).get("enabled")),
        )
        self.finished_setup.emit(patch)
        self.accept()

    def cancel(self) -> None:
        """跳过引导：只记下“不再自动弹出”，不保存用户在向导里填的内容。"""
        self._mark_skipped()
        super().reject()

    def reject(self) -> None:  # noqa: D102 - 覆盖：Esc / 右上角关闭都等同跳过
        self._mark_skipped()
        super().reject()

    def _mark_skipped(self) -> None:
        if self.skipped:
            return
        self.skipped = True
        save_config(self.ctx.config, {"app": {"onboarding_done": True}})
        log.info("用户跳过配置引导，不再自动弹出；可在系统设置里手动配置或重新运行引导")

    # ============================================================== 样式工具
    def _set_status_color(self, label: QLabel, level: str) -> None:
        name = {"ok": "OnbOk", "warn": "OnbWarn", "bad": "OnbBad"}.get(level, "OnbMuted")
        if label.objectName() != name:
            label.setObjectName(name)
        label.style().unpolish(label)
        label.style().polish(label)

    def _restyle_all(self) -> None:
        for widget in self.findChildren(QWidget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


__all__ = ["OnboardingWizard", "STEP_TITLES"]
