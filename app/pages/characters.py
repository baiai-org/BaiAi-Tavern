"""角色管理页面：导入 / 启用 / 编辑 / 删除 SillyTavern 角色卡。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
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

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None, creating: bool = False):
        super().__init__(parent)
        self.creating = creating
        self.setWindowTitle(
            "新建角色（自己写人设，不需要角色卡）" if creating else "编辑角色：%s" % (data.get("name") or "")
        )
        self.resize(760, 640)
        self._editors: Dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

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
        layout.addLayout(form)

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
            self.list_layout.insertWidget(index, card)
            index += 1

    # ============================================================== 操作实现
    def _create_character(self) -> None:
        """自定义角色：直接写人设，不需要角色卡。"""
        dialog = CharacterEditDialog({}, self, creating=True)
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
                    results.append({"file": path.name, "status": result.get("status"), "name": (result.get("character") or {}).get("name")})
                except Exception as exc:
                    failures.append({"file": path.name, "error": str(exc)})
            return {"imported": results, "failed": failures}

        def _ok(result: Any) -> None:
            imported = (result or {}).get("imported") or []
            failed = (result or {}).get("failed") or []
            if imported:
                names = "、".join("%s（%s）" % (item.get("name"), "新增" if item.get("status") == "created" else "更新") for item in imported)
                self.toast("已导入 %d 个角色：%s" % (len(imported), names), "info")
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
            dialog = CharacterEditDialog(data, self)
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
        for card in self._cards():
            if card.character_id == character_id:
                name = card.name_label.text()
                bound_ids = {str(item.get("id") or "") for item in (card.data.get("bound_bots") or [])}
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
