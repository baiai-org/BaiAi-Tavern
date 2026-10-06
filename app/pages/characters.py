"""角色管理页面：导入 / 启用 / 编辑 / 删除 SillyTavern 角色卡。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..uikit import set_icon
from ..widgets.character_card import CharacterCard
from ..widgets.fields import ghost_button, hint_label, primary_button
from .base import Page

CARD_FILTER = "SillyTavern 角色卡 (*.png *.json *.yaml *.yml);;PNG (*.png);;JSON (*.json);;YAML (*.yaml *.yml);;所有文件 (*)"


class CharacterEditDialog(QDialog):
    """编辑/新建角色的核心字段（人格隔离的关键数据）。"""

    FIELDS = [
        ("name", "名称", False),
        ("description", "描述", True),
        ("personality", "性格", True),
        ("scenario", "场景", True),
        ("first_mes", "开场白", True),
        ("mes_example", "示例对话", True),
        ("system_prompt", "系统指令", True),
        ("creator_notes", "补充设定", True),
    ]

    # 新建角色时一键填充的示例，帮助用户快速上手
    TEMPLATE = {
        "name": "小助手",
        "description": "{{char}} 是一个话不多但很靠得住的朋友，喜欢听你讲生活里的小事。",
        "personality": "温和、耐心、偶尔有点冷幽默。",
        "scenario": "QQ 上的日常私聊。",
        "first_mes": "在的，今天过得怎么样？",
        "mes_example": "{{user}}：今天有点累\n{{char}}：那就先说点轻松的，晚饭吃了吗？",
        "system_prompt": "用简短、口语化的中文回复，一次只说一件事，不要长篇说教，不要提到自己是 AI。",
        "creator_notes": "",
    }

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None, creating: bool = False, ctx=None):
        super().__init__(parent)
        self.creating = creating
        self.ctx = ctx
        self.setWindowTitle(
            "新建角色（自己写人设，不需要角色卡）" if creating else "编辑角色：%s" % (data.get("name") or "")
        )
        self.resize(760, 640)
        self._editors: Dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        # 表单内容可能比窗口高（字段多 + 多行文本），包进滚动区避免底部字段被裁掉
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        for key, label, multiline in self.FIELDS:
            value = str(data.get(key) or "")
            if multiline:
                editor = QPlainTextEdit(value, self)
                editor.setMinimumHeight(88 if key != "mes_example" else 120)
            else:
                editor = QLineEdit(value, self)
            self._editors[key] = editor
            form.addRow(label, editor)

        # 占位符说明：Chub/SillyTavern 卡片的 {{char}} / {{user}} 是标准占位符，
        # 用户常把它们当成“没填完的空缺”，这里明确告知
        form.addRow(
            QLabel(""),
            hint_label(
                "上面的 {{char}} 会在聊天时自动替换为角色名，{{user}} 替换为你的昵称，"
                "是 SillyTavern 角色卡的标准写法，保留即可、不必改成中文名字。"
            ),
        )

        # Chub 卡片的「补充设定」是整张展示页 HTML，用户常当成乱码——说明白
        notes_value = str(data.get("creator_notes") or "")
        if "<" in notes_value and ">" in notes_value and len(notes_value) > 200:
            form.addRow(
                QLabel(""),
                hint_label(
                    "「补充设定」里是角色卡的作者备注——这张卡带的是 Chub 展示页整页 HTML，"
                    "很长且看起来像乱码。它不影响对话（不会发给模型），保留原样即可；"
                    "想整理的话可以只留可读的文字。"
                ),
            )

        # 音色与音色调节已移到角色管理列表的「音色」按钮（角色级独立配置，含试听）
        form.addRow(
            QLabel("音色（语音回复用）"),
            hint_label(
                "在角色卡片上点「音色」按钮单独设置：音色选择、试听、语速 / 音调 / 音量调节都在这里。"
            ),
        )

        form_scroll = QScrollArea(self)
        form_scroll.setWidgetResizable(True)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        form_container = QWidget(form_scroll)
        # 与主页面一致：内容可收缩，防止长文本把表单撑宽裁掉右侧
        form_container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form_container.setMinimumWidth(0)
        form_container_layout = QVBoxLayout(form_container)
        form_container_layout.setContentsMargins(0, 0, 6, 0)
        form_container_layout.addLayout(form)
        form_container_layout.addStretch(1)
        form_scroll.setWidget(form_container)
        layout.addWidget(form_scroll, 1)

        hint_row = QHBoxLayout()
        if creating:
            template_button = ghost_button("一键填入示例", self)
            template_button.setProperty("chip", True)
            set_icon(template_button, "wand")
            template_button.clicked.connect(self._fill_template)
            hint_row.addWidget(template_button)
        hint_row.addWidget(
            hint_label("描述/性格/场景/示例对话都会进入该角色的系统提示词，写得具体一点效果更好。")
        )
        layout.addLayout(hint_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Save).setText("创建" if creating else "保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_template(self) -> None:
        for key, value in self.TEMPLATE.items():
            editor = self._editors.get(key)
            if editor is None:
                continue
            if isinstance(editor, QPlainTextEdit):
                editor.setPlainText(value)
            else:
                editor.setText(value)

    def values(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, editor in self._editors.items():
            if isinstance(editor, QPlainTextEdit):
                result[key] = editor.toPlainText().strip()
            else:
                result[key] = editor.text().strip()
        return result

class CharacterVoiceDialog(QDialog):
    """单个角色的音色设置：音色选择 + 试听 + 语速 / 音调 / 音量调节。

    从角色编辑里拆出来（角色管理列表的「音色」按钮）。留空 / 0 值 = 跟随
    「模型路由」文字转语音线路的全局设置。
    """

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None, ctx=None):
        super().__init__(parent)
        self.ctx = ctx
        self.character_id = str(data.get("id") or "")
        self.setWindowTitle("音色设置：%s" % (data.get("name") or ""))
        self.resize(700, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)

        # ---------------------------------------------------------- 音色选择
        voice_cell = QWidget(self)
        voice_cell_layout = QVBoxLayout(voice_cell)
        voice_cell_layout.setContentsMargins(0, 0, 0, 0)
        voice_cell_layout.setSpacing(2)
        voice_row = QWidget(voice_cell)
        voice_layout = QHBoxLayout(voice_row)
        voice_layout.setContentsMargins(0, 0, 0, 0)
        voice_layout.setSpacing(6)
        from ..widgets.provider_form import EDGE_TTS_ZH_VOICES

        self.combo_voice = QComboBox(voice_row)
        self.combo_voice.setEditable(True)
        self.combo_voice.setInsertPolicy(QComboBox.NoInsert)
        self.combo_voice.addItems(list(EDGE_TTS_ZH_VOICES))
        current_voice = str(data.get("tts_voice") or "")
        existing = [self.combo_voice.itemText(i) for i in range(self.combo_voice.count())]
        if current_voice and current_voice not in existing:
            self.combo_voice.addItem(current_voice)
        self.combo_voice.setCurrentText(current_voice)
        self.combo_voice.setPlaceholderText("留空 = 跟随「模型路由」的全局音色")
        self.btn_preview_voice = ghost_button("试听", voice_row)
        self.btn_preview_voice.setProperty("chip", True)
        self.btn_preview_voice.clicked.connect(self._preview_voice)
        voice_layout.addWidget(self.combo_voice, 1)
        voice_layout.addWidget(self.btn_preview_voice)
        voice_cell_layout.addWidget(voice_row)
        voice_cell_layout.addWidget(
            hint_label(
                "该角色发语音时使用的音色（下拉含全部语种，中文在前）；"
                "留空则跟随「模型路由」里文字转语音线路的全局音色。试听文案每次随机换一句。"
            )
        )
        form.addRow(QLabel("音色"), voice_cell)

        # ---------------------------------------------------- 音色调节（角色级）
        tune_row = QWidget(self)
        tune_layout = QHBoxLayout(tune_row)
        tune_layout.setContentsMargins(0, 0, 0, 0)
        tune_layout.setSpacing(8)
        from ..widgets.provider_form import _labeled_spin

        self.spin_rate = QSpinBox(tune_row)
        self.spin_rate.setRange(-50, 100)
        self.spin_rate.setSuffix(" %")
        self.spin_rate.setValue(0)
        self.spin_pitch = QSpinBox(tune_row)
        self.spin_pitch.setRange(-50, 50)
        self.spin_pitch.setSuffix(" Hz")
        self.spin_pitch.setValue(0)
        self.spin_volume = QSpinBox(tune_row)
        self.spin_volume.setRange(-50, 100)
        self.spin_volume.setSuffix(" %")
        self.spin_volume.setValue(0)
        self.lbl_tune_edge = QLabel("edge-tts：", tune_row)
        self.lbl_tune_edge.setObjectName("HintLabel")
        tune_layout.addWidget(self.lbl_tune_edge)
        for label, spin in (("语速", self.spin_rate), ("音调", self.spin_pitch), ("音量", self.spin_volume)):
            tune_layout.addWidget(_labeled_spin(label, spin))
        self.dspin_speed = QDoubleSpinBox(tune_row)
        self.dspin_speed.setRange(0.5, 2.0)
        self.dspin_speed.setSingleStep(0.05)
        self.dspin_speed.setDecimals(2)
        self.dspin_speed.setValue(1.0)
        self.lbl_tune_openai = QLabel("OpenAI 兼容：", tune_row)
        self.lbl_tune_openai.setObjectName("HintLabel")
        tune_layout.addWidget(self.lbl_tune_openai)
        self._tune_openai_widget = _labeled_spin("语速（倍率）", self.dspin_speed)
        tune_layout.addWidget(self._tune_openai_widget)
        tune_layout.addStretch(1)
        self._tune_edge_widgets = (self.lbl_tune_edge, self.spin_rate, self.spin_pitch, self.spin_volume)
        form.addRow(
            QLabel("音色调节"),
            hint_label(
                "只对这个角色生效，覆盖「模型路由」的全局音色调节；"
                "全部填 0 / 1 则跟随全局。edge-tts 可调语速 / 音调 / 音量，OpenAI 兼容引擎可调语速倍率。"
            ),
        )
        form.addRow("", tune_row)

        # 回显已保存的角色级调节值
        def _num(key: str) -> Optional[int]:
            import re

            match = re.search(r"[-+]?\d+", str(data.get(key) or ""))
            return int(match.group()) if match else None

        rate = _num("tts_rate")
        if rate is not None:
            self.spin_rate.setValue(max(-50, min(100, rate)))
        pitch = _num("tts_pitch")
        if pitch is not None:
            self.spin_pitch.setValue(max(-50, min(50, pitch)))
        volume = _num("tts_volume")
        if volume is not None:
            self.spin_volume.setValue(max(-50, min(100, volume)))
        speed_text = str(data.get("tts_speed") or "").strip()
        if speed_text:
            try:
                self.dspin_speed.setValue(max(0.5, min(2.0, float(speed_text))))
            except ValueError:
                pass

        layout.addLayout(form)

        # 按全局 TTS 引擎显示对应的调节参数组（拉不到状态时两组都显示）
        self._sync_tune_display()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_full_voice_list()

    # -------------------------------------------------------------- 音色清单
    def _load_full_voice_list(self) -> None:
        """从 Bot 拉 edge-tts 全量音色（300+，中文在前）合并进下拉框。

        Bot 未启动 / 非 edge-tts 引擎时保留静态中文清单（可编辑输入不受影响）。
        """
        if self.ctx is None:
            return

        def _ok(data: Any) -> None:
            names = [
                str(item.get("short_name") or "").strip()
                for item in (data or [])
                if isinstance(item, dict)
            ]
            names = [name for name in names if name]
            if not names or not self.isVisible():
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
            pass

        try:
            self.ctx.run_task(
                lambda: self.ctx.api.media_voices(),
                on_ok=_ok,
                on_error=_error,
                key="char_voice_list_%s" % id(self),
                label="加载音色清单",
            )
        except Exception:
            pass

    # -------------------------------------------------------------- 引擎显示
    def _sync_tune_display(self) -> None:
        """按全局文字转语音引擎隐藏不适用的调节参数组（与模型路由页一致）。"""
        if self.ctx is None:
            return

        def _ok(status: Any) -> None:
            slots = (status or {}).get("slots") or {}
            engine = str((slots.get("tts") or {}).get("engine") or "")
            is_edge = engine in ("", "edge-tts")
            for widget in self._tune_edge_widgets:
                widget.setVisible(is_edge)
            self.lbl_tune_openai.setVisible(not is_edge)
            self._tune_openai_widget.setVisible(not is_edge)

        def _error(_message: str) -> None:
            pass

        try:
            self.ctx.run_task(
                lambda: self.ctx.api.providers_status(),
                on_ok=_ok,
                on_error=_error,
                key="char_voice_engine_%s" % id(self),
                label="读取文字转语音引擎",
            )
        except Exception:
            pass

    # -------------------------------------------------------------- 取值
    def _tune_values(self) -> Dict[str, str]:
        """试听用的当前调节值（键名与 providers.tts 的音色调节一致）。"""
        result: Dict[str, str] = {}
        if self.spin_rate.value():
            result["rate"] = "%+d%%" % self.spin_rate.value()
        if self.spin_pitch.value():
            result["pitch"] = "%+dHz" % self.spin_pitch.value()
        if self.spin_volume.value():
            result["volume"] = "%+d%%" % self.spin_volume.value()
        if abs(self.dspin_speed.value() - 1.0) > 0.001:
            result["speed"] = str(round(self.dspin_speed.value(), 2))
        return result

    def values(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"tts_voice": self.combo_voice.currentText().strip()}
        if self.spin_rate.value():
            result["tts_rate"] = "%+d%%" % self.spin_rate.value()
        else:
            result["tts_rate"] = ""
        if self.spin_pitch.value():
            result["tts_pitch"] = "%+dHz" % self.spin_pitch.value()
        else:
            result["tts_pitch"] = ""
        if self.spin_volume.value():
            result["tts_volume"] = "%+d%%" % self.spin_volume.value()
        else:
            result["tts_volume"] = ""
        if abs(self.dspin_speed.value() - 1.0) > 0.001:
            result["tts_speed"] = str(round(self.dspin_speed.value(), 2))
        else:
            result["tts_speed"] = ""
        return result

    # -------------------------------------------------------------- 试听
    def _preview_voice(self) -> None:
        if self.ctx is None:
            return
        voice = self.combo_voice.currentText().strip()
        self.btn_preview_voice.setEnabled(False)

        def _ok(data: Any) -> None:
            self.btn_preview_voice.setEnabled(True)
            data = data if isinstance(data, dict) else {}
            if not data.get("ok"):
                QMessageBox.information(self, "试听", "试听失败：%s" % (data.get("message") or "线路未配置"))
                return
            preview = str(data.get("preview_b64") or "")
            if preview:
                import base64
                import os
                import time

                from common.paths import data_dir

                try:
                    path = data_dir() / "media" / ("voice_preview_%d.mp3" % int(time.time() * 1000))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(base64.b64decode(preview))
                    os.startfile(str(path))  # type: ignore[attr-defined]
                except Exception:
                    pass
            # 试听文案是随机的：每次点「试听」换一句，多听几句判断音色
            sentence = str(data.get("preview_text") or "")
            if sentence:
                QMessageBox.information(
                    self, "试听", "本句试听文案：%s\n（再点一次「试听」会随机换一句）" % sentence
                )

        def _error(message: str) -> None:
            self.btn_preview_voice.setEnabled(True)
            QMessageBox.information(self, "试听", "试听失败：%s" % message)

        try:
            self.ctx.run_task(
                lambda: self.ctx.api.providers_test("tts", voice=voice, values=self._tune_values()),
                on_ok=_ok,
                on_error=_error,
                key="voice_preview",
                label="试听音色",
            )
        except Exception as exc:
            self.btn_preview_voice.setEnabled(True)
            QMessageBox.information(self, "试听", "试听失败：%s" % exc)


class CharactersPage(Page):
    page_title = "角色管理"
    page_subtitle = "导入 SillyTavern V2 角色卡（PNG / JSON / YAML），每个角色拥有独立人格与记忆"

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_create = primary_button("新建角色")
        self.btn_builtin = ghost_button("导入内置角色")
        self.btn_import = ghost_button("导入角色卡")
        self.btn_import_dir = ghost_button("从文件夹导入")
        self.btn_scan = ghost_button("扫描 data\\characters")
        self.btn_bots = ghost_button("机器人绑定")
        self.btn_refresh = ghost_button("刷新")
        self.btn_open_dir = ghost_button("打开角色卡目录")
        for button, icon_name in (
            (self.btn_create, "plus"),
            (self.btn_builtin, "file"),
            (self.btn_import, "file"),
            (self.btn_import_dir, "folder"),
            (self.btn_scan, "scan"),
            (self.btn_bots, "link"),
            (self.btn_refresh, "refresh"),
            (self.btn_open_dir, "folder"),
        ):
            set_icon(button, icon_name)
        for button in (
            self.btn_create,
            self.btn_builtin,
            self.btn_import,
            self.btn_import_dir,
            self.btn_scan,
            self.btn_bots,
            self.btn_refresh,
            self.btn_open_dir,
        ):
            self.add_action(button)

        self.btn_create.clicked.connect(self._create_character)
        self.btn_builtin.clicked.connect(self._import_builtin)
        self.btn_import.clicked.connect(self._import_files)
        self.btn_import_dir.clicked.connect(self._import_directory)
        self.btn_scan.clicked.connect(self._scan_directory)
        self.btn_bots.clicked.connect(lambda: self._goto_bots_page())
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_open_dir.clicked.connect(self._open_characters_dir)

        self.summary = QLabel("正在加载角色…")
        self.summary.setObjectName("ValueLabel")
        layout.addWidget(self.summary)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_container = QWidget(self.scroll)
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        layout.addWidget(self.scroll, 1)

        self.empty_label = hint_label(
            "还没有角色。三种方式任选：\n"
            "· 点「新建角色」自己写一个人设（不需要角色卡）\n"
            "· 点「导入内置角色」使用随程序附带的 3 个默认角色\n"
            "· 点「导入角色卡」选择 SillyTavern 的 PNG / JSON / YAML 卡，"
            "或把卡放进 data\\characters 后点「扫描」"
        )
        self.list_layout.insertWidget(0, self.empty_label)

    # ============================================================== 数据加载
    def refresh(self) -> None:
        def _ok(result: Any) -> None:
            characters: List[Dict[str, Any]] = result if isinstance(result, list) else []
            self._render(characters)

        self.run_task(self.api().characters, on_ok=_ok, key="load_characters", label="加载角色")

    def _render(self, characters: List[Dict[str, Any]]) -> None:
        # 清空旧卡片
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty_label:
                widget.setParent(None)
                widget.deleteLater()

        enabled = len([item for item in characters if int(item.get("enabled") or 0) == 1])
        self.summary.setText("共 %d 个角色，其中 %d 个参与主动消息" % (len(characters), enabled))

        if not characters:
            if self.empty_label.parent() is None:
                self.list_layout.insertWidget(0, self.empty_label)
            self.empty_label.show()
            return

        self.empty_label.hide()
        index = 0
        for item in characters:
            card = CharacterCard(item, self.list_container)
            card.toggled.connect(self._toggle_character)
            card.edit_requested.connect(self._edit_character)
            card.delete_requested.connect(self._delete_character)
            card.proactive_requested.connect(self._trigger_for_character)
            card.bind_requested.connect(self._bind_to_bot)
            card.voice_requested.connect(self._edit_voice)
            card.avatar_requested.connect(self._change_avatar)
            self.list_layout.insertWidget(index, card)
            index += 1

    # ============================================================== 操作实现
    def _create_character(self) -> None:
        """自定义角色：直接写人设，不需要角色卡。"""
        dialog = CharacterEditDialog({}, self, creating=True, ctx=self.ctx)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if not values.get("name"):
            QMessageBox.warning(self, "无法保存", "角色名称不能为空")
            return

        def _ok(_result: Any) -> None:
            self.toast("角色「%s」已创建" % values["name"], "info")
            self.refresh()
            self.ctx.request_status_refresh()

        def _error(message: str) -> None:
            QMessageBox.warning(self, "创建失败", message)

        self.run_task(
            self.api().create_character,
            values,
            on_ok=_ok,
            on_error=_error,
            key="create_character",
            label="新建角色",
        )

    def _import_builtin(self) -> None:
        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            if imported:
                names = "、".join(str((item.get("character") or {}).get("name") or item.get("file")) for item in imported)
                self.toast("已导入 %d 个内置角色：%s" % (len(imported), names), "info")
            else:
                self.toast("内置角色都已存在，没有新增", "info")
            if failed:
                QMessageBox.warning(
                    self,
                    "部分内置角色导入失败",
                    "\n".join("%s：%s" % (item["file"], item["error"]) for item in failed[:8]),
                )
            self.refresh()

        self.run_task(
            self.api().import_builtin_characters,
            on_ok=_ok,
            key="import_builtin",
            label="导入内置角色",
        )

    def _import_files(self) -> None:
        directory = str(self.ctx.config.effective_characters_path())
        files, _ = QFileDialog.getOpenFileNames(self, "选择 SillyTavern 角色卡", directory, CARD_FILTER)
        if not files:
            return
        paths = [Path(item) for item in files]

        def _work() -> Dict[str, Any]:
            results = []
            failures = []
            for path in paths:
                try:
                    result = self.api().import_character(path)
                    results.append({
                        "file": path.name,
                        "status": result.get("status"),
                        "name": (result.get("character") or {}).get("name"),
                        "missing": result.get("missing_core_fields") or [],
                    })
                except Exception as exc:
                    failures.append({"file": path.name, "error": str(exc)})
            return {"imported": results, "failed": failures}

        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            if imported:
                names = "、".join("%s（%s）" % (item.get("name"), "新增" if item.get("status") == "created" else "更新") for item in imported)
                message = "已导入 %d 个角色：%s" % (len(imported), names)
                # 卡片本身没写的字段要说明白：空框是卡片内容问题，不是解析丢失
                missing = next((item.get("missing") for item in imported if item.get("missing")), None)
                if len(imported) == 1 and missing:
                    message += "；这张卡片本身没有写：%s（空框属正常，可在「编辑」里补全）" % "、".join(missing)
                self.toast(message, "info")
            if failed:
                details = "\n".join("%s：%s" % (item["file"], item["error"]) for item in failed)
                QMessageBox.warning(self, "部分角色卡导入失败", details)
            self.refresh()
            self.ctx.request_status_refresh()

        self.run_task(_work, on_ok=_ok, key="import_characters", label="导入角色卡")

    def _import_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择包含角色卡的文件夹", str(self.ctx.config.effective_characters_path())
        )
        if not directory:
            return

        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            self.toast("目录导入完成：成功 %d 个，失败 %d 个" % (len(imported), len(failed)), "info" if not failed else "warn")
            if failed:
                QMessageBox.warning(
                    self,
                    "部分角色卡导入失败",
                    "\n".join("%s：%s" % (item["file"], item["error"]) for item in failed[:12]),
                )
            self.refresh()

        self.run_task(
            self.api().import_character_path,
            directory,
            on_ok=_ok,
            key="import_dir",
            label="从文件夹导入",
        )

    def _scan_directory(self) -> None:
        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            self.toast(
                "扫描完成：%s（成功 %d，失败 %d）" % ((result or {}).get("directory", ""), len(imported), len(failed)),
                "info",
            )
            self.refresh()

        self.run_task(self.api().scan_characters, on_ok=_ok, key="scan_characters", label="扫描角色卡目录")

    def _toggle_character(self, character_id: str, enabled: bool) -> None:
        self.run_task(
            self.api().set_character_enabled,
            character_id,
            enabled,
            on_ok=lambda _r: self.refresh(),
            label="切换角色状态",
        )

    def _edit_character(self, character_id: str) -> None:
        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            dialog = CharacterEditDialog(data, self, ctx=self.ctx)
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.values()
            if not values.get("name"):
                QMessageBox.warning(self, "无法保存", "角色名称不能为空")
                return

            self.run_task(
                self.api().update_character,
                character_id,
                values,
                on_ok=lambda _r: (self.toast("角色已更新", "info"), self.refresh()),
                label="保存角色",
            )

        self.run_task(self.api().character, character_id, on_ok=_ok, label="读取角色")

    def _edit_voice(self, character_id: str) -> None:
        """角色级音色设置：音色 + 试听 + 语速 / 音调 / 音量调节。"""

        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            dialog = CharacterVoiceDialog(data, self, ctx=self.ctx)
            if dialog.exec() != QDialog.Accepted:
                return
            values = dialog.values()
            name = str(data.get("name") or "")
            self.run_task(
                self.api().update_character,
                character_id,
                values,
                on_ok=lambda _r: (self.toast("「%s」的音色设置已保存" % (name or "该角色"), "info"), self.refresh()),
                label="保存角色音色",
            )

        self.run_task(self.api().character, character_id, on_ok=_ok, label="读取角色")

    def _change_avatar(self, character_id: str) -> None:
        """自选图片替换角色头像。"""
        card = next((item for item in self._cards() if item.character_id == character_id), None)
        name = card.name_label.text() if card else ""
        file, _ = QFileDialog.getOpenFileName(
            self,
            "给「%s」选择一张头像图片" % name,
            str(self.ctx.config.effective_characters_path()),
            "图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;所有文件 (*)",
        )
        if not file:
            return

        def _ok(_result: Any) -> None:
            self.toast("「%s」的头像已更换" % (name or "该角色"), "info")
            self.refresh()

        def _error(message: str) -> None:
            QMessageBox.warning(self, "更换头像失败", message)

        self.run_task(
            lambda: self.api().set_character_avatar(character_id, Path(file)),
            on_ok=_ok,
            on_error=_error,
            key="set_avatar_%s" % character_id,
            label="更换角色头像",
        )

    def _delete_character(self, character_id: str) -> None:
        confirm = QMessageBox.question(
            self,
            "删除角色",
            "确定要删除这个角色吗？该角色的对话记录与长期记忆也会一并删除，且无法恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.run_task(
            self.api().delete_character,
            character_id,
            on_ok=lambda _r: (self.toast("角色已删除", "info"), self.refresh()),
            label="删除角色",
        )

    def _cards(self) -> List[CharacterCard]:
        result: List[CharacterCard] = []
        for index in range(self.list_layout.count()):
            widget = self.list_layout.itemAt(index).widget()
            if isinstance(widget, CharacterCard):
                result.append(widget)
        return result

    def _bind_to_bot(self, character_id: str) -> None:
        """把某个角色绑定给一个机器人（一个机器人一个角色）。"""
        name = ""
        bound_ids: set = set()
        bind_button = None
        for card in self._cards():
            if card.character_id == character_id:
                name = card.name_label.text()
                bound_ids = {str(item.get("id") or "") for item in (card.data.get("bound_bots") or [])}
                bind_button = card.bind_button
                break
        self._bind_name = name

        def _ok(result: Any) -> None:
            data = result if isinstance(result, dict) else {}
            bots: List[Dict[str, Any]] = list(data.get("bots") or [])
            menu = QMenu(self)
            menu.setTitle("把「%s」绑定给…" % name)
            if not bots:
                menu.addAction("还没有机器人（请先到「机器人」页面新增）")
            for bot in bots:
                bot_id = str(bot.get("id") or "")
                bot_name = str(bot.get("name") or bot_id)
                suffix = "（当前绑定）" if bot_id in bound_ids else ""
                action = menu.addAction("%s%s" % (bot_name, suffix))
                action.triggered.connect(
                    lambda _checked=False, target=bot_id, label=bot_name: self._assign_bot(
                        target, character_id, name
                    )
                )
            if bound_ids:
                menu.addSeparator()
                for bot in bots:
                    bot_id = str(bot.get("id") or "")
                    if bot_id not in bound_ids:
                        continue
                    action = menu.addAction("解除「%s」的绑定" % (bot.get("name") or bot_id))
                    action.triggered.connect(
                        lambda _checked=False, target=bot_id: self._assign_bot(target, "", "")
                    )
            self._bind_menu = menu
            # 菜单显示在「绑定机器人」按钮正下方（不再弹到屏幕中心）
            if bind_button is not None and bind_button.isVisible():
                menu.exec(bind_button.mapToGlobal(QPoint(0, bind_button.height())))
            else:
                menu.exec(self.mapToGlobal(self.rect().center()))

        self.run_task(self.api().bots, on_ok=_ok, key="bind_load_bots", label="读取机器人列表")

    def _assign_bot(self, bot_id: str, character_id: str, character_name: str) -> None:
        if not bot_id:
            return

        def _ok(_result: Any) -> None:
            if character_id:
                self.toast("已把角色「%s」绑定给机器人" % character_name, "info")
            else:
                self.toast("已解除绑定", "info")
            self.refresh()
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().update_bot,
            bot_id,
            {"character_id": character_id, "character_name": character_name},
            on_ok=_ok,
            key="bind_bot_%s" % bot_id,
            label="绑定角色",
        )

    def _trigger_for_character(self, character_id: str) -> None:
        def _ok(result: Any) -> None:
            if not isinstance(result, dict):
                self.toast("触发完成", "info")
                return
            if result.get("skipped"):
                self.toast("未发送：%s" % result.get("reason", "条件不满足"), "warn")
            else:
                self.toast(
                    "「%s」已发送：%s" % (result.get("character"), result.get("content", "")[:40]), "info"
                )
            self.ctx.request_status_refresh()

        self.run_task(
            self.api().proactive_trigger,
            character_id,
            True,
            on_ok=_ok,
            key="trigger_%s" % character_id,
            label="角色主动消息",
        )

    def _goto_bots_page(self) -> None:
        """跳转到「机器人」页面去指定「哪个机器人用哪个角色」。"""
        window = self.window()
        jump = getattr(window, "show_page_by_key", None)
        if callable(jump):
            jump("bots")
        else:  # pragma: no cover - 兜底
            self.toast("请在左侧打开「机器人」页面设置绑定", "info")

    def _open_characters_dir(self) -> None:
        path = self.ctx.config.effective_characters_path()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            import subprocess

            subprocess.Popen(["explorer", str(path)])

    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") in ("characters_changed", "proactive_sent"):
            self.refresh()


__all__ = ["CharactersPage", "CharacterEditDialog"]
