"""模型路由页面（V0.2）：不同能力走不同的 API 线路。

三个槽位（图像理解 / 图像生成 / 文字转语音）各自独立配置，保存后 Bot 热重载即生效。

听语音（语音转文字）不占槽位：QQ 官方平台随语音消息推送参考转写，零配置可用。

主模型（文字对话）统一在「系统设置 → LLM」段配置，本页不重复，避免两处不一致。
富媒体行为（语音回复概率 / 发图开关等）V0.2.2 起在「消息设置」页配置。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from common.providers import ALL_SLOTS, SLOT_CHAT, SLOT_LABELS

from ..config_store import gui_config, save_config
from ..widgets.fields import hint_label, primary_button
from ..widgets.provider_form import ProviderSlotForm
from .base import Page


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

        # ---------------------------------------------------- 富媒体行为已搬到「消息设置」页
        media_moved = hint_label(
            "富媒体行为（图片 / 语音总开关、语音回复概率等）在「消息设置」页配置，"
            "可以按机器人单独设置；生图风格在「机器人」页按机器人设置。",
            self,
        )
        layout.addWidget(media_moved)

        layout.addStretch(1)

    # ================================================================ 加载
    def refresh(self) -> None:
        try:
            manager = gui_config()
            data = manager.data or {}
            providers = data.get("providers") or {}
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
