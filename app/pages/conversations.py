"""对话与记忆页面（G-17 / G-18）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QMouseEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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


class _MediaCellWidget(QWidget):
    """可点击的媒体单元格容器（图片缩略图 / 语音徽标）。

    QTableWidget 里用 setCellWidget 塞自定义控件后，子控件会把鼠标事件
    吃掉，表格的 itemClicked 不再触发（V0.2.2 前「缩略图点了没反应」
    的根因）。这里让容器自己响应左键点击并发出 clicked 信号；子控件
    全部设为鼠标事件透明（见 _MediaCellWidget 使用处的
    WA_TransparentForMouseEvents），保证点缩略图任意位置都算点击。
    """

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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

        # 按月 / 按天 / 关键词检索（V0.2.2：对话记录分级保存，方便翻旧账）
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.month_combo = QComboBox(chat_tab)
        self.month_combo.setMinimumWidth(110)
        self.month_combo.addItem("全部月份")
        self.day_combo = QComboBox(chat_tab)
        self.day_combo.setMinimumWidth(130)
        self.day_combo.addItem("全部日期")
        self.search_input = QLineEdit(chat_tab)
        self.search_input.setPlaceholderText("搜索消息内容…")
        self.search_input.returnPressed.connect(lambda: self._load_messages(search=self.search_input.text().strip()))
        filter_row.addWidget(self.month_combo, 0)
        filter_row.addWidget(self.day_combo, 0)
        filter_row.addWidget(self.search_input, 1)
        chat_layout.addLayout(filter_row)
        self.month_combo.currentIndexChanged.connect(lambda _i: self._load_messages())
        self.day_combo.currentIndexChanged.connect(lambda _i: self._load_messages())

        self.message_table = QTableWidget(0, 3, chat_tab)
        self.message_table.setHorizontalHeaderLabels(["时间", "角色", "内容"])
        self.message_table.verticalHeader().setVisible(False)
        self.message_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.message_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.message_table.setWordWrap(True)
        self.message_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.message_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.message_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        chat_layout.addWidget(self.message_table, 1)
        media_row = QHBoxLayout()
        media_row.setSpacing(8)
        self.btn_view_media = ghost_button("查看选中消息的图片 / 播放语音")
        self.btn_view_media.setEnabled(False)
        self.media_status = hint_label("图片 / 语音消息在列表里直接显示缩略图和播放徽标，点一下就能查看 / 播放")
        media_row.addWidget(self.btn_view_media)
        media_row.addWidget(self.media_status, 1)
        chat_layout.addLayout(media_row)
        self.message_table.itemSelectionChanged.connect(self._on_message_selected)
        self.message_table.itemClicked.connect(self._on_message_cell_clicked)
        self.btn_view_media.clicked.connect(self._view_selected_media)
        self.chat_hint = hint_label(
            "提示：最近 7 天的对话原文进上下文；更早的会压缩成摘要（15 天内）供角色回忆，"
            "更久的只归档保存在这里，可用上方月份 / 日期 / 搜索查看。"
        )
        chat_layout.addWidget(self.chat_hint)
        tabs.addTab(chat_tab, "对话记录")

        # 语音播放器（懒创建，页面销毁时随 self 一起释放）
        self._player: Optional[QMediaPlayer] = None
        self._audio_output: Optional[QAudioOutput] = None

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

    # ========================================================== 媒体查看（V0.2.2）
    def _selected_media_row(self) -> Optional[Dict[str, str]]:
        model = self.message_table.selectionModel()
        if not model:
            return None
        # 遍历所有选中行（而不是只看第 0 列的 selectedRows）：
        # 用户点中「角色」列的单个 item 时同样能找到该行媒体
        rows = sorted({index.row() for index in model.selectedIndexes()})
        for row in rows:
            time_item = self.message_table.item(row, 0)
            if time_item is None:
                continue
            kind = str(time_item.data(Qt.UserRole + 1) or "text")
            media_path = str(time_item.data(Qt.UserRole + 2) or "")
            if kind in ("image", "voice") and media_path:
                return {"kind": kind, "path": media_path}
        return None

    def _on_message_selected(self) -> None:
        self.btn_view_media.setEnabled(self._selected_media_row() is not None)

    def _on_message_cell_clicked(self, row: int, column: int) -> None:
        """点击列表里的图片缩略图 / 语音徽标，直接查看 / 播放。

        单元格是自定义控件（setCellWidget）时，Qt 不会触发 itemClicked
        （子控件把鼠标事件吃掉了），所以媒体单元格自己发 clicked 信号
        走同一个入口（见 ``_MediaCellWidget``）。
        """
        if column != 2:
            return
        self._activate_media_row(row)

    def _activate_media_row(self, row: int) -> None:
        # 让该行成为选中行（_selected_media_row 依赖 selectionModel）
        current = self.message_table.currentRow()
        if current != row:
            self.message_table.selectRow(row)
        if self._selected_media_row() is not None:
            self._view_selected_media()

    def _media_cell_widget(self, kind: str, path: str, caption: str, row: int, parent: QWidget):
        """内容列的媒体单元格：图片显示缩略图，语音显示播放徽标。

        整个单元格可点击（点缩略图 / 徽标 / 文字都能查看图片、播放语音）；
        文件不在磁盘上时返回 None（调用方退回纯文字显示）。
        """
        from PySide6.QtGui import QPixmap

        from ..uikit import icon as _icon

        if not Path(path).is_file():
            return None
        container = _MediaCellWidget(parent)
        container.setToolTip(path)
        container.clicked.connect(lambda r=row: self._activate_media_row(r))
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(8)
        if kind == "image":
            label = QLabel(container)
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                label.setPixmap(
                    pixmap.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                label.setText("图片")
                label.setAlignment(Qt.AlignCenter)
            label.setFixedSize(72, 72)
            label.setToolTip(path)
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(label, 0, Qt.AlignTop)
            text_label = QLabel(caption or "（用户发来了一张图片，点击查看）", container)
            text_label.setWordWrap(True)
            text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(text_label, 1)
        else:
            # 语音徽标：QPainter 画的播放三角（QLabel 只能 setPixmap，没有 setIcon）
            # + 文字，不依赖 emoji 字形
            from PySide6.QtCore import QSize

            triangle = QLabel(container)
            triangle.setPixmap(_icon("play", "#4a90d9", 16).pixmap(QSize(16, 16)))
            triangle.setToolTip(path)
            triangle.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            badge = QLabel("语音", container)
            badge.setStyleSheet("font-weight: 600; color: #4a90d9;")
            badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(triangle, 0, Qt.AlignTop)
            layout.addWidget(badge, 0, Qt.AlignTop)
            text_label = QLabel(caption or "（用户发来了一条语音，点击播放）", container)
            text_label.setWordWrap(True)
            text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(text_label, 1)
        return container

    def _view_selected_media(self) -> None:
        from pathlib import Path

        info = self._selected_media_row()
        if not info:
            return
        path = Path(info["path"])
        if not path.is_file():
            self.media_status.setText("媒体文件已不在磁盘上（路径：%s）" % info["path"])
            return
        if info["kind"] == "image":
            self._open_with_system(path)
            self.media_status.setText("已打开图片：%s" % path.name)
        else:
            # 语音：内置播放器（媒体文件名不带点，按尾缀判断；
            # silk 格式 Qt 播不了，交给系统程序）
            tail = path.name.lower().rpartition("_")[-1]
            if tail == "silk":
                self._open_with_system(path)
                self.media_status.setText("已用系统程序打开 silk 语音：%s" % path.name)
                return
            try:
                if self._player is None:
                    self._audio_output = QAudioOutput(self)
                    self._player = QMediaPlayer(self)
                    self._player.setAudioOutput(self._audio_output)
                self._player.setSource(QUrl.fromLocalFile(str(path)))
                self._player.play()
                self.media_status.setText("正在播放：%s（再点一次继续播放下一条）" % path.name)
            except Exception as exc:  # pragma: no cover
                self.media_status.setText("播放失败：%s" % exc)

    @staticmethod
    def _open_with_system(path: Path) -> None:
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception:  # pragma: no cover
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]

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
        # 换角色：清掉上一次的筛选条件
        self.month_combo.blockSignals(True)
        self.day_combo.blockSignals(True)
        self.month_combo.setCurrentIndex(0)
        self.day_combo.setCurrentIndex(0)
        self.month_combo.blockSignals(False)
        self.day_combo.blockSignals(False)
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)
        self.btn_view_media.setEnabled(False)
        self._load_messages()
        self._load_memories()

    def _load_messages(self, search: str = "") -> None:
        if not self.current_character_id:
            return
        character_id = self.current_character_id

        def _ok(result: Any) -> None:
            if character_id != self.current_character_id:
                return
            messages = (result or {}).get("messages") or []
            # clearContents 清掉上一轮遗留的单元格 widget（媒体缩略图），
            # 否则复用的行会带着上次的缩略图
            self.message_table.setRowCount(len(messages))
            self.message_table.clearContents()
            for row, item in enumerate(messages):
                role = str(item.get("role") or "")
                speaker = self.current_character_name if role == "assistant" else "我"
                if int(item.get("is_proactive") or 0) == 1:
                    speaker += "（主动）"
                content_text = str(item.get("content") or "")
                kind = str(item.get("kind") or "text")
                media_path = str(item.get("media_path") or "")
                if kind == "image":
                    # 图片消息：入库时已带【图片】前缀，缺了则补上；
                    # 有存档文件时直接显示缩略图（V0.2.2）
                    if "【图片】" in content_text:
                        caption = content_text.split("】", 1)[1].strip()
                    else:
                        caption = content_text.strip()
                elif kind == "voice":
                    # 语音消息：保留【语音】前缀 + 转写文字；有存档时显示播放徽标
                    if "【语音】" in content_text:
                        caption = content_text.split("】", 1)[1].strip()
                    else:
                        caption = content_text.strip()
                else:
                    caption = content_text
                    # 回复里的 [IMG] 生图标记已在 QQ 里发成图片，这里不重复展示
                    lines = [ln for ln in caption.splitlines() if not ln.strip().upper().startswith("[IMG]")]
                    cleaned = "\n".join(lines).strip()
                    if cleaned:
                        caption = cleaned
                time_item = QTableWidgetItem(str(item.get("created_at") or ""))
                time_item.setData(Qt.UserRole + 1, kind)
                time_item.setData(Qt.UserRole + 2, media_path)
                self.message_table.setItem(row, 0, time_item)
                speaker_item = QTableWidgetItem(speaker)
                speaker_item.setForeground(
                    Qt.GlobalColor.gray if role != "assistant" else Qt.GlobalColor.white
                )
                self.message_table.setItem(row, 1, speaker_item)
                cell = self._media_cell_widget(kind, media_path, caption, row, self.message_table)
                if cell is not None:
                    self.message_table.setCellWidget(row, 2, cell)
                else:
                    self.message_table.setItem(row, 2, QTableWidgetItem(caption))
            self.message_table.resizeRowsToContents()
            if messages:
                self.message_table.scrollToBottom()

        month = self._selected_month()
        day = self._selected_day()
        search = search or self.search_input.text().strip()
        self.run_task(
            lambda: self.api().messages(character_id, limit=300, search=search, month=month, day=day),
            on_ok=_ok,
            key="load_messages",
            label="加载消息",
        )
        if not month and not day and not search:
            # 无筛选时顺带加载月份 / 日期下拉（切角色后也要重新拉）
            self._load_date_filters()

    def _selected_month(self) -> str:
        index = self.month_combo.currentIndex()
        return str(self.month_combo.itemData(index) or "") if index > 0 else ""

    def _selected_day(self) -> str:
        index = self.day_combo.currentIndex()
        return str(self.day_combo.itemData(index) or "") if index > 0 else ""

    def _load_date_filters(self) -> None:
        if not self.current_character_id:
            return
        character_id = self.current_character_id

        def _day_ok(result: Any) -> None:
            if character_id != self.current_character_id:
                return
            days = (result or {}).get("days") or []
            prev_day = self._selected_day()
            self.day_combo.blockSignals(True)
            self.day_combo.clear()
            self.day_combo.addItem("全部日期")
            for day in days[:60]:
                self.day_combo.addItem(day, day)
            if prev_day:
                target = self.day_combo.findData(prev_day)
                if target > 0:
                    self.day_combo.setCurrentIndex(target)
            else:
                self.day_combo.setCurrentIndex(0)
            self.day_combo.blockSignals(False)

        def _month_ok(months_result: Any) -> None:
            if character_id != self.current_character_id:
                return
            months = (months_result or {}).get("months") or []
            prev_month = self._selected_month()
            self.month_combo.blockSignals(True)
            self.month_combo.clear()
            self.month_combo.addItem("全部月份")
            for month in months[:36]:
                self.month_combo.addItem(month, month)
            if prev_month:
                target = self.month_combo.findData(prev_month)
                if target > 0:
                    self.month_combo.setCurrentIndex(target)
                else:
                    self.month_combo.setCurrentIndex(0)
            self.month_combo.blockSignals(False)
            month = self._selected_month()
            self.run_task(
                lambda: self.api().message_days(character_id, month=month),
                on_ok=_day_ok,
                key="load_message_days",
                label="加载日期列表",
            )

        self.run_task(
            lambda: self.api().message_months(character_id),
            on_ok=_month_ok,
            key="load_message_months",
            label="加载月份列表",
        )

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
