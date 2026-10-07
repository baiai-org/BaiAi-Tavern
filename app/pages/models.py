"""模型路由页面（V0.2）：不同能力走不同的 API 线路。

三个槽位（图像理解 / 图像生成 / 文字转语音）各自独立配置，
外加富媒体行为（语音回复概率、发图开关等）。保存后 Bot 热重载即生效。

听语音（语音转文字）不占槽位：QQ 官方平台随语音消息推送参考转写，零配置可用。

主模型（文字对话）统一在「系统设置 → LLM」段配置，本页不重复，避免两处不一致。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common.providers import ALL_SLOTS, SLOT_CHAT, SLOT_LABELS

from ..config_store import gui_config, save_config
from ..widgets.fields import add_form_row, hint_label, primary_button
from ..widgets.provider_form import ProviderSlotForm
from .base import Page


class _ProbabilityRow(QWidget):
    """0–100 的滑动条 + 百分比显示。"""

    changed = Signal()

    def __init__(self, value: float = 0.3, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(value * 100)))
        self.slider.valueChanged.connect(self._on_changed)
        self.label = QLabel(self)
        self.label.setObjectName("ValueLabel")
        self.label.setFixedWidth(48)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._sync_label()
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)

    def _sync_label(self) -> None:
        self.label.setText("%d%%" % self.slider.value())

    def _on_changed(self, _value: int) -> None:
        self._sync_label()
        self.changed.emit()

    def value(self) -> float:
        return self.slider.value() / 100.0

    def set_value(self, value: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(max(0.0, min(1.0, float(value))) * 100)))
        self.slider.blockSignals(False)
        self._sync_label()


class ModelsPage(Page):
    page_title = "模型路由"
    page_subtitle = (
        "不同能力走不同的 API 线路：看图、生图、发语音都可以各用各家的端点 / 模型 / Key。"
        "听语音（语音转文字）直接用 QQ 官方平台自带的参考转写，零配置。"
        "主模型（文字对话）统一在「系统设置 → LLM」配置。留空的线路会自动降级，不影响纯文字聊天。"
    )
    scrollable = True

    def __init__(self, ctx, parent: Optional[QWidget] = None):
        self._loaded_config = False
        self._voice_retry_sent = False  # 音色清单首次拉取失败（Bot 未就绪）后的补救重试
        super().__init__(ctx, parent)

    def build(self, layout: QVBoxLayout) -> None:
        # ---------------------------------------------------- 保存按钮
        self.btn_save = primary_button("保存配置", self)
        self.btn_save.clicked.connect(self._save)
        self.add_action(self.btn_save)
        self.lbl_saved = QLabel("尚未保存", self)
        self.lbl_saved.setObjectName("MutedLabel")
        self.actions_layout.insertWidget(0, self.lbl_saved)

        # ---------------------------------------------------- 主模型说明（整合到系统设置）
        chat_hint = hint_label(
            "主模型（文字对话）统一在「系统设置 → LLM」里配置（Base URL / API Key / 模型 / 温度等），"
            "本页不再重复，避免两处不一致。",
            self,
        )
        layout.addWidget(chat_hint)

        # ---------------------------------------------------- 四个槽位（主模型不在本页）
        for slot in ALL_SLOTS:
            if slot == SLOT_CHAT:
                continue
            group = QGroupBox("%s（%s）" % (slot, SLOT_LABELS.get(slot, slot)), self)
            form = ProviderSlotForm(self.ctx, slot, group)
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(12, 10, 12, 10)
            group_layout.addWidget(hint_label(form.slot_hint(), group))
            group_layout.addWidget(form)
            layout.addWidget(group)
            setattr(self, "form_%s" % slot, form)

        # ---------------------------------------------------- 富媒体行为
        media_group = QGroupBox("富媒体行为", self)
        media_form = QFormLayout()
        media_form.setContentsMargins(0, 8, 0, 0)
        media_form.setSpacing(10)

        self.chk_media_enabled = QCheckBox("启用图片 / 语音能力（总开关）", media_group)
        self.chk_media_enabled.setChecked(True)
        self.chk_media_enabled.toggled.connect(lambda _c: self._mark_dirty())

        self.chk_allow_image = QCheckBox("允许角色给你发图（回复里出现 [IMG] 描述时自动生图）", media_group)
        self.chk_allow_image.setChecked(True)
        self.chk_allow_image.toggled.connect(lambda _c: self._mark_dirty())

        self.combo_image_style = QComboBox(media_group)
        self.combo_image_style.addItems(
            [
                "auto",
                "anime",
                "realistic",
                "off",
            ]
        )
        self.combo_image_style.setCurrentIndex(0)
        self.combo_image_style.currentIndexChanged.connect(lambda _i: self._mark_dirty())

        self.prob_voice = _ProbabilityRow(0.3, media_group)
        self.prob_voice.changed.connect(self._mark_dirty)

        self.spin_voice_max = QSpinBox(media_group)
        self.spin_voice_max.setRange(20, 500)
        self.spin_voice_max.setValue(180)
        self.spin_voice_max.setSuffix(" 字")
        self.spin_voice_max.valueChanged.connect(lambda _v: self._mark_dirty())

        self.spin_temp_days = QSpinBox(media_group)
        self.spin_temp_days.setRange(0, 30)
        self.spin_temp_days.setValue(3)
        self.spin_temp_days.setSuffix(" 天")
        self.spin_temp_days.valueChanged.connect(lambda _v: self._mark_dirty())

        media_form.addRow(self.chk_media_enabled)
        media_form.addRow(self.chk_allow_image)
        add_form_row(
            media_form,
            "生图风格",
            self.combo_image_style,
            "角色发图时自动按人设匹配：二次元角色出动漫风、真实风格角色出写实照片风"
            "（auto，默认）；也可强制指定（anime 动漫 / realistic 写实 / off 不指定）",
            label_width=110,
        )
        add_form_row(
            media_form,
            "语音回复概率",
            self.prob_voice,
            "每次回复随机决定发文字还是发语音（你发文字她可能回语音，你发语音她也可能回文字）；0% 纯文字，100% 纯语音。主动消息同样生效。",
            label_width=110,
        )
        add_form_row(
            media_form,
            "单条语音上限",
            self.spin_voice_max,
            "超过这个字数的回复会拆成多条语音发送",
            label_width=110,
        )
        add_form_row(
            media_form,
            "媒体文件保留",
            self.spin_temp_days,
            "收发的图片 / 语音临时文件保留天数，到期自动清理（0 = 立即清理）",
            label_width=110,
        )

        media_layout = QVBoxLayout(media_group)
        media_layout.setContentsMargins(12, 10, 12, 10)
        media_layout.addLayout(media_form)
        layout.addWidget(media_group)

        layout.addStretch(1)

    # ================================================================ 加载
    def refresh(self) -> None:
        try:
            manager = gui_config()
            data = manager.data or {}
            providers = data.get("providers") or {}
            media = data.get("media") or {}
        except Exception:
            return
        for slot in ALL_SLOTS:
            if slot == SLOT_CHAT:
                continue  # 主模型统一在「系统设置 → LLM」配置，本页不读写
            form: ProviderSlotForm = getattr(self, "form_%s" % slot)
            form.set_values(dict((providers or {}).get(slot) or {}))
        # 每次进入页面都重拉全量音色清单：首次拉取若早于 Bot 就绪会只剩静态 14 个，
        # 这里保证用户打开页面时（Bot 已在运行）下拉框里是全量 300+ 音色
        self.form_tts._refresh_voice_list()
        self.chk_media_enabled.setChecked(bool(media.get("enabled", True)))
        self.chk_allow_image.setChecked(bool(media.get("allow_image", True)))
        _style = str(media.get("image_style") or "auto").lower()
        if _style not in ("auto", "anime", "realistic", "off"):
            _style = "auto"
        _idx = self.combo_image_style.findText(_style)
        self.combo_image_style.setCurrentIndex(_idx if _idx >= 0 else 0)
        _prob = media.get("voice_reply_probability")
        self.prob_voice.set_value(float(_prob if _prob is not None else 0.1))
        self.spin_voice_max.setValue(int(media.get("voice_max_chars", 180) or 180))
        self.spin_temp_days.setValue(int(media.get("temp_days", 3) or 3))
        self._loaded_config = True

    # ================================================================ 状态
    def on_status(self, snapshot: Dict[str, Any]) -> None:
        """Bot 上线后补救一次音色清单拉取（打开页面时 Bot 可能还没就绪）。"""
        if not bool(snapshot.get("online")) or self._voice_retry_sent:
            return
        form = self.form_tts
        if form.combo_voice is not None and form.combo_voice.count() < 50:
            self._voice_retry_sent = True
            form._refresh_voice_list()

    # ================================================================ 保存
    def _collect(self) -> Dict[str, Any]:
        providers: Dict[str, Dict[str, str]] = {}
        for slot in ALL_SLOTS:
            if slot == SLOT_CHAT:
                continue  # 主模型在「系统设置」配置；不写 providers.chat，保留磁盘上可能的旧值
            form: ProviderSlotForm = getattr(self, "form_%s" % slot)
            providers[slot] = form.values()
        return {
            "providers": providers,
            "media": {
                "enabled": self.chk_media_enabled.isChecked(),
                "allow_image": self.chk_allow_image.isChecked(),
                "image_style": self.combo_image_style.currentText(),
                "voice_reply_probability": self.prob_voice.value(),
                "voice_max_chars": int(self.spin_voice_max.value()),
                "temp_days": int(self.spin_temp_days.value()),
            },
        }

    def _mark_dirty(self) -> None:
        self.lbl_saved.setText("有未保存的修改")
        self.lbl_saved.setProperty("state", "warn")
        style = self.lbl_saved.style()
        if style is not None:
            style.unpolish(self.lbl_saved)
            style.polish(self.lbl_saved)

    def _save(self) -> None:
        patch = self._collect()
        try:
            manager = gui_config()
            save_config(manager, patch)
        except Exception as exc:
            self.toast("保存失败：%s" % exc, "error")
            return
        self.lbl_saved.setText("已保存到 config.yaml")
        self.lbl_saved.setProperty("state", "ok")
        style = self.lbl_saved.style()
        if style is not None:
            style.unpolish(self.lbl_saved)
            style.polish(self.lbl_saved)
        self.ctx.reload_config()
        self.toast("模型路由已保存，Bot 将在下一次热重载（约 30 秒内）生效", "info")
