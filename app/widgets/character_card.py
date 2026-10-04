"""角色卡展示控件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from common.utils import truncate

from ..icons import avatar_pixmap_from_file, placeholder_avatar
from .fields import ghost_button


class CharacterCard(QFrame):
    toggled = Signal(str, bool)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    proactive_requested = Signal(str)
    bind_requested = Signal(str)
    clicked = Signal(str)

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(116)
        self.data: Dict[str, Any] = dict(data or {})
        self.character_id = str(data.get("id") or "")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)

        # ------------------------------------------------------------ 头像
        self.avatar = QLabel(self)
        self.avatar.setFixedSize(64, 64)
        self.avatar.setPixmap(placeholder_avatar(str(data.get("name") or "?"), 64))
        layout.addWidget(self.avatar, 0, Qt.AlignTop)

        # ------------------------------------------------------------ 信息
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(4)
        self.name_label = QLabel(str(data.get("name") or "未命名"), self)
        self.name_label.setObjectName("CharacterName")
        info.addWidget(self.name_label)

        self.desc_label = QLabel(truncate(data.get("description") or data.get("personality") or "（没有描述）", 110), self)
        self.desc_label.setObjectName("CharacterDesc")
        self.desc_label.setWordWrap(True)
        info.addWidget(self.desc_label)

        self.meta_label = QLabel("", self)
        self.meta_label.setObjectName("MutedLabel")
        self.meta_label.setWordWrap(True)
        info.addWidget(self.meta_label)

        # 哪些机器人正在使用这个角色（多机器人时一眼能看清绑定关系）
        self.bots_label = QLabel("", self)
        self.bots_label.setObjectName("CharacterBound")
        self.bots_label.setWordWrap(True)
        info.addWidget(self.bots_label)
        info.addStretch(1)
        layout.addLayout(info, 1)

        # ------------------------------------------------------------ 操作
        actions = QVBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)

        self.enable_box = QCheckBox("参与主动消息", self)
        self.enable_box.toggled.connect(
            lambda checked: self.toggled.emit(self.character_id, bool(checked))
        )
        actions.addWidget(self.enable_box)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        self.proactive_button = ghost_button("主动消息", self)
        self.proactive_button.setProperty("chip", True)
        self.proactive_button.clicked.connect(lambda: self.proactive_requested.emit(self.character_id))
        self.bind_button = ghost_button("绑定机器人", self)
        self.bind_button.setProperty("chip", True)
        self.bind_button.clicked.connect(lambda: self.bind_requested.emit(self.character_id))
        self.edit_button = ghost_button("编辑", self)
        self.edit_button.setProperty("chip", True)
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.character_id))
        self.delete_button = ghost_button("删除", self)
        self.delete_button.setProperty("chip", True)
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self.character_id))
        buttons.addWidget(self.proactive_button)
        buttons.addWidget(self.bind_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        actions.addLayout(buttons)
        actions.addStretch(1)
        layout.addLayout(actions, 0)

        self.update_data(data)

    # ---------------------------------------------------------------- 数据
    def update_data(self, data: Dict[str, Any]) -> None:
        self.data = dict(data or {})
        self.character_id = str(data.get("id") or self.character_id)
        self.name_label.setText(str(data.get("name") or "未命名"))
        self.desc_label.setText(
            truncate(data.get("description") or data.get("personality") or "（没有描述）", 110)
        )

        enabled = bool(int(data.get("enabled") or 0))
        self.enable_box.blockSignals(True)
        self.enable_box.setChecked(enabled)
        self.enable_box.blockSignals(False)

        tags = data.get("tags_list") or []
        spec = str(data.get("card_spec") or "")
        spec_label = spec.replace("chara_card_", "").upper() or "未知"
        parts = ["今日 %s 条" % data.get("today_proactive", 0), "角色卡 %s" % spec_label]
        if tags:
            parts.append("标签：" + "、".join(str(item) for item in tags[:4]))
        self.meta_label.setText("　·　".join(parts))

        bound_bots = data.get("bound_bots") or []
        if bound_bots:
            names = "、".join(str(item.get("name") or "") for item in bound_bots if item.get("name"))
            self.bots_label.setText("使用该角色的机器人：%s" % (names or "（名称未知）"))
            self.bots_label.setObjectName("CharacterBound")
        else:
            self.bots_label.setText("还没有机器人绑定这个角色（可在「机器人」页面绑定）")
            self.bots_label.setObjectName("MutedLabel")
        self.bots_label.style().unpolish(self.bots_label)
        self.bots_label.style().polish(self.bots_label)

        avatar_path = str(data.get("avatar_abs") or "")
        if avatar_path and Path(avatar_path).exists():
            self.set_avatar(avatar_pixmap_from_file(Path(avatar_path), str(data.get("name") or ""), 64))
        else:
            self.set_avatar(placeholder_avatar(str(data.get("name") or "?"), 64))

    def set_avatar(self, pixmap: QPixmap) -> None:
        self.avatar.setPixmap(pixmap)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.character_id)
        super().mousePressEvent(event)


__all__ = ["CharacterCard"]
