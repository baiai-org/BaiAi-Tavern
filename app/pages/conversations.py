"""对话与记忆页面（G-17 / G-18）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from common.utils import truncate

from ..widgets.fields import ghost_button, hint_label, primary_button
from .base import Page


class ConversationsPage(Page):
    page_title = "对话查看"
    page_subtitle = "按角色查看历史消息，维护长期记忆条目"

    def build(self, layout: QVBoxLayout) -> None:
        self.btn_refresh = ghost_button("刷新")
        self.btn_clear = ghost_button("清空该角色对话")
        self.btn_add_memory = ghost_button("＋ 添加记忆")
        self.btn_delete_memory = ghost_button("删除选中记忆")
        for button in (self.btn_refresh, self.btn_clear, self.btn_add_memory, self.btn_delete_memory):
            self.add_action(button)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_clear.clicked.connect(self._clear_messages)
        self.btn_add_memory.clicked.connect(self._add_memory)
        self.btn_delete_memory.clicked.connect(self._delete_memory)

        splitter = QSplitter(Qt.Horizontal, self)

        # ------------------------------------------------------------ 左侧
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(QLabel("角色会话", left))
        self.conversation_table = QTableWidget(0, 4, left)
        self.conversation_table.setHorizontalHeaderLabels(["角色", "消息数", "最近时间", "最近内容"])
        self.conversation_table.verticalHeader().setVisible(False)
        self.conversation_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.conversation_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.conversation_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.conversation_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.conversation_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.conversation_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.conversation_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.conversation_table.itemSelectionChanged.connect(self._on_conversation_selected)
        left_layout.addWidget(self.conversation_table, 1)
        splitter.addWidget(left)

        # ------------------------------------------------------------ 右侧
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)

        self.title_label = QLabel("请选择左侧的角色", right)
        self.title_label.setObjectName("SectionTitle")
        right_layout.addWidget(self.title_label)

        tabs = QTabWidget(right)

        # 对话记录
        chat_tab = QWidget(tabs)
        chat_layout = QVBoxLayout(chat_tab)
        chat_layout.setContentsMargins(10, 10, 10, 10)
        chat_layout.setSpacing(8)
        self.message_table = QTableWidget(0, 3, chat_tab)
        self.message_table.setHorizontalHeaderLabels(["时间", "角色", "内容"])
        self.message_table.verticalHeader().setVisible(False)
        self.message_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.message_table.setWordWrap(True)
        self.message_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.message_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.message_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        chat_layout.addWidget(self.message_table, 1)
        self.chat_hint = hint_label("提示：对话历史会作为短期记忆进入提示词；清空后角色会“忘记”这段对话。")
        chat_layout.addWidget(self.chat_hint)
        tabs.addTab(chat_tab, "对话记录")

        # 长期记忆
        memory_tab = QWidget(tabs)
        memory_layout = QVBoxLayout(memory_tab)
        memory_layout.setContentsMargins(10, 10, 10, 10)
        memory_layout.setSpacing(8)

        add_row = QHBoxLayout()
        self.memory_input = QLineEdit(memory_tab)
        self.memory_input.setPlaceholderText("例如：用户喜欢在深夜加班，讨厌被催稿")
        self.memory_input.returnPressed.connect(self._add_memory)
        memory_add_button = primary_button("添加", memory_tab)
        memory_add_button.clicked.connect(self._add_memory)
        add_row.addWidget(self.memory_input, 1)
        add_row.addWidget(memory_add_button)
        memory_layout.addLayout(add_row)

        self.memory_table = QTableWidget(0, 3, memory_tab)
        self.memory_table.setHorizontalHeaderLabels(["时间", "内容", "ID"])
        self.memory_table.verticalHeader().setVisible(False)
        self.memory_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.memory_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.memory_table.setWordWrap(True)
        self.memory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.memory_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.memory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        memory_layout.addWidget(self.memory_table, 1)
        memory_layout.addWidget(hint_label("主动消息与回复生成时会自动检索最相关的若干条记忆。"))
        tabs.addTab(memory_tab, "长期记忆")

        right_layout.addWidget(tabs, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.current_character_id: Optional[str] = None
        self.current_character_name: str = ""

    # ============================================================== 数据加载
    def refresh(self) -> None:
        def _ok(result: Any) -> None:
            conversations: List[Dict[str, Any]] = result if isinstance(result, list) else []
            # 重新填充表格会清掉现有选中；记住当前角色，填充后把选中跟回去，
            # 否则点「刷新」后消息面板会停在旧角色上不再更新
            prev_id = self.current_character_id
            self.conversation_table.setRowCount(len(conversations))
            for row, item in enumerate(conversations):
                name_item = QTableWidgetItem(str(item.get("name") or "未知"))
                name_item.setData(Qt.UserRole, str(item.get("character_id") or ""))
                self.conversation_table.setItem(row, 0, name_item)
                count_item = QTableWidgetItem(str(item.get("message_count") or 0))
                count_item.setTextAlignment(Qt.AlignCenter)
                self.conversation_table.setItem(row, 1, count_item)
                self.conversation_table.setItem(row, 2, QTableWidgetItem(str(item.get("last_at") or "-")))
                self.conversation_table.setItem(row, 3, QTableWidgetItem(truncate(item.get("last_content") or "-", 60)))
            if not conversations:
                self.current_character_id = None
                return
            target = 0
            if prev_id:
                for row, item in enumerate(conversations):
                    if str(item.get("character_id") or "") == prev_id:
                        target = row
                        break
            self.conversation_table.selectRow(target)

        self.run_task(self.api().conversations, on_ok=_ok, key="load_conversations", label="加载会话")

    def _on_conversation_selected(self) -> None:
        rows = self.conversation_table.selectionModel().selectedRows() if self.conversation_table.selectionModel() else []
        if not rows:
            return
        row = rows[0].row()
        name_item = self.conversation_table.item(row, 0)
        if name_item is None:
            return
        character_id = str(name_item.data(Qt.UserRole) or "")
        self.current_character_id = character_id
        self.current_character_name = name_item.text()
        self.title_label.setText("%s（ID：%s）" % (self.current_character_name, character_id))
        self._load_messages()
        self._load_memories()

    def _load_messages(self) -> None:
        if not self.current_character_id:
            return
        character_id = self.current_character_id

        def _ok(result: Any) -> None:
            if character_id != self.current_character_id:
                return
            messages = (result or {}).get("messages") or []
            self.message_table.setRowCount(len(messages))
            for row, item in enumerate(messages):
                role = str(item.get("role") or "")
                speaker = self.current_character_name if role == "assistant" else "我"
                if int(item.get("is_proactive") or 0) == 1:
                    speaker += "（主动）"
                content_text = str(item.get("content") or "")
                kind = str(item.get("kind") or "text")
                if kind == "image":
                    # 图片消息：入库时已带【图片】前缀，缺了则补上
                    if "【图片】" not in content_text:
                        content_text = ("【图片】" + content_text).strip()
                else:
                    # 回复里的 [IMG] 生图标记已在 QQ 里发成图片，这里不重复展示
                    lines = [ln for ln in content_text.splitlines() if not ln.strip().upper().startswith("[IMG]")]
                    cleaned = "\n".join(lines).strip()
                    if cleaned:
                        content_text = cleaned
                self.message_table.setItem(row, 0, QTableWidgetItem(str(item.get("created_at") or "")))
                speaker_item = QTableWidgetItem(speaker)
                speaker_item.setForeground(
                    Qt.GlobalColor.gray if role != "assistant" else Qt.GlobalColor.white
                )
                self.message_table.setItem(row, 1, speaker_item)
                self.message_table.setItem(row, 2, QTableWidgetItem(content_text))
            self.message_table.resizeRowsToContents()
            if messages:
                self.message_table.scrollToBottom()

        self.run_task(self.api().messages, character_id, 300, on_ok=_ok, label="加载对话")

    def _load_memories(self) -> None:
        if not self.current_character_id:
            return
        character_id = self.current_character_id

        def _ok(result: Any) -> None:
            if character_id != self.current_character_id:
                return
            memories = result if isinstance(result, list) else []
            self.memory_table.setRowCount(len(memories))
            for row, item in enumerate(memories):
                self.memory_table.setItem(row, 0, QTableWidgetItem(str(item.get("created_at") or "")))
                self.memory_table.setItem(row, 1, QTableWidgetItem(str(item.get("content") or "")))
                id_item = QTableWidgetItem(str(item.get("id") or ""))
                # 存原始值（字符串）：QVariant 装不下超大整数，交给接口时再转
                id_item.setData(Qt.UserRole, str(item.get("id") or ""))
                self.memory_table.setItem(row, 2, id_item)
            self.memory_table.resizeRowsToContents()

        self.run_task(self.api().memories, character_id, on_ok=_ok, label="加载记忆")

    # ============================================================== 操作
    def _clear_messages(self) -> None:
        if not self.current_character_id:
            self.toast("请先选择一个角色", "warn")
            return
        confirm = QMessageBox.question(
            self,
            "清空对话",
            "确定要清空「%s」的全部对话记录吗？" % self.current_character_name,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.run_task(
            self.api().clear_messages,
            self.current_character_id,
            on_ok=lambda result: (
                self.toast("已清空 %s 条消息" % (result or {}).get("removed", 0), "info"),
                self._load_messages(),
                self.refresh(),
            ),
            label="清空对话",
        )

    def _add_memory(self) -> None:
        if not self.current_character_id:
            self.toast("请先选择一个角色", "warn")
            return
        content = self.memory_input.text().strip()
        if not content:
            text, ok = QInputDialog.getMultiLineText(self, "添加长期记忆", "记忆内容：")
            if not ok or not text.strip():
                return
            content = text.strip()

        def _ok(_result: Any) -> None:
            self.memory_input.clear()
            self.toast("记忆已添加", "info")
            self._load_memories()

        self.run_task(
            self.api().add_memory, self.current_character_id, content, on_ok=_ok, label="添加记忆"
        )

    def _delete_memory(self) -> None:
        rows = self.memory_table.selectionModel().selectedRows() if self.memory_table.selectionModel() else []
        if not rows:
            self.toast("请先在表格中选择一条记忆", "warn")
            return
        memory_id = self.memory_table.item(rows[0].row(), 2).data(Qt.UserRole)
        self.run_task(
            self.api().delete_memory,
            str(memory_id or ""),
            on_ok=lambda _r: (self.toast("记忆已删除", "info"), self._load_memories()),
            label="删除记忆",
        )

    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("type") == "chat_activity":
            self.refresh()
            if self.current_character_id:
                self._load_messages()


__all__ = ["ConversationsPage"]
