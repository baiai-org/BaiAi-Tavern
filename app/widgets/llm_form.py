"""可复用的 LLM 配置控件。

包含：
* **服务商一键选择**：预设常用 OpenAI 兼容端点，选中即填好 Base URL；
* **模型列表获取**：从上游 ``/models`` 拉取模型，横向铺在**可选模型列表**里点选，
  也可以在上方可编辑下拉框直接输入模型名；
* **连通性测试**：当场验证 Key 与模型是否可用，成功后自动拉一次模型列表。

「配置引导」和「系统设置」共用这一个控件，保证两处行为一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger
from common.utils import truncate

from ..llm_check import fetch_models, filter_chat_models, test_llm
from ..llm_presets import CUSTOM_LABEL, by_base_url, by_label, labels
from .fields import add_form_row, ghost_button, hint_label

log = get_logger("app.llm_form")


class LLMConfigForm(QWidget):
    """服务商 + Base URL + API Key + 模型（可获取/可选） + 测试连接。"""

    changed = Signal()
    model_selected = Signal(str)

    def __init__(self, ctx, parent: Optional[QWidget] = None, show_test: bool = True):
        super().__init__(parent)
        self.ctx = ctx
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_models: List[str] = []   # 已过滤（可用的对话模型）
        self.all_models: List[str] = []    # 上游返回的全部模型
        self._key_tag = str(id(self))

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ---------------------------------------------------------- 服务商
        provider_row = QWidget(self)
        provider_layout = QHBoxLayout(provider_row)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        self.combo_preset = QComboBox(provider_row)
        self.combo_preset.addItems(labels())
        self.combo_preset.setMinimumWidth(230)
        self.link_docs = QLabel("", provider_row)
        self.link_docs.setObjectName("HintLabel")
        self.link_docs.setOpenExternalLinks(True)
        self.link_docs.setTextFormat(Qt.RichText)
        provider_layout.addWidget(self.combo_preset)
        provider_layout.addWidget(self.link_docs, 1)
        add_form_row(
            layout,
            "服务商",
            provider_row,
            "选择常用服务商会自动填好下面的 Base URL；选「%s」可自己填任意兼容地址" % CUSTOM_LABEL,
        )
        self.lbl_provider_note = hint_label("")
        layout.addRow("", self.lbl_provider_note)

        # -------------------------------------------------------- Base URL
        self.edit_base = QLineEdit(self)
        self.edit_base.setPlaceholderText("https://api.deepseek.com/v1")
        self.edit_base.textChanged.connect(self._on_base_changed)
        add_form_row(layout, "Base URL", self.edit_base, "OpenAI 兼容地址，通常以 /v1 结尾")

        # --------------------------------------------------------- API Key
        key_row = QWidget(self)
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.edit_key = QLineEdit(key_row)
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("sk-…")
        self.btn_show_key = ghost_button("显示", key_row)
        self.btn_show_key.setProperty("chip", True)
        self.btn_show_key.setCheckable(True)
        self.btn_show_key.toggled.connect(
            lambda checked: self.edit_key.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        key_layout.addWidget(self.edit_key, 1)
        key_layout.addWidget(self.btn_show_key)
        add_form_row(layout, "API Key", key_row, "只保存在本机 config.yaml；本地部署的服务可以留空")

        # ----------------------------------------------------------- 模型
        model_row = QWidget(self)
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(8)
        self.combo_model = QComboBox(model_row)
        self.combo_model.setEditable(True)
        self.combo_model.setInsertPolicy(QComboBox.NoInsert)
        self.combo_model.setMinimumWidth(240)
        self.combo_model.setCurrentText("")
        self.combo_model.lineEdit().setPlaceholderText("在下面列表里点选，或直接输入模型名")
        self.combo_model.editTextChanged.connect(self._on_model_text_changed)
        self.btn_fetch = ghost_button("获取模型列表", model_row)
        self.btn_fetch.clicked.connect(lambda: self.fetch_models())
        model_layout.addWidget(self.combo_model, 1)
        model_layout.addWidget(self.btn_fetch)
        add_form_row(layout, "当前模型", model_row, "最终生效的就是这里的模型名")

        # ------------------------------------------------------- 模型列表
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
        self.edit_filter.setFixedWidth(130)
        self.edit_filter.textChanged.connect(lambda _t: self._refresh_model_list())
        self.chk_show_all = QCheckBox("含非对话模型", list_row)
        self.chk_show_all.toggled.connect(lambda _c: self._refresh_model_list())
        tools.addWidget(self.lbl_models_count)
        tools.addStretch(1)
        tools.addWidget(self.edit_filter)
        tools.addWidget(self.chk_show_all)
        list_layout.addLayout(tools)

        self.list_models = QListWidget(list_row)
        self.list_models.setObjectName("OnbList")
        self.list_models.setMinimumHeight(120)
        self.list_models.setMaximumHeight(180)
        self.list_models.itemClicked.connect(self._on_model_item_clicked)
        self.list_models.itemDoubleClicked.connect(self._on_model_item_clicked)
        list_layout.addWidget(self.list_models)
        add_form_row(
            layout,
            "可选模型",
            list_row,
            "点「获取模型列表」后在这里直接点选；列表会自动过滤向量/语音等非对话模型",
        )

        # --------------------------------------------------------- 连通性
        self.lbl_status = QLabel("尚未测试", self)
        self.lbl_status.setObjectName("MutedLabel")
        self.lbl_status.setWordWrap(True)
        if show_test:
            test_row = QWidget(self)
            test_layout = QHBoxLayout(test_row)
            test_layout.setContentsMargins(0, 0, 0, 0)
            test_layout.setSpacing(8)
            self.btn_test = ghost_button("测试连接", test_row)
            self.btn_test.clicked.connect(self.test_connection)
            test_layout.addWidget(self.btn_test)
            test_layout.addWidget(self.lbl_status, 1)
            add_form_row(layout, "连通性", test_row)
        else:  # pragma: no cover - 保留扩展位
            layout.addRow("", self.lbl_status)

        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        self._sync_provider_display()
        self._refresh_model_list()

    # ============================================================== 取值/赋值
    def values(self) -> Dict[str, str]:
        return {
            "base_url": self.edit_base.text().strip(),
            "api_key": self.edit_key.text().strip(),
            "model": self.combo_model.currentText().strip(),
        }

    def model(self) -> str:
        return self.combo_model.currentText().strip()

    def set_values(self, base_url: str = "", api_key: str = "", model: str = "") -> None:
        provider = by_base_url(base_url)
        self.combo_preset.blockSignals(True)
        self.combo_preset.setCurrentText(provider.name if provider else CUSTOM_LABEL)
        self.combo_preset.blockSignals(False)

        self.edit_base.blockSignals(True)
        self.edit_base.setText(str(base_url or ""))
        self.edit_base.blockSignals(False)
        self.edit_key.setText(str(api_key or ""))

        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        suggestions = list(provider.models) if provider else []
        if suggestions:
            self.combo_model.addItems(suggestions)
        if model:
            if model not in suggestions:
                self.combo_model.addItem(model)
            self.combo_model.setCurrentText(model)
        elif suggestions:
            self.combo_model.setCurrentText(suggestions[0])
        self.combo_model.blockSignals(False)
        self._sync_provider_display()
        self._sync_list_selection()

    def set_models(self, models: List[str], keep_current: bool = True) -> None:
        """直接设置候选模型（自检用，不访问网络）。"""
        current = self.combo_model.currentText().strip() if keep_current else ""
        self.all_models = list(models)
        self.last_models = filter_chat_models(list(models)) or list(models)
        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        self.combo_model.addItems(self.last_models)
        self.combo_model.setCurrentText(current or (self.last_models[0] if self.last_models else ""))
        self.combo_model.blockSignals(False)
        self._refresh_model_list()

    # ============================================================== 内部行为
    def _on_base_changed(self, _text: str) -> None:
        provider = by_base_url(self.edit_base.text())
        self.combo_preset.blockSignals(True)
        self.combo_preset.setCurrentText(provider.name if provider else CUSTOM_LABEL)
        self.combo_preset.blockSignals(False)
        self._sync_provider_display()
        self.changed.emit()

    def _on_model_text_changed(self, _text: str) -> None:
        self._sync_list_selection()
        self.changed.emit()

    def _on_preset_changed(self, _index: int) -> None:
        """选择预设服务商：填 Base URL、切换到该服务商的候选模型、更新申请入口。

        切换服务商时**不保留**原来的模型名——它对新服务商几乎一定无效，
        继续留着只会让「测试连接」报出令人困惑的错误。
        """
        provider = by_label(self.combo_preset.currentText())
        if provider is None:
            self._sync_provider_display()
            return
        self.edit_base.blockSignals(True)
        self.edit_base.setText(provider.base_url)
        self.edit_base.blockSignals(False)

        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        if provider.models:
            self.combo_model.addItems(provider.models)
            self.combo_model.setCurrentText(provider.models[0])
        else:
            self.combo_model.setCurrentText("")
        self.combo_model.blockSignals(False)

        self._sync_provider_display()
        if provider.models:
            hint = "已切到「%s」，模型选为 %s；点「获取模型列表」可拉取该服务商的全部模型" % (
                provider.name,
                provider.models[0],
            )
        else:
            hint = "已切到「%s」，请点「获取模型列表」选择模型%s" % (
                provider.name,
                "（本地部署，API Key 可留空）" if provider.local else "",
            )
        self._set_status(hint, "muted")
        self._refresh_model_list()
        self.changed.emit()

    def _sync_provider_display(self) -> None:
        provider = by_label(self.combo_preset.currentText())
        if provider is None:
            self.combo_preset.setToolTip("自定义 / 其他：直接填写任意 OpenAI 兼容地址")
            self.link_docs.setText("")
            self.lbl_provider_note.setText("")
            return
        self.combo_preset.setToolTip(provider.note or provider.name)
        if provider.docs:
            self.link_docs.setText(
                '<a href="%s" style="color:#4c7df0; text-decoration:none;">申请 API Key ↗</a>'
                % provider.docs
            )
        else:
            self.link_docs.setText("")
        parts = [part for part in (provider.note, "本地部署，API Key 可留空" if provider.local else "") if part]
        self.lbl_provider_note.setText("　".join(parts))

    def _set_status(self, text: str, level: str = "muted") -> None:
        name = {"ok": "StatusOk", "warn": "StatusWarn", "bad": "StatusBad"}.get(level, "MutedLabel")
        if self.lbl_status.objectName() != name:
            self.lbl_status.setObjectName(name)
        self.lbl_status.setText(text)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)

    # ============================================================== 模型列表
    def visible_models(self) -> List[str]:
        """当前列表里显示的模型（受「含非对话模型」与筛选框影响）。"""
        source = self.all_models if self.chk_show_all.isChecked() else self.last_models
        keyword = self.edit_filter.text().strip().lower()
        if not keyword:
            return list(source)
        return [name for name in source if keyword in name.lower()]

    def _refresh_model_list(self) -> None:
        models = self.visible_models()
        self.list_models.clear()
        if not self.all_models:
            placeholder = QListWidgetItem("（点上方「获取模型列表」拉取上游模型）")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list_models.addItem(placeholder)
            self.list_models.setEnabled(False)
            self.lbl_models_count.setText("尚未获取")
            return
        self.list_models.setEnabled(True)
        for name in models:
            self.list_models.addItem(name)
        total = len(self.all_models if self.chk_show_all.isChecked() else self.last_models)
        self.lbl_models_count.setText("可选 %d 个（共 %d 个）" % (len(models), total))
        self._sync_list_selection()

    def _sync_list_selection(self) -> None:
        current = self.combo_model.currentText().strip()
        for index in range(self.list_models.count()):
            item = self.list_models.item(index)
            if item.text() == current and (item.flags() & Qt.ItemIsEnabled):
                self.list_models.setCurrentItem(item)
                return
        self.list_models.clearSelection()

    def _on_model_item_clicked(self, item: QListWidgetItem) -> None:
        if not (item.flags() & Qt.ItemIsEnabled):
            return
        name = item.text()
        self.combo_model.setCurrentText(name)
        self._set_status("已选择模型：%s" % name, "ok")
        self.model_selected.emit(name)

    # ============================================================== 动作
    def fetch_models(self, silent: bool = False) -> None:
        values = self.values()
        if not values["base_url"]:
            self._set_status("请先填写 Base URL", "bad")
            return
        if not silent:
            self._set_status("正在从上游获取模型列表…", "muted")
        self.btn_fetch.setEnabled(False)

        def _ok(result: Any) -> None:
            self.btn_fetch.setEnabled(True)
            data = result if isinstance(result, dict) else {}
            if not data.get("ok"):
                self._set_status("获取失败：%s" % truncate(data.get("message"), 70), "bad")
                return
            raw: List[str] = list(data.get("models") or [])
            chat = filter_chat_models(raw) or raw
            self.all_models = raw
            self.last_models = chat

            current = self.combo_model.currentText().strip()
            self.combo_model.blockSignals(True)
            self.combo_model.clear()
            self.combo_model.addItems(chat)
            if current and current not in chat:
                self.combo_model.addItem(current)
            self.combo_model.setCurrentText(current or (chat[0] if chat else ""))
            self.combo_model.blockSignals(False)

            self._refresh_model_list()
            filtered_out = max(0, len(raw) - len(chat))
            self._set_status(
                "✓ 获取到 %d 个模型，其中可用对话模型 %d 个%s；在下面的列表里点选即可"
                % (len(raw), len(chat), ("，已过滤 %d 个非对话模型" % filtered_out) if filtered_out else ""),
                "ok",
            )
            self.changed.emit()

        def _error(message: str) -> None:
            self.btn_fetch.setEnabled(True)
            self._set_status("获取失败：%s" % truncate(message, 70), "bad")

        self.ctx.run_task(
            # 拉全部模型，前端再按「含非对话模型」开关切换显示
            lambda: fetch_models(values["base_url"], values["api_key"], include_non_chat=True),
            on_ok=_ok,
            on_error=_error,
            key="llm_models_%s" % self._key_tag,
            label="获取模型列表",
        )

    def test_connection(self) -> None:
        values = self.values()
        if not values["base_url"]:
            self._set_status("请先填写 Base URL", "bad")
            return
        if not values["model"]:
            self._set_status("请先选择或填写模型名称", "bad")
            return
        self.btn_test.setEnabled(False)
        self._set_status("正在测试…", "muted")

        def _ok(result: Any) -> None:
            self.btn_test.setEnabled(True)
            data = result if isinstance(result, dict) else {}
            self.last_result = data
            if data.get("ok"):
                self._set_status("✓ %s" % data.get("message"), "ok")
                # 连接成功说明地址与 Key 都可用，顺手把真实模型列表拉下来
                if self.combo_model.count() <= 1:
                    self.fetch_models(silent=True)
            else:
                self._set_status("✗ %s" % data.get("message"), "bad")

        def _error(message: str) -> None:
            self.btn_test.setEnabled(True)
            self.last_result = {"ok": False, "message": message}
            self._set_status("✗ %s" % truncate(message, 70), "bad")

        self.ctx.run_task(
            lambda: test_llm(values["base_url"], values["api_key"], values["model"]),
            on_ok=_ok,
            on_error=_error,
            key="llm_test_%s" % self._key_tag,
            label="测试 LLM 连接",
        )


__all__ = ["LLMConfigForm"]
