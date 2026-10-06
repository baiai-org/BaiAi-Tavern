"""模型路由槽位表单（V0.2）。

「模型路由」页面里每个能力槽位（图像理解 / 图像生成 / 文字转语音）
共用这个表单：引擎 + **服务商预设** + Base URL + API Key + **模型（可获取列表/
点选）** + 音色 + 「测试线路」。

服务商预设与「获取模型列表」的行为和 :class:`LLMConfigForm`（主模型）一致：
选中预设自动填 Base URL 与候选模型，点「获取模型列表」从上游 ``/models`` 拉取
真实清单，点选即填入模型输入框。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from common.providers import (
    ENGINE_DASHSCOPE,
    ENGINE_EDGE_TTS,
    ENGINE_GEMINI,
    ENGINE_OPENAI,
    SLOT_ENGINES,
    SLOT_LABELS,
    SLOT_TTS,
)

from .. import provider_presets
from ..llm_check import fetch_models
from ..llm_presets import CUSTOM_LABEL
from .fields import add_form_row, ghost_button, hint_label

# 各槽位的填写说明（界面显示用）
SLOT_HINTS: Dict[str, str] = {
    "chat": "主模型统一在「系统设置 → LLM」里配置。",
    "vision": "支持多模态的 OpenAI 兼容端点（通义千问 VL / DeepSeek-VL2 / GPT-4o / Gemini 等）。配置后角色能看懂你发来的图片。",
    "image": "两种引擎：OpenAI 兼容的 /images/generations 端点（通义万相 / GPT-Image / Gemini 兼容层等），或 Gemini 原生接口（支持全部 Gemini 图像模型，含 nano banana 2 系列）。角色想给你发图时调用；Gemini 接口对参数挑剔，程序会自动降级参数 / 自动切换出图方式。",
    "tts": "推荐阿里云百炼（默认已填好：qwen-audio-3.1-tts-flash + yuxiaoyun_v3.1，只需粘贴 API Key；自动启用情感与拟声标签 + 语气指令 + 语速/音调/音量调节）；edge-tts 在线免费无需 Key；OpenAI 兼容走 /audio/speech（如 MiniMax / ElevenLabs）。",
}


def _engine_label(engine: str, slot: str) -> str:
    if engine == ENGINE_EDGE_TTS:
        return "edge-tts（在线免费，无需 Key）"
    if engine == ENGINE_DASHSCOPE:
        if slot == SLOT_TTS:
            # 下拉项文字要短：QComboBox 的最小宽度按当前项文字计算，
            # 过长会把整个表单（进而可滚动页面）撑宽导致右侧被裁
            return "阿里云百炼 DashScope（推荐）"
        return "阿里云百炼 DashScope（Qwen3-TTS / CosyVoice 原生接口）"
    if engine == ENGINE_GEMINI:
        return "Gemini 原生接口（支持全部 Gemini 图像模型）"
    return "OpenAI 兼容端点"


def _labeled_spin(label: str, spin: Any) -> QWidget:
    """「标签 + 数值框」的紧凑组合（音色调节行用）。"""
    from PySide6.QtWidgets import QFormLayout as _QFormLayout

    cell = QWidget()
    form = _QFormLayout(cell)
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(2)
    form.addRow(label, spin)
    spin.setParent(cell)
    return cell


#: 常见 edge-tts 中文音色（静态兜底清单，与 Microsoft 当前官方清单一致；
#: 界面启动时会再从 /api/media/voices 拉全量清单（含全部语种），这里只是离线保底）
EDGE_TTS_ZH_VOICES = (
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuGaaiNeural",
    "zh-HK-HiuMaanNeural",
    "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural",
    "zh-TW-YunJheNeural",
    "zh-TW-HsiaoYuNeural",
)


class ProviderSlotForm(QWidget):
    """单个模型槽位的表单：引擎 / 服务商 / Base URL / Key / 模型（可获取列表）/ 音色 / 测试。"""

    changed = Signal()

    def __init__(self, ctx: Any, slot: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.slot = slot
        self.last_result: Optional[Dict[str, Any]] = None
        self.all_models: List[str] = []
        self._key_tag = str(id(self))

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        engines = SLOT_ENGINES.get(slot, [ENGINE_OPENAI])

        # ---------------------------------------------------------- 引擎
        if len(engines) > 1:
            self.combo_engine = QComboBox(self)
            self.combo_engine.addItems([_engine_label(e, slot) for e in engines])
            add_form_row(layout, "引擎", self.combo_engine)
        else:
            self.combo_engine = None
            self.lbl_engine = hint_label(_engine_label(ENGINE_OPENAI, slot), self)
            layout.addRow(QLabel(""), self.lbl_engine)

        # ---------------------------------------------------------- 服务商
        presets = provider_presets.SLOT_PRESETS.get(slot, ())
        if presets:
            preset_row = QWidget(self)
            preset_layout = QHBoxLayout(preset_row)
            preset_layout.setContentsMargins(0, 0, 0, 0)
            preset_layout.setSpacing(8)
            self.combo_preset = QComboBox(preset_row)
            self.combo_preset.addItems(provider_presets.labels(slot))
            self.combo_preset.setMinimumWidth(230)
            self.link_docs = QLabel("", preset_row)
            self.link_docs.setObjectName("HintLabel")
            self.link_docs.setOpenExternalLinks(True)
            self.link_docs.setTextFormat(Qt.RichText)
            preset_layout.addWidget(self.combo_preset)
            preset_layout.addWidget(self.link_docs, 1)
            add_form_row(
                layout,
                "服务商",
                preset_row,
                "选择常用服务商会自动填好下面的 Base URL；选「%s」可自己填任意兼容地址" % CUSTOM_LABEL,
            )
            self._provider_row_index = layout.rowCount() - 1
            self.lbl_provider_note = hint_label("")
            layout.addRow("", self.lbl_provider_note)
            self._provider_note_row_index = layout.rowCount() - 1
        else:
            self.combo_preset = None
            self.link_docs = None
            self.lbl_provider_note = None
            self._provider_row_index = -1
            self._provider_note_row_index = -1

        # ------------------------------------------------------ Base URL
        self.edit_base = QLineEdit(self)
        self.edit_base.setPlaceholderText(self._placeholder_base())
        add_form_row(layout, "Base URL", self.edit_base, "OpenAI 兼容地址，通常以 /v1 结尾；本地服务填 http://127.0.0.1:端口/v1")

        # ------------------------------------------------------- API Key
        key_row = QWidget(self)
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.edit_key = QLineEdit(key_row)
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("sk-…（本地 / edge-tts 可留空）")
        self.btn_show_key = ghost_button("显示", key_row)
        self.btn_show_key.setProperty("chip", True)
        self.btn_show_key.setCheckable(True)
        self.btn_show_key.toggled.connect(
            lambda checked: self.edit_key.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        key_layout.addWidget(self.edit_key, 1)
        key_layout.addWidget(self.btn_show_key)
        add_form_row(layout, "API Key", key_row)

        # ---------------------------------------------------------- 模型
        model_row = QWidget(self)
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(8)
        self.edit_model = QComboBox(model_row)
        self.edit_model.setEditable(True)
        self.edit_model.setInsertPolicy(QComboBox.NoInsert)
        self.edit_model.setMinimumWidth(240)
        self.edit_model.lineEdit().setPlaceholderText(self._placeholder_model())
        self.btn_fetch = ghost_button("获取模型列表", model_row)
        self.btn_fetch.clicked.connect(lambda _checked: self.fetch_models())
        model_layout.addWidget(self.edit_model, 1)
        model_layout.addWidget(self.btn_fetch)
        add_form_row(
            layout,
            "模型",
            model_row,
            "最终生效的就是这里的模型名：选服务商预设 / 点下方列表 / 直接输入都可以",
        )

        # ------------------------------------------------------- 可选模型
        list_row = QWidget(self)
        list_layout = QVBoxLayout(list_row)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(6)
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(8)
        self.lbl_models_count = QLabel("尚未获取", list_row)
        self.lbl_models_count.setObjectName("HintLabel")
        self.edit_filter = QLineEdit(list_row)
        self.edit_filter.setPlaceholderText("筛选模型名…")
        self.edit_filter.setFixedWidth(160)
        tools.addWidget(self.lbl_models_count)
        tools.addStretch(1)
        tools.addWidget(self.edit_filter)
        list_layout.addLayout(tools)
        self.list_models = QListWidget(list_row)
        self.list_models.setObjectName("OnbList")
        self.list_models.setMinimumHeight(110)
        self.list_models.setMaximumHeight(160)
        list_layout.addWidget(self.list_models)
        add_form_row(
            layout,
            "可选模型",
            list_row,
            "点「获取模型列表」从上游拉取真实模型清单，点选即填入上方「模型」",
        )
        self._model_list_row_index = layout.rowCount() - 1

        # ---------------------------------------------------------- 音色（tts）
        self.combo_voice: Optional[QComboBox] = None
        if slot == SLOT_TTS:
            self.combo_voice = QComboBox(self)
            self.combo_voice.setEditable(True)
            self.combo_voice.setInsertPolicy(QComboBox.NoInsert)
            self.combo_voice.addItems(list(EDGE_TTS_ZH_VOICES))
            add_form_row(
                layout,
                "音色",
                self.combo_voice,
                "edge-tts 音色名（可下拉选或手动输入，启动后会自动加载全部语种音色）；"
                "OpenAI 引擎时填服务商要求的 voice 名。角色可单独覆盖。",
            )
            # ---------------------------------------------------- 音色调节（语速/音调/音量）
            # 外层 VBox + 内层 HBox：数值框一行、百炼长提示单独一行（换行），
            # 长文字只增加高度不增加最小宽度——否则会把整个可滚动页面撑到
            # 2500px+ 宽（视口只有 ~900px），右侧控件被裁、页面"突然偏移"
            self.tune_row = QWidget(self)
            tune_outer = QVBoxLayout(self.tune_row)
            tune_outer.setContentsMargins(0, 0, 0, 0)
            tune_outer.setSpacing(4)
            tune_layout = QHBoxLayout()
            tune_layout.setContentsMargins(0, 0, 0, 0)
            tune_layout.setSpacing(8)
            # edge-tts 三参数（微软 Edge TTS 原生支持 rate / pitch / volume）
            self.spin_rate = QSpinBox(self.tune_row)
            self.spin_rate.setRange(-50, 100)
            self.spin_rate.setSuffix(" %")
            self.spin_rate.setValue(0)
            self.spin_pitch = QSpinBox(self.tune_row)
            self.spin_pitch.setRange(-50, 50)
            self.spin_pitch.setSuffix(" Hz")
            self.spin_pitch.setValue(0)
            self.spin_volume = QSpinBox(self.tune_row)
            self.spin_volume.setRange(-50, 100)
            self.spin_volume.setSuffix(" %")
            self.spin_volume.setValue(0)
            # 数值框最小宽度收窄：整行放不下时宁可更窄，也不能把页面撑宽裁掉右侧控件
            for _spin in (self.spin_rate, self.spin_pitch, self.spin_volume, None):
                if _spin is not None:
                    _spin.setMinimumWidth(72)
            self.lbl_tune_edge = QLabel("edge-tts：", self.tune_row)
            self.lbl_tune_edge.setObjectName("HintLabel")
            tune_layout.addWidget(self.lbl_tune_edge)
            for label, spin in (("语速", self.spin_rate), ("音调", self.spin_pitch), ("音量", self.spin_volume)):
                tune_layout.addWidget(_labeled_spin(label, spin))
            # OpenAI 兼容引擎：语速倍率（OpenAI /audio/speech 官方支持 speed 0.25~4.0）
            self.dspin_speed = QDoubleSpinBox(self.tune_row)
            self.dspin_speed.setRange(0.5, 2.0)
            self.dspin_speed.setSingleStep(0.05)
            self.dspin_speed.setDecimals(2)
            self.dspin_speed.setValue(1.0)
            self.dspin_speed.setMinimumWidth(72)
            self.lbl_tune_openai = QLabel("OpenAI 兼容：", self.tune_row)
            self.lbl_tune_openai.setObjectName("HintLabel")
            tune_layout.addWidget(self.lbl_tune_openai)
            self._tune_openai_widget = _labeled_spin("倍率", self.dspin_speed)
            tune_layout.addWidget(self._tune_openai_widget)
            tune_layout.addStretch(1)
            tune_outer.addLayout(tune_layout)
            # 百炼引擎：qwen-audio 系列可调语速/音调/音量（官方 rate/pitch/volume 参数）。
            # 单独一行 + 换行：百炼默认是推荐引擎，这行提示常显示，宽度必须可收缩
            self.lbl_tune_dashscope = QLabel(
                "百炼：qwen-audio 系列可调上方参数（官方 rate 0.5~2.0 倍 / pitch / volume 0~100）；"
                "qwen-audio 与 qwen3-tts-instruct 模型还会由主模型按语境自动叠加情感标签/指令；无参数调节的模型忽略上方数值",
                self.tune_row,
            )
            self.lbl_tune_dashscope.setObjectName("HintLabel")
            self.lbl_tune_dashscope.setWordWrap(True)
            self.lbl_tune_dashscope.setVisible(False)
            tune_outer.addWidget(self.lbl_tune_dashscope)
            self._tune_edge_widgets = (self.lbl_tune_edge, self.spin_rate, self.spin_pitch, self.spin_volume)
            add_form_row(
                layout,
                "音色调节",
                self.tune_row,
                "edge-tts 可调语速（-50%~+100%）/ 音调（±50Hz）/ 音量；OpenAI 兼容引擎可调语速倍率。"
                "0 为默认，试听时会按当前设置合成。",
            )
            self._tune_row_index = layout.rowCount() - 1
            self._refresh_voice_list()
        else:
            self.tune_row = None
            self.spin_rate = None
            self.spin_pitch = None
            self.spin_volume = None
            self.dspin_speed = None
            self.lbl_tune_dashscope = None
            self._tune_row_index = -1

        # -------------------------------------------------------- 测试线路
        test_row = QWidget(self)
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.setSpacing(8)
        self.btn_test = ghost_button("测试线路", test_row)
        self.btn_test.clicked.connect(self.test)
        self.lbl_status = QLabel("尚未测试", test_row)
        self.lbl_status.setObjectName("MutedLabel")
        self.lbl_status.setWordWrap(True)
        test_layout.addWidget(self.btn_test)
        test_layout.addWidget(self.lbl_status, 1)
        add_form_row(
            layout,
            "连通性",
            test_row,
            "按当前填写的内容当场调用一次，验证端点 / Key / 模型是否可用（生图会真实产出一张图）",
        )

        # 信号最后连，避免初始化期间 addItems 触发回调
        if self.combo_engine is not None:
            self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        if self.combo_preset is not None:
            self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.edit_base.textChanged.connect(self._on_base_changed)
        self.edit_key.textChanged.connect(lambda _t: self.changed.emit())
        self.edit_model.currentTextChanged.connect(
            lambda _t: (self._sync_engine_display(), self.changed.emit())
        )
        self.edit_filter.textChanged.connect(lambda _t: self._refresh_model_list())
        self.list_models.itemClicked.connect(self._on_model_item_clicked)
        self.list_models.itemDoubleClicked.connect(self._on_model_item_clicked)
        if self.combo_voice is not None:
            self.combo_voice.currentTextChanged.connect(lambda _t: self.changed.emit())
            self.spin_rate.valueChanged.connect(lambda _v: self.changed.emit())
            self.spin_pitch.valueChanged.connect(lambda _v: self.changed.emit())
            self.spin_volume.valueChanged.connect(lambda _v: self.changed.emit())
            self.dspin_speed.valueChanged.connect(lambda _v: self.changed.emit())

        self._sync_engine_display()
        self._sync_provider_display()
        self._refresh_model_list()

    # ============================================================== 取值/赋值
    def values(self) -> Dict[str, str]:
        result = {
            "base_url": self.edit_base.text().strip(),
            "api_key": self.edit_key.text().strip(),
            "model": self.edit_model.currentText().strip(),
        }
        if self.combo_engine is not None:
            result["engine"] = self.current_engine()
        if self.combo_voice is not None:
            result["voice"] = self.combo_voice.currentText().strip()
            if self.current_engine() == ENGINE_EDGE_TTS:
                # edge-tts 风格参数：0 不写（保持服务商默认）
                if self.spin_rate.value():
                    result["rate"] = "%+d%%" % self.spin_rate.value()
                if self.spin_pitch.value():
                    result["pitch"] = "%+dHz" % self.spin_pitch.value()
                if self.spin_volume.value():
                    result["volume"] = "%+d%%" % self.spin_volume.value()
            else:
                if abs(self.dspin_speed.value() - 1.0) > 0.001:
                    result["speed"] = str(round(self.dspin_speed.value(), 2))
        return result

    def current_engine(self) -> str:
        if self.combo_engine is None:
            return ENGINE_OPENAI
        index = max(0, self.combo_engine.currentIndex())
        engines = SLOT_ENGINES.get(self.slot, [ENGINE_OPENAI])
        return engines[index] if index < len(engines) else ENGINE_OPENAI

    def slot_hint(self) -> str:
        return SLOT_HINTS.get(self.slot, "")

    def set_values(self, values: Dict[str, Any]) -> None:
        # 百炼是 TTS 推荐默认：还没填过任何东西时自动填好推荐组合
        # （百炼地址 + qwen-audio-3.1-tts-flash + yuxiaoyun_v3.1），用户只需粘贴 API Key
        if self.slot == SLOT_TTS and not any(
            str(values.get(k) or "").strip() for k in ("base_url", "model", "voice")
        ) and str(values.get("engine") or "").strip() in ("", ENGINE_DASHSCOPE):
            _presets = provider_presets.SLOT_PRESETS.get(SLOT_TTS)
            default_preset = _presets[0] if _presets else None
            if default_preset is not None and default_preset.models:
                values = dict(values)
                values["base_url"] = default_preset.base_url
                values["model"] = default_preset.models[0]
                values["voice"] = "yuxiaoyun_v3.1"
        engine = str(values.get("engine") or "")
        if self.combo_engine is not None:
            engines = SLOT_ENGINES.get(self.slot, [ENGINE_OPENAI])
            index = engines.index(engine) if engine in engines else 0
            self.combo_engine.blockSignals(True)
            self.combo_engine.setCurrentIndex(index)
            self.combo_engine.blockSignals(False)
        base_url = str(values.get("base_url") or "")
        # 服务商回显：按已保存的 Base URL 反查预设
        if self.combo_preset is not None:
            preset = provider_presets.by_base_url(self.slot, base_url)
            self.combo_preset.blockSignals(True)
            self.combo_preset.setCurrentText(preset.name if preset else CUSTOM_LABEL)
            self.combo_preset.blockSignals(False)
            self._sync_provider_display()
        self.edit_base.blockSignals(True)
        self.edit_base.setText(base_url)
        self.edit_base.blockSignals(False)
        self.edit_key.setText(str(values.get("api_key") or ""))
        # 模型候选：预设候选 + 已保存值
        model = str(values.get("model") or "")
        preset = provider_presets.by_base_url(self.slot, base_url) if self.combo_preset is not None else None
        candidates = list(preset.models) if preset else []
        self.edit_model.blockSignals(True)
        self.edit_model.clear()
        if candidates:
            self.edit_model.addItems(candidates)
        if model:
            if model not in candidates:
                self.edit_model.addItem(model)
            self.edit_model.setCurrentText(model)
        elif candidates:
            self.edit_model.setCurrentText(candidates[0])
        if self.combo_voice is not None:
            voice = str(values.get("voice") or "")
            self.combo_voice.blockSignals(True)
            existing = [self.combo_voice.itemText(i) for i in range(self.combo_voice.count())]
            if voice and voice not in existing:
                self.combo_voice.addItem(voice)
            self.combo_voice.setCurrentText(voice)
            self.combo_voice.blockSignals(False)
            # 音色调节参数回显
            import re

            def _num(key: str) -> Optional[int]:
                match = re.search(r"[-+]?\d+", str(values.get(key) or ""))
                return int(match.group()) if match else None

            rate = _num("rate")
            if rate is not None:
                self.spin_rate.setValue(max(-50, min(100, rate)))
            pitch = _num("pitch")
            if pitch is not None:
                self.spin_pitch.setValue(max(-50, min(50, pitch)))
            volume = _num("volume")
            if volume is not None:
                self.spin_volume.setValue(max(-50, min(100, volume)))
            speed_text = str(values.get("speed") or "").strip()
            if speed_text:
                try:
                    self.dspin_speed.setValue(max(0.5, min(2.0, float(speed_text))))
                except ValueError:
                    pass
        self._sync_engine_display()

    def refresh_status(self, status: Dict[str, Any]) -> None:
        """状态快照里的槽位配置（来自 Bot 进程，用于页面初次显示前同步）。"""
        slot_status = (status.get("slots") or {}).get(self.slot) or {}
        if slot_status:
            self.set_values(
                {
                    "engine": slot_status.get("engine"),
                    "base_url": slot_status.get("base_url"),
                    "api_key": "",
                    "model": slot_status.get("model"),
                    "voice": slot_status.get("voice"),
                }
            )

    # ============================================================== 模型列表
    def visible_models(self) -> List[str]:
        keyword = self.edit_filter.text().strip().lower()
        if not keyword:
            return list(self.all_models)
        return [name for name in self.all_models if keyword in name.lower()]

    def _refresh_model_list(self) -> None:
        models = self.visible_models()
        self.list_models.blockSignals(True)
        self.list_models.clear()
        if not self.all_models:
            placeholder = QListWidgetItem("（点上方「获取模型列表」拉取上游模型）")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list_models.addItem(placeholder)
            self.list_models.setEnabled(False)
            self.lbl_models_count.setText("尚未获取")
        else:
            self.list_models.setEnabled(True)
            for name in models:
                self.list_models.addItem(name)
            self.lbl_models_count.setText("可选 %d 个（共 %d 个）" % (len(models), len(self.all_models)))
        self.list_models.blockSignals(False)

    def _on_model_item_clicked(self, item: QListWidgetItem) -> None:
        self.edit_model.setCurrentText(item.text())

    def fetch_models(self, silent: bool = False) -> None:
        """从上游 ``/models`` 拉取模型清单（不依赖 Bot 进程，直接请求）。"""
        if self.ctx is None:
            return
        base_url = self.edit_base.text().strip()
        if not base_url:
            self.lbl_models_count.setText("先填写 Base URL")
            return
        if not silent:
            self.btn_fetch.setEnabled(False)
            self.lbl_models_count.setText("获取中…")

        def _ok(data: Any) -> None:
            self.btn_fetch.setEnabled(True)
            models = [str(m) for m in ((data or {}).get("models") or [])]
            self.all_models = models
            self._refresh_model_list()
            if models:
                self.lbl_models_count.setText("已获取 %d 个模型" % len(models))
            else:
                self.lbl_models_count.setText("上游没有返回模型")
            if self.slot == SLOT_TTS:
                # 上游模型变了（比如换成 instruct 系列），音色清单跟着刷新
                self._refresh_voice_list()

        def _error(message: str) -> None:
            self.btn_fetch.setEnabled(True)
            self.lbl_models_count.setText("✗ %s" % message)

        self.ctx.run_task(
            lambda: fetch_models(base_url, self.edit_key.text().strip(), include_non_chat=True),
            on_ok=_ok,
            on_error=_error,
            key="provider_models_%s" % self._key_tag,
            label="获取模型列表（%s）" % SLOT_LABELS.get(self.slot, self.slot),
        )

    # ============================================================== 音色清单
    def _refresh_voice_list(self) -> None:
        """从 Bot 拉音色清单（edge-tts 全量 300+ / 百炼候选），合并进下拉框。

        tts 槽位按**表单当前引擎**取清单（未保存也能换引擎试听）。
        """
        if self.ctx is None or self.combo_voice is None:
            return
        engine = self.current_engine() if self.slot == SLOT_TTS else ""

        def _ok(data: Any) -> None:
            names = [str(item.get("short_name") or "").strip() for item in (data or []) if isinstance(item, dict)]
            names = [name for name in names if name]
            if not names:
                return
            self.combo_voice.blockSignals(True)
            current = self.combo_voice.currentText().strip()
            self.combo_voice.clear()
            self.combo_voice.addItems(names)
            if current:
                if current in names:
                    self.combo_voice.setCurrentText(current)
                else:
                    self.combo_voice.addItem(current)
                    self.combo_voice.setCurrentText(current)
            self.combo_voice.blockSignals(False)

        def _error(_message: str) -> None:
            # Bot 未启动 / openai 引擎：保留当前静态清单
            pass

        self.ctx.run_task(
            lambda: self.ctx.api.media_voices(engine),
            on_ok=_ok,
            on_error=_error,
            key="media_voices_%s" % self._key_tag,
            label="加载音色清单",
        )

    # ============================================================== 测试
    def test(self) -> None:
        if self.ctx is None:
            return
        # 同槽位测试进行中则忽略重复点击（runner 按 key 去重，返回 False）。
        # 注意：这里**不** disable 按钮——按钮获得过点击焦点，disable 会让焦点
        # 自动跳到下一个可聚焦控件（下方表单的引擎下拉框），滚动区跟着过去，
        # 表现为"窗口突然跳到生图段"（用户实测反馈）。
        self._set_status("正在测试…", "")

        def _ok(data: Any) -> None:
            self.last_result = data if isinstance(data, dict) else {"ok": False, "message": str(data)}
            ok = bool(self.last_result.get("ok"))
            message = str(self.last_result.get("message") or ("测试通过" if ok else "测试失败"))
            self._set_status(("✓ " if ok else "✗ ") + message, "ok" if ok else "bad")
            if ok and self.slot == SLOT_TTS and self.last_result.get("preview_b64"):
                self._play_preview(str(self.last_result["preview_b64"]))

        def _error(message: str) -> None:
            self.last_result = {"ok": False, "message": message}
            self._set_status("✗ %s" % message, "bad")

        # 带上表单当前值：不点「保存」也能按界面所见测试
        self.ctx.run_task(
            lambda: self.ctx.api.providers_test(self.slot, values=self.values()),
            on_ok=_ok,
            on_error=_error,
            key="provider_test_%s" % self.slot,
            label="测试线路（%s）" % SLOT_LABELS.get(self.slot, self.slot),
        )

    def _play_preview(self, b64: str) -> None:
        """把 TTS 试听片段落盘后用系统默认播放器打开（Windows）。"""
        import base64
        import os

        try:
            from common.paths import data_dir

            path = data_dir() / "media" / ("tts_preview_%d.mp3" % int(__import__("time").time() * 1000))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(b64))
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            pass

    # ============================================================== 内部
    def _set_status(self, text: str, state: str) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setProperty("state", state)
        style = self.lbl_status.style()
        if style is not None:
            style.unpolish(self.lbl_status)
            style.polish(self.lbl_status)

    def _on_engine_changed(self, _index: int) -> None:
        self._sync_engine_display()
        if self.slot == SLOT_TTS and self.combo_voice is not None:
            # 引擎切换后音色清单跟着换（edge-tts 全量 / 百炼候选）
            self._refresh_voice_list()
        self.changed.emit()

    def _on_preset_changed(self, _index: int) -> None:
        """选择预设服务商：填 Base URL、切换候选模型、更新申请入口。

        切换服务商时**不保留**原来的模型名——它对新服务商几乎一定无效。
        """
        if self.combo_preset is None:
            return
        preset = provider_presets.by_label(self.slot, self.combo_preset.currentText())
        if preset is None:
            self._sync_provider_display()
            self.changed.emit()
            return
        # 预设指定引擎时同步切换（如 Gemini 原生接口生图预设）
        if preset.engine and self.combo_engine is not None:
            engines = SLOT_ENGINES.get(self.slot, [])
            if preset.engine in engines:
                self.combo_engine.blockSignals(True)
                self.combo_engine.setCurrentIndex(engines.index(preset.engine))
                self.combo_engine.blockSignals(False)
                self._sync_engine_display()
        self.edit_base.blockSignals(True)
        self.edit_base.setText(preset.base_url)
        self.edit_base.blockSignals(False)
        self.edit_model.blockSignals(True)
        self.edit_model.clear()
        if preset.models:
            self.edit_model.addItems(preset.models)
            self.edit_model.setCurrentText(preset.models[0])
        else:
            self.edit_model.setCurrentText("")
        self.edit_model.blockSignals(False)
        self._sync_provider_display()
        if preset.models:
            hint = "已切到「%s」，模型选为 %s；点「获取模型列表」可拉取全部模型" % (
                preset.name,
                preset.models[0],
            )
        else:
            hint = "已切到「%s」，请点「获取模型列表」选择模型%s" % (
                preset.name,
                "（本地部署，API Key 可留空）" if preset.local else "",
            )
        self._set_status(hint, "muted")
        self.changed.emit()

    def _sync_provider_display(self) -> None:
        if self.combo_preset is None:
            return
        preset = provider_presets.by_label(self.slot, self.combo_preset.currentText())
        if preset is None:
            self.combo_preset.setToolTip("自定义 / 其他：直接填写任意 OpenAI 兼容地址")
            self.link_docs.setText("")  # type: ignore[union-attr]
            self.lbl_provider_note.setText("")  # type: ignore[union-attr]
            return
        self.combo_preset.setToolTip(preset.note or preset.name)
        if preset.docs:
            self.link_docs.setText(  # type: ignore[union-attr]
                '<a href="%s" style="color:#4c7df0; text-decoration:none;">申请 API Key ↗</a>' % preset.docs
            )
        else:
            self.link_docs.setText("")  # type: ignore[union-attr]
        parts = [part for part in (preset.note, "本地部署，API Key 可留空" if preset.local else "") if part]
        self.lbl_provider_note.setText("　".join(parts))  # type: ignore[union-attr]

    def _on_base_changed(self, text: str) -> None:
        if self.combo_preset is None:
            return
        preset = provider_presets.by_base_url(self.slot, text)
        if preset is not None:
            index = self.combo_preset.findText(preset.name)
            if index >= 0:
                self.combo_preset.blockSignals(True)
                self.combo_preset.setCurrentIndex(index)
                self.combo_preset.blockSignals(False)
                self._sync_provider_display()
        self.changed.emit()

    def _sync_engine_display(self) -> None:
        engine = self.current_engine()
        is_edge = self.combo_engine is not None and engine == ENGINE_EDGE_TTS
        is_dashscope = self.combo_engine is not None and engine == ENGINE_DASHSCOPE
        # 不走 OpenAI 兼容端点的引擎（edge-tts）：端点相关字段置灰
        hide = is_edge and self.slot == SLOT_TTS
        if self.edit_base is not None:
            self.edit_base.setEnabled(not hide)
        if self.edit_key is not None:
            self.edit_key.setEnabled(not hide)
        if self.edit_model is not None:
            self.edit_model.setEnabled(not hide)
        self.btn_fetch.setEnabled(not hide)
        # 服务商 / 可选模型两行整体隐藏
        if self.combo_preset is not None:
            self.layout().setRowVisible(self._provider_row_index, not hide)  # type: ignore[union-attr]
            self.layout().setRowVisible(self._provider_note_row_index, not hide)  # type: ignore[union-attr]
        self.layout().setRowVisible(self._model_list_row_index, not hide)
        # Base URL 提示：百炼引擎说明地址写法（兼容 /compatible-mode/v1）
        if self.edit_base is not None:
            self.edit_base.setPlaceholderText(
                "https://dashscope.aliyuncs.com 或工作空间 https://ws-xxx.cn-beijing.maas.aliyuncs.com"
                if is_dashscope
                else self._placeholder_base()
            )
        # 音色调节行：按引擎显示对应参数（edge-tts 语速/音调/音量；OpenAI 语速倍率；
        # 百炼 qwen-audio 系列同样可调——官方 rate/pitch/volume 参数，GUI 值自动映射）
        if self.slot == SLOT_TTS and self.tune_row is not None:
            model_text = (self.edit_model.currentText().strip() if self.edit_model is not None else "").lower()
            is_qwen_audio = is_dashscope and model_text.startswith("qwen-audio")
            for widget in self._tune_edge_widgets:
                widget.setVisible(is_edge or is_qwen_audio)
            self.lbl_tune_openai.setVisible(engine == ENGINE_OPENAI)
            self._tune_openai_widget.setVisible(engine == ENGINE_OPENAI or is_qwen_audio)
            self.lbl_tune_dashscope.setVisible(is_dashscope)

    def _placeholder_base(self) -> str:
        return {
            "chat": "https://api.deepseek.com/v1",
            "vision": "https://api.deepseek.com/v1",
            "image": "https://generativelanguage.googleapis.com/v1beta/openai",
            "tts": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }.get(self.slot, "https://api.deepseek.com/v1")

    def _placeholder_model(self) -> str:
        return {
            "chat": "deepseek-chat",
            "vision": "qwen-vl-max",
            "image": "gemini-2.5-flash-image",
            "tts": "qwen-audio-3.1-tts-flash",
        }.get(self.slot, "")
