"""「关于」对话框：应用信息 + 引用的开源项目（按 1. 2. 3. 排列，带链接）。

开源清单集中在这里维护：新增依赖时同步补一条即可，README 里也引用同一份数据。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from common.logging_setup import get_logger

from . import APP_AUTHOR, APP_DISPLAY_NAME, APP_HOMEPAGE, APP_VERSION_DISPLAY
from .icons import app_pixmap
from .widgets.fields import ghost_button

log = get_logger("app.about")

# (名称, 用途, 主页) —— 顺序即界面上的 1. 2. 3. …
OPEN_SOURCE_PROJECTS: List[Tuple[str, str, str]] = [
    ("Python", "程序运行环境", "https://www.python.org/"),
    ("PySide6 / Qt for Python", "桌面界面、托盘与控件绘制（LGPLv3）", "https://doc.qt.io/qtforpython/"),
    ("FastAPI", "Bot 进程的本地 HTTP 控制接口", "https://fastapi.tiangolo.com/"),
    ("Uvicorn", "ASGI 服务器", "https://www.uvicorn.org/"),
    ("APScheduler", "定时 / 空闲 / 随机主动消息调度", "https://apscheduler.readthedocs.io/"),
    ("aiosqlite", "SQLite 异步访问", "https://github.com/omnilib/aiosqlite"),
    ("httpx", "HTTP 客户端（GUI↔Bot、LLM、QQ 接口）", "https://www.python-httpx.org/"),
    ("websockets", "QQ 官方机器人 WebSocket 网关", "https://websockets.readthedocs.io/"),
    ("OpenAI Python SDK", "调用兼容 OpenAI 协议的大模型接口", "https://github.com/openai/openai-python"),
    ("PyYAML", "读写 config.yaml", "https://pyyaml.org/"),
    ("Pillow", "角色卡图片处理", "https://python-pillow.org/"),
    ("PyInstaller", "打包为 Windows 可执行文件", "https://pyinstaller.org/"),
    ("Tkinter / Tcl-Tk", "安装 / 卸载向导的界面（Python 标准库）", "https://docs.python.org/3/library/tkinter.html"),
]


def open_source_text(numbered: bool = True) -> str:
    """把开源清单渲染成文本（收藏、复制、粘贴到别处都用同一份）。"""
    lines: List[str] = []
    for index, (name, usage, url) in enumerate(OPEN_SOURCE_PROJECTS, 1):
        prefix = "%d. " % index if numbered else "- "
        lines.append("%s%s —— %s\n   %s" % (prefix, name, usage, url))
    return "\n".join(lines)


class AboutDialog(QDialog):
    """关于窗口（非模态，可一直开着）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("关于 %s" % APP_DISPLAY_NAME)
        self.setObjectName("AboutDialog")
        self.setMinimumSize(640, 620)
        self.resize(680, 660)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        # ------------------------------------------------------------ 头部
        head = QHBoxLayout()
        head.setSpacing(14)
        icon = QLabel(self)
        icon.setPixmap(app_pixmap(56))
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel("%s  %s" % (APP_DISPLAY_NAME, APP_VERSION_DISPLAY), self)
        name.setObjectName("PageTitle")
        titles.addWidget(name)
        author = QLabel("作者：%s　·　%s" % (APP_AUTHOR, APP_HOMEPAGE), self)
        author.setObjectName("PageSubtitle")
        author.setTextInteractionFlags(Qt.TextSelectableByMouse)
        titles.addWidget(author)
        head.addLayout(titles)
        head.addStretch(1)
        layout.addLayout(head)

        intro = QLabel(
            "QQ 多角色 AI 主动消息桌面应用：导入 SillyTavern 角色卡或自己写人设，"
            "给每个机器人绑定一个角色，由角色在合适的时机主动找你说话。",
            self,
        )
        intro.setObjectName("OnbText")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ------------------------------------------------------ 开源项目
        heading = QLabel("引用的开源项目（点击链接可打开主页）", self)
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget(scroll)
        container.setObjectName("AboutList")
        list_layout = QVBoxLayout(container)
        list_layout.setContentsMargins(12, 10, 12, 10)
        list_layout.setSpacing(8)
        for index, (name_text, usage, url) in enumerate(OPEN_SOURCE_PROJECTS, 1):
            item = QLabel(
                '<b>%d. %s</b> —— %s<br/>'
                '<a href="%s" style="color:#6f9bff; text-decoration:none;">%s</a>'
                % (index, name_text, usage, url, url),
                container,
            )
            item.setObjectName("OnbText")
            item.setWordWrap(True)
            item.setTextInteractionFlags(Qt.TextBrowserInteraction)
            item.setOpenExternalLinks(True)
            list_layout.addWidget(item)
        list_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        # ------------------------------------------------------------ 底部
        buttons = QDialogButtonBox(self)
        self.btn_copy = ghost_button("复制开源清单", self)
        self.btn_copy.clicked.connect(self.copy_list)
        buttons.addButton(self.btn_copy, QDialogButtonBox.ActionRole)
        self.btn_close = buttons.addButton("关闭", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------------------------------------------------------------- 动作
    def copy_list(self) -> None:
        try:
            QGuiApplication.clipboard().setText(open_source_text())
            self.btn_copy.setText("已复制")
        except Exception as exc:  # pragma: no cover
            log.debug("复制开源清单失败：%s", exc)


def show_about(parent: Optional[QWidget] = None) -> AboutDialog:
    """显示「关于」窗口（非模态，重复点击时复用同一个窗口）。"""
    existing = getattr(parent, "_about_dialog", None) if parent is not None else None
    dialog = existing if isinstance(existing, AboutDialog) else AboutDialog(parent)
    if parent is not None:
        setattr(parent, "_about_dialog", dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


__all__ = ["AboutDialog", "OPEN_SOURCE_PROJECTS", "open_source_text", "show_about"]
