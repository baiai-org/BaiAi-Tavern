"""页面基类：统一的页头、可滚动内容区与异步任务辅助。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext

from common.logging_setup import get_logger

log = get_logger("app.pages")


class Page(QWidget):
    page_title = "页面"
    page_subtitle = ""
    scrollable = False

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self._loaded_once = False

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ------------------------------------------------------------ 页头
        header = QWidget(self)
        header.setObjectName("PageHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 10)
        header_layout.setSpacing(12)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)
        self.title_label = QLabel(self.page_title, header)
        self.title_label.setObjectName("PageTitle")
        titles.addWidget(self.title_label)
        if self.page_subtitle:
            self.subtitle_label = QLabel(self.page_subtitle, header)
            self.subtitle_label.setObjectName("PageSubtitle")
            # 说明文字较长：允许换行，避免窗口不够宽时被裁掉（用户反馈过"显示不全"）
            self.subtitle_label.setWordWrap(True)
            titles.addWidget(self.subtitle_label)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)

        self.actions_layout = QHBoxLayout()
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(8)
        header_layout.addLayout(self.actions_layout)
        root.addWidget(header)

        # ------------------------------------------------------------ 内容
        container = QWidget(self)
        self.content_layout = QVBoxLayout(container)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)

        if self.scrollable:
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            # 内容容器允许收缩到比子控件最小宽度更窄：否则任何一个不换行的
            # 长文本控件（标签 / 下拉项 / 路径）都会把整页撑宽，右侧被裁掉，
            # 表现为页面"突然偏移、显示不全"（历史坑：TTS 百炼提示标签 2553px）
            container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            container.setMinimumWidth(0)
            scroll.setWidget(container)
            root.addWidget(scroll, 1)
            self.scroll_area = scroll
        else:
            root.addWidget(container, 1)
            self.scroll_area = None

        self.build(self.content_layout)

        self.ctx.status_updated.connect(self._status_guard)

    # ---------------------------------------------------------------- 扩展点
    def build(self, layout: QVBoxLayout) -> None:  # pragma: no cover - 子类实现
        """构建页面内容。"""

    def on_status(self, snapshot: Dict[str, Any]) -> None:
        """收到状态快照（默认约 3 秒一次）。"""

    def on_event(self, event: Dict[str, Any]) -> None:
        """收到 Bot 推送事件。"""

    def refresh(self) -> None:
        """主动刷新数据（页面首次显示或点击刷新按钮时调用）。"""

    def add_action(self, widget: QWidget) -> None:
        self.actions_layout.addWidget(widget)

    # ---------------------------------------------------------------- 工具
    def _status_guard(self, snapshot: Dict[str, Any]) -> None:
        try:
            self.on_status(snapshot)
        except RuntimeError:  # pragma: no cover - 页面已销毁
            pass
        except Exception as exc:
            # 状态快照来自 Bot 进程，字段由外部平台数据驱动：
            # 单个页面的渲染问题不该让整个程序弹「未处理的错误」而中断，
            # 这里记下页面名与堆栈（gui.log），界面继续可用。
            log.exception("页面「%s」处理状态快照失败：%s", self.page_title, exc)

    def ensure_loaded(self) -> None:
        if self._loaded_once:
            return
        self._loaded_once = True
        self.refresh()

    def reload_if_loaded(self) -> None:
        """已被打开过的页面重新拉取数据（配置变更后调用）。"""
        if self._loaded_once:
            self.refresh()

    def toast(self, message: str, level: str = "info") -> None:
        self.ctx.notify.emit(self.page_title, message, level)

    def run_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_ok: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        key: Optional[str] = None,
        label: str = "",
        **kwargs: Any
    ) -> bool:
        runner_label = label or key or self.page_title
        return self.ctx.run_task(
            fn,
            *args,
            on_ok=on_ok,
            on_error=on_error or self._default_error(runner_label),
            key=key,
            label=runner_label,
            **kwargs
        )

    def _default_error(self, label: str) -> Callable[[str], None]:
        def _handler(message: str) -> None:
            self.toast("%s失败：%s" % (label, message), "error")

        return _handler

    def api(self):
        return self.ctx.api


__all__ = ["Page"]
