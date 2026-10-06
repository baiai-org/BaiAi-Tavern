"""GUI 集成自检：用真实的 Bot 进程 + mock QQ 官方机器人/LLM，逐页验证界面行为。

运行::

    python -m tests.gui_smoke

检查内容：

* 主窗口与八个页面能正常构建，左下角「关于」按钮在导航下方；
* 「关于」窗口显示名称 / V0.2.1 / baiai.org，开源清单按 1. 2. 3. 编号并带链接；
* 状态轮询把 Bot 状态推送到界面（仪表盘卡片、状态栏、侧边栏）；
* 图标全部是 QPainter 绘制或由 UI 资源生成，不依赖 emoji 字形；
* 每个页面的文字排版都没有被裁切；
* 角色管理页渲染角色卡，并能通过界面切换启用状态；
* 主动消息页能读取并保存配置；
* 对话查看页能列出会话与消息（消息来自 mock 的官方机器人单聊事件）；
* 系统设置页能回显配置，并能真的点「测试连接」「重新连接」连接到 mock 开放平台；
* 机器人管理页能新增 / 保存 / 删除第 2 个机器人；
* 点击「立即触发主动消息」后，消息真的经 Bot 送到了（mock）官方平台；
* WebSocket 事件流能连上并把事件送到界面。

QQ 只有一种接入方式——**QQ 官方机器人**（AppID + AppSecret → access_token → 网关），
因此自检也只用官方的单聊/群 @ 事件，不涉及任何第三方协议。

使用 Qt 的 offscreen 平台，无需真实桌面环境。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QLabel, QLineEdit  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# mock 官方平台里“给机器人发消息的用户”的 openid
TARGET_OPENID = "user-official"
REPLY_TEXT = "今天也辛苦啦。"
PROACTIVE_TEXT = "忽然想问问你在做什么。"

# 历史遗留键（旧版第三方协议 NapCat / OneBot 的配置字段）：自检配置里不应该出现
LEGACY_QQ_KEYS = (
    "mode",
    "napcat_api_url",
    "access_token",
    "self_id",
    "target_user_id",
    "allowed_group_ids",
    "reply_max_segments",
    "reply_segment_max_len",
    "typing_speed_cps",
    "typing_delay_max",
    "typing_delay_enabled",
    "send_retries",
)
LEGACY_APP_KEYS = ("start_napcat_on_launch",)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("  [PASS] %s" % name)
        else:
            self.failed.append(name)
            print("  [FAIL] %s%s" % (name, ("  ->  %s" % detail) if detail else ""))
        return bool(condition)

    def summary(self) -> int:
        print("\n" + "=" * 74)
        print("  GUI 自检结果：%d 项通过，%d 项失败" % (self.passed, len(self.failed)))
        for item in self.failed:
            print("   · 失败：%s" % item)
        print("=" * 74)
        return 0 if not self.failed else 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pump(app, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def install_excepthook(caught: List[str]) -> None:
    """捕获未处理异常。

    Qt 槽函数里抛出的异常不会传回调用方（PySide6 交给 sys.excepthook），
    因此在真实程序里会弹「程序遇到未处理的错误」对话框，而在自检里会被静默吞掉。
    这里显式收集起来，最后断言为空——否则这类 bug 会漏过测试。
    """
    import traceback

    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        caught.append("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    sys.excepthook = _hook


def patch_message_boxes(recorded: List[str]) -> None:
    """让所有模态弹窗自动应答。

    模态对话框会开启自己的事件循环并一直等到用户点击——在无人值守的自检里就是死等。
    这里统一自动回答，并把内容记下来供断言。
    """
    from PySide6.QtWidgets import QMessageBox

    def _record(text: str) -> None:
        recorded.append(text)

    def _information(_parent, title, text, *args, **kwargs):
        _record("%s | %s" % (title, text))
        return QMessageBox.Ok

    def _warning(_parent, title, text, *args, **kwargs):
        _record("%s | %s" % (title, text))
        return QMessageBox.Ok

    def _critical(_parent, title, text, *args, **kwargs):
        _record("%s | %s" % (title, text))
        return QMessageBox.Ok

    def _question(_parent, title, text, *args, **kwargs):
        _record("%s | %s" % (title, text))
        return QMessageBox.Yes

    QMessageBox.information = staticmethod(_information)  # type: ignore[assignment]
    QMessageBox.warning = staticmethod(_warning)  # type: ignore[assignment]
    QMessageBox.critical = staticmethod(_critical)  # type: ignore[assignment]
    QMessageBox.question = staticmethod(_question)  # type: ignore[assignment]


def wait_until(app, predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def character_cards(page) -> List[Any]:
    """取当前渲染出来的角色卡控件（刷新后必须重新获取，旧控件已被销毁）。"""
    from app.widgets.character_card import CharacterCard

    result = []
    for index in range(page.list_layout.count()):
        widget = page.list_layout.itemAt(index).widget()
        if isinstance(widget, CharacterCard):
            result.append(widget)
    return result


def _limit_in_file(path) -> Any:
    """读取 config.yaml 里的每日上限（排查"界面写了但没生效"用）。"""
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return (data.get("proactive") or {}).get("global_daily_limit")
    except Exception as exc:
        return "错误：%s" % exc


def official_values(mock) -> Dict[str, Any]:
    """本次自检要写进配置的官方机器人凭据（全部指向 mock 开放平台）。"""
    from tests import mock_servers

    return {
        "app_id": mock_servers.OFFICIAL_APP_ID,
        "app_secret": mock_servers.OFFICIAL_APP_SECRET,
        "api_domain": mock.base_url,
        "token_url": "%s/app/getAppAccessToken" % mock.base_url,
        "sandbox": False,
        "target_openid": TARGET_OPENID,
        "group_openid": "",
        "allow_all_users": True,
        "allowed_users": [],
        "allowed_groups": [],
        "markdown": False,
        "max_reply_segments": 3,
        "reply_segment_max_len": 200,
    }


def prepare_config(config_path: Path, mock, api_port: int, onboarding_done: bool = True) -> None:
    """把公共 helper 写出的配置补齐成 GUI 自检需要的形态。

    自检只关心界面行为，因此这里把所有依赖显式写死，避免和公共 helper 的默认值耦合：

    * LLM 指向 mock（自检要拉模型列表、要测连接）；
    * QQ 只有「官方机器人」一种方式：写入 mock 开放平台的 AppID / AppSecret / 地址；
    * 清掉历史遗留键（旧版第三方协议的 NapCat / OneBot 字段）。
    """
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    app = data.setdefault("app", {})
    app["start_bot_on_launch"] = False
    app["onboarding_done"] = bool(onboarding_done)
    for key in LEGACY_APP_KEYS:
        app.pop(key, None)

    data.setdefault("api", {}).update({"host": "127.0.0.1", "port": int(api_port)})

    llm = data.setdefault("llm", {})
    llm.update(
        {
            "base_url": "%s/v1" % mock.base_url,
            "api_key": "mock-key",
            "model": "mock-model",
            "max_tokens": 128,
            "temperature": 0.5,
            "timeout": 20,
            "max_retries": 1,
            "fallback_messages": ["兜底话术"],
        }
    )

    qq = data.setdefault("qq", {})
    official = dict(qq.get("official") or {})
    official.update(official_values(mock))
    qq["official"] = official
    qq["name"] = "官方机器人"
    qq["enabled"] = True
    qq["user_nickname"] = "小可爱"
    qq["reply_enabled"] = True
    qq["group_reply_enabled"] = False
    for key in LEGACY_QQ_KEYS:
        qq.pop(key, None)

    proactive = data.setdefault("proactive", {})
    proactive.update(
        {
            "enabled": True,
            "scheduled_enabled": False,
            "scheduled_times": ["09:00", "21:00"],
            "idle_enabled": False,
            "random_enabled": False,
            "min_interval_minutes": 0,
            "probability": 1.0,
            "global_daily_limit": 10,
            "per_character_daily_limit": 3,
            "active_hours": {"enabled": False, "start": "00:00", "end": "23:59"},
            "dnd_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
            "max_message_chars": 60,
            "context_messages": 6,
        }
    )
    # 内置角色由自检自己导入，避免启动时自动导入影响“角色卡数量”的断言
    data.setdefault("characters", {})["import_builtin"] = False
    data.pop("napcat", None)
    data.pop("onebot", None)

    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def truncation_report(page) -> List[str]:
    """找出「显示不全」的文本：不换行且提示宽度大于实际宽度的标签。

    用户反馈过窗口太小导致文字排版异常，这里用几何关系把这类问题自动抓出来。
    """
    from PySide6.QtWidgets import QLabel

    offenders: List[str] = []
    for label in page.findChildren(QLabel):
        try:
            if not label.isVisible() or not label.text().strip():
                continue
            if label.wordWrap():
                continue  # 会自动换行，不存在被裁掉的问题
            needed = label.sizeHint().width()
            if needed > label.width() + 2:
                offenders.append(
                    "%s(%s)：需要 %dpx，只有 %dpx"
                    % (label.objectName() or "label", label.text()[:28], needed, label.width())
                )
        except RuntimeError:  # pragma: no cover - 控件已销毁
            continue
    return offenders


def main() -> int:
    from tests import card_factory, mock_servers, smoke_test

    # 输出重定向到文件时 stdout 是块缓冲；Qt 析构偶发 abort（0xC0000409）会直接
    # 杀进程，缓冲里的摘要就丢了——行缓冲保证「N 项通过」随时可见
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    checker = Checker()
    print("BaiAi-Tavern GUI 集成自检开始（Python %s）" % sys.version.split()[0])

    # 动态端口，并让配置模板指向本次端口
    mock_port = free_port()
    api_port = free_port()
    smoke_test.MOCK_URL = "http://127.0.0.1:%d" % mock_port
    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port

    mock = mock_servers.MockProcess(port=mock_port).start()
    mock.reset(reply_text=REPLY_TEXT, proactive_text=PROACTIVE_TEXT)

    # resolve()：Windows CI 的 %TEMP% 是 8.3 短名（C:\Users\RUNNER~1\...），
    # 程序的 data_dir() 会展开成长名，两边需统一后再比较
    data_dir = Path(tempfile.mkdtemp(prefix="tavern-gui-")).resolve()
    config_path = smoke_test.write_bot_config(data_dir)
    prepare_config(config_path, mock, api_port)

    os.environ["QQAI_DATA_DIR"] = str(data_dir)
    os.environ["QQAI_HOME"] = str(ROOT)

    cards_dir = data_dir / "characters"
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_factory.write_png_card(cards_dir / "深夜角色.png", "深夜角色")
    card_factory.write_json_card(cards_dir / "元气角色.json", "元气角色")
    card_factory.write_yaml_card(cards_dir / "吐槽角色.yaml", "吐槽角色")

    env = os.environ.copy()
    env.update({"QQAI_DATA_DIR": str(data_dir), "QQAI_HOME": str(ROOT), "PYTHONPATH": str(ROOT)})
    bot = subprocess.Popen(
        [sys.executable, "-m", "bot.main", "--no-console"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    bot_output = smoke_test.drain(bot)

    import httpx

    from PySide6.QtWidgets import QApplication

    from app.config_store import gui_config
    from app.context import AppContext
    from app.main_window import MainWindow
    from app.theme import apply_theme

    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=20.0)
    app = QApplication(sys.argv[:1])
    context = None
    window = None
    bot_stopped = False
    unhandled: List[str] = []
    dialogs: List[str] = []
    install_excepthook(unhandled)
    patch_message_boxes(dialogs)

    try:
        started = False
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if client.get("/api/health", timeout=5).status_code == 200:
                    started = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        checker.check("Bot 进程已启动", started)
        if not started:
            print("\n".join(bot_output[-40:]))
            return 1

        client.post("/api/characters/scan", timeout=60)

        # ------------------------------------------------------------ GUI
        config = gui_config()
        apply_theme(app, "dark")
        context = AppContext(config)
        window = MainWindow(context)
        window.show()
        pump(app, 0.5)

        checker.check("主窗口构建成功", window.isVisible())
        checker.check("侧边栏包含 8 个页面", window.nav.count() == 8, str(window.nav.count()))

        def page_by_key(key: str):
            """按导航关键字取页面（页面顺序变化时自检不用跟着改）。"""
            from app.main_window import NAV_KEYS

            return window.pages[NAV_KEYS.index(key)]

        titles = [page.page_title for page in window.pages]
        checker.check(
            "八个页面齐备（含机器人管理与模型路由）",
            titles
            == ["仪表盘", "机器人", "角色管理", "模型路由", "主动消息", "对话查看", "系统设置", "日志"],
            str(titles),
        )
        # 页面宽度守卫：任何可滚动页面的内容都不许比视口宽。
        # 历史上 TTS 百炼提示标签（单行 1474px）把模型路由页撑到 2553px，
        # 右侧控件被裁、页面"突然左移偏移、显示不全"——此检查防止复发。
        # 注意：只切页不主动 refresh（ensure_loaded 会把未配置槽位的默认预设
        # 写进界面，污染后面的断言；宽度检查在后续各页真实 refresh 时自然生效）
        from PySide6.QtWidgets import QScrollArea as _QScrollArea

        _overflow_pages = []
        for _page in window.pages:
            window.show_page(window.pages.index(_page))
            pump(app, 0.8)
            for _scroll in _page.findChildren(_QScrollArea):
                _widget = _scroll.widget()
                if _widget is not None and _widget.width() > _scroll.viewport().width() + 2:
                    _overflow_pages.append(
                        "%s：内容 %d > 视口 %d"
                        % (_page.page_title, _widget.width(), _scroll.viewport().width())
                    )
        window.show_page_by_key("dashboard")
        pump(app, 0.3)
        checker.check(
            "所有页面内容宽度都不超出视口（防止页面偏移/显示不全）",
            not _overflow_pages,
            "；".join(_overflow_pages),
        )
        checker.check(
            "无托盘环境下给出提示且不崩溃",
            window.tray is not None,
        )
        checker.check(
            "侧边栏导航项文字正确（顺序固定）",
            [window.nav.item(i).text() for i in range(window.nav.count())] == titles,
            str([window.nav.item(i).text() for i in range(window.nav.count())]),
        )

        # ------------------------------------------------- 窗口尺寸与控件标志
        checker.check(
            "默认窗口足够大（不会因为窗口太小挤坏排版）",
            window.width() >= 1160 and window.height() >= 760,
            "%dx%d" % (window.width(), window.height()),
        )
        from app.uikit import asset_paths

        assets = asset_paths()
        checker.check(
            "样式表箭头/对勾图标已生成（按钮不再是一坨灰色方块）",
            all(Path(path).exists() for path in assets.values())
            and {"arrow_up", "arrow_down", "chevron", "check"} <= set(assets),
            str(sorted(assets)),
        )
        qss = app.styleSheet()
        checker.check("样式表占位符已全部替换", "@UI:" not in qss)
        checker.check(
            "SpinBox 的上下箭头引用了真实图片",
            "QSpinBox::up-button" in qss and "arrow_up.png" in qss and "arrow_down.png" in qss,
            qss[qss.find("QSpinBox::up-button") : qss.find("QSpinBox::up-button") + 120],
        )
        checker.check(
            "勾选框与下拉框也带图标",
            "check.png" in qss
            and "@UI:chevron@" not in qss
            and "chevron.png" in qss
            and "dot.png" in qss,
        )

        # 按钮/导航图标：全部用 QPainter 画出来，不依赖字体里的符号（▶ ■ ⟳ ✉ 📂 可能缺字形）
        from app.uikit import icon as ui_icon

        checker.check(
            "导航项使用绘制的图标（不依赖 emoji 字形）",
            all(not window.nav.item(i).icon().isNull() for i in range(window.nav.count())),
            str([window.nav.item(i).text() for i in range(window.nav.count())]),
        )
        icon_buttons = {
            "dashboard": ("btn_start", "btn_stop", "btn_restart", "btn_trigger", "btn_reload", "btn_open_data"),
            "proactive": ("btn_save", "btn_trigger", "btn_reload"),
            "characters": ("btn_create", "btn_import", "btn_scan", "btn_refresh", "btn_open_dir", "btn_bots"),
            "bots": ("btn_add", "btn_remove", "btn_save", "btn_refresh"),
            "settings": ("btn_save", "btn_onboarding", "btn_reload", "btn_open_config"),
        }
        missing = []
        for key, names in icon_buttons.items():
            page = page_by_key(key)
            for name in names:
                button = getattr(page, name, None)
                if button is None or button.icon().isNull():
                    missing.append("%s.%s" % (key, name))
        checker.check("主要操作按钮都带图标（不再是一坨没有标志的灰块）", not missing, "、".join(missing))

        symbol_chars = "▶■⟳⟲↻✉📂📊🤖🎭⏰💬⚙️📜"
        symbol_left = []
        for key, names in icon_buttons.items():
            page = page_by_key(key)
            for name in names:
                button = getattr(page, name, None)
                if button is not None and any(ch in button.text() for ch in symbol_chars):
                    symbol_left.append("%s.%s=%s" % (key, name, button.text()))
        checker.check("按钮文字里不再依赖符号字形", not symbol_left, "、".join(symbol_left))
        checker.check(
            "图标绘制接口可用（各按钮图标名都能画出来）",
            all(not ui_icon(name).isNull() for name in ("play", "send", "arrow_up", "robot", "gear")),
        )

        # --------------------------------------------------- 关于（左下角按钮）
        about_button = getattr(window, "btn_about", None)
        checker.check("左下角有「关于」按钮", about_button is not None and not about_button.icon().isNull())
        if about_button is not None:
            checker.check(
                "「关于」按钮位置在左下角（在导航列表下方）",
                about_button.y() > window.nav.y() + window.nav.height() - 8,
                "按钮 y=%d，导航底部=%d" % (about_button.y(), window.nav.y() + window.nav.height()),
            )
            about_button.click()
            pump(app, 0.3)
            from app.about import OPEN_SOURCE_PROJECTS, AboutDialog

            dialog = getattr(window, "_about_dialog", None)
            checker.check("点击「关于」弹出关于窗口", isinstance(dialog, AboutDialog) and dialog.isVisible())
            if isinstance(dialog, AboutDialog):
                body = " ".join(
                    child.text() for child in dialog.findChildren(QLabel) if hasattr(child, "text")
                )
                checker.check(
                    "关于窗口显示名称 / 版本 V0.2.1 / 作者 baiai.org",
                    "BaiAi-Tavern" in body and "V0.2.1" in body and "baiai.org" in body,
                    body[:200],
                )
                checker.check(
                    "开源项目按 1. 2. 3. 编号排列",
                    "1. Python" in body and "2. PySide6" in body and "3. FastAPI" in body,
                    body[:200],
                )
                names = " ".join(name for name, _usage, _url in OPEN_SOURCE_PROJECTS)
                checker.check(
                    "开源清单里不再有第三方 QQ 协议相关项目（NoneBot / NapCat 已移除）",
                    "NoneBot" not in names and "NapCat" not in names and "OneBot" not in names,
                    names,
                )
                link_count = sum(1 for child in dialog.findChildren(QLabel) if "href=" in child.text())
                checker.check(
                    "开源项目带主页链接（可点击打开）",
                    link_count >= len(OPEN_SOURCE_PROJECTS),
                    "%d 个链接（清单共 %d 项）" % (link_count, len(OPEN_SOURCE_PROJECTS)),
                )
                dialog.btn_copy.click()
                pump(app, 0.2)
                checker.check(
                    "关于窗口可以复制开源清单",
                    "1. Python" in QGuiApplication.clipboard().text(),
                    QGuiApplication.clipboard().text()[:80],
                )
                dialog.close()
                pump(app, 0.2)

        # --------------------------------------------------- 安装与更新（左下角按钮）
        from app import updater as up
        from app.config_store import save_config as _save_cfg
        from app.lifecycle import InstallUpdateDialog, UpdateNoticeDialog

        # 主窗口构建 6 秒后会触发一次启动更新检查（singleShot）；
        # 先把时间泵过这个点，避免它落在下面的断言窗口里干扰状态
        pump(app, 7.5)

        fake_release_info = {
            "ok": True,
            "release": {"tag_name": "v0.3", "body": "- 新增更新系统\n- 一些修复"},
            "latest_tag": "v0.3",
            "latest_display": "V0.3",
            "current_display": "V0.2.1",
            "newer": True,
            "asset": {"name": "BaiAi-Tavern-V0.3.exe", "browser_download_url": "http://127.0.0.1:1/x.exe"},
            "sums_asset": None,
            "release_url": "http://127.0.0.1:1",
        }
        update_button = getattr(window, "btn_install_update", None)
        checker.check("左下角有「安装与更新」按钮（带图标）", update_button is not None and not update_button.icon().isNull())
        original_check_latest = up.check_latest
        up.check_latest = lambda current_version, api_base=None, timeout=None: dict(fake_release_info)
        try:
            if update_button is not None:
                checker.check(
                    "「安装与更新」位于左下角（与「关于」同一行，在导航下方）",
                    update_button.y() > window.nav.y() + window.nav.height() - 8,
                    "按钮 y=%d" % update_button.y(),
                )
                update_button.click()
                wait_until(app, lambda: window._install_update_dialog is not None, timeout=10)
                dialog = window._install_update_dialog
                checker.check("点击「安装与更新」打开一体窗口", isinstance(dialog, InstallUpdateDialog) and dialog is not None and dialog.isVisible())
                if isinstance(dialog, InstallUpdateDialog):
                    body = " ".join(child.text() for child in dialog.findChildren(QLabel) if hasattr(child, "text"))
                    checker.check(
                        "一体窗口含 更新 / 安装 / 卸载 / 关于 四个区块",
                        "更新" in body and "安装" in body and "卸载" in body and "关于" in body,
                        body[:160],
                    )
                    checker.check(
                        "一体窗口带 检查/更新/打开Releases/卸载 控件",
                        dialog.btn_check is not None
                        and dialog.btn_update is not None
                        and dialog.btn_releases is not None
                        and dialog.edit_target_dir is not None
                        and dialog.btn_uninstall is not None
                        and dialog.btn_uninstall_wipe is not None,
                        "",
                    )
                    checked = wait_until(app, lambda: dialog.btn_update.isEnabled(), timeout=15)
                    checker.check(
                        "更新检查（mock 发现新版）后「立即更新」可用",
                        checked and dialog.btn_update.text() == "立即更新",
                        str(dialog.lbl_update_state.text())[:160],
                    )
                    checker.check(
                        "新版状态行与 Release 说明可见",
                        "V0.3" in str(dialog.lbl_update_state.text()) and "更新系统" in str(dialog.lbl_release_note.text()),
                        str(dialog.lbl_update_state.text())[:160],
                    )
                    _save_cfg(context.config, {"app": {"update_skipped_version": "v0.3"}})
                    dialog._sync_update_controls()
                    checker.check(
                        "跳过某版本时显示跳过状态与「恢复提示」",
                        dialog.lbl_skip.isVisible() and dialog.btn_unskip.isVisible() and "V0.3" in str(dialog.lbl_skip.text()),
                        str(dialog.lbl_skip.text())[:100],
                    )
                    dialog.btn_unskip.click()
                    pump(app, 0.2)
                    checker.check(
                        "点「恢复提示」清掉跳过版本",
                        str(context.config.get("app.update_skipped_version", "") or "") == "",
                        str(context.config.get("app.update_skipped_version", "")),
                    )
                    dialog.chk_auto.setChecked(False)
                    pump(app, 0.2)
                    auto_off = not bool(context.config.get("app.update_check_enabled", True))
                    dialog.chk_auto.setChecked(True)
                    pump(app, 0.2)
                    checker.check(
                        "取消「启动时自动检查更新」即保存「不再提示」",
                        auto_off and bool(context.config.get("app.update_check_enabled", True)),
                        "",
                    )
                    dialog.close()
                    pump(app, 0.3)

            # 启动检查：跳过该版本不弹 / 未跳过弹提醒 / 不再提示整体跳过
            if window._update_notice is not None:  # 清掉启动自动检查可能留下的提醒
                try:
                    window._update_notice.close()
                except Exception:
                    pass
                window._update_notice = None
                pump(app, 0.2)
            _save_cfg(context.config, {"app": {"update_skipped_version": "v0.3", "update_check_enabled": True}})
            window._on_startup_update_check(dict(fake_release_info))
            pump(app, 0.3)
            checker.check("最新版本被「跳过」时启动不弹更新提醒", window._update_notice is None, "")

            _save_cfg(context.config, {"app": {"update_skipped_version": ""}})
            window._on_startup_update_check(dict(fake_release_info))
            pump(app, 0.3)
            notice = window._update_notice
            checker.check("启动发现新版本时弹出更新提醒", isinstance(notice, UpdateNoticeDialog) and notice.isVisible())
            if isinstance(notice, UpdateNoticeDialog):
                checker.check(
                    "提醒给四个出口：立即更新 / 跳过此版本 / 不再提示 / 稍后再说",
                    notice.btn_update is not None
                    and notice.btn_skip is not None
                    and notice.btn_never is not None
                    and notice.btn_later is not None,
                    "",
                )
                notice._on_clicked(notice.btn_never)
                pump(app, 0.2)
                checker.check("选「不再提示」后启动自动检查被关闭", not bool(context.config.get("app.update_check_enabled", True)), "")

                window._update_notice = None
                window.maybe_check_update()
                pump(app, 0.6)
                checker.check("「不再提示」生效：启动自动检查整体跳过（不弹提醒）", window._update_notice is None, "")
                _save_cfg(context.config, {"app": {"update_check_enabled": True}})
        finally:
            up.check_latest = original_check_latest
            _save_cfg(context.config, {"app": {"update_check_enabled": True, "update_skipped_version": ""}})

        # --------------------------------------------------------- 仪表盘
        online = wait_until(app, lambda: bool(context.last_status.get("online")), timeout=30)
        checker.check("界面轮询到 Bot 在线状态", online)
        gateway = wait_until(
            app,
            lambda: bool((context.last_status.get("qq") or {}).get("connected")),
            timeout=60,
        )
        checker.check(
            "Bot 已连上官方网关（mock 开放平台）",
            gateway,
            json.dumps(context.last_status.get("qq") or {}, ensure_ascii=False)[:200],
        )
        dashboard = page_by_key("dashboard")
        checker.check(
            "仪表盘显示运行状态",
            "运行中" in dashboard.card_bot.value_label.text(),
            dashboard.card_bot.value_label.text(),
        )
        checker.check(
            "仪表盘显示 QQ 官方机器人已连接",
            "已连接" in dashboard.card_qq.value_label.text(),
            dashboard.card_qq.value_label.text(),
        )
        identity_sub = dashboard.card_identity.value_label.text()
        checker.check(
            "仪表盘显示登录身份（官方机器人 AppID）",
            bool(identity_sub.strip()),
            identity_sub,
        )
        checker.check(
            "仪表盘不再有 NapCat 卡片与「重启 NapCat」按钮",
            not hasattr(dashboard, "card_napcat") and not hasattr(dashboard, "btn_restart_napcat"),
        )
        checker.check(
            "状态栏显示官方机器人身份（不再显示 QQ 号）",
            "官方机器人" in window.qq_text.text(),
            window.qq_text.text(),
        )
        checker.check("状态栏显示 Bot 运行状态", "运行中" in window.status_text.text(), window.status_text.text())
        checker.check(
            "侧边栏底部显示今日统计与主动消息目标",
            "今日主动消息" in window.sidebar_status.text() and "openid" in window.sidebar_status.text(),
            window.sidebar_status.text(),
        )

        # ------------------------------------------------------- 角色管理
        window.show_page_by_key("characters")
        characters_page = page_by_key("characters")
        characters_page.refresh()
        rendered = wait_until(app, lambda: len(character_cards(characters_page)) == 3, timeout=30)
        checker.check("角色管理页渲染出 3 张角色卡", rendered, str(len(character_cards(characters_page))))
        cards = character_cards(characters_page)
        checker.check(
            "角色卡显示名称",
            any("深夜角色" in item.name_label.text() for item in cards),
            str([item.name_label.text() for item in cards]),
        )
        checker.check(
            "角色管理页提供「新建角色」与「导入内置角色」",
            characters_page.btn_create.isEnabled() and characters_page.btn_builtin.isEnabled(),
        )
        # 不用角色卡，直接用界面填写的内容创建角色
        created = client.post(
            "/api/characters",
            json={
                "name": "界面新建角色",
                "description": "{{char}} 是界面新建的测试角色",
                "personality": "随和",
                "first_mes": "你好",
            },
            timeout=30,
        )
        checker.check("可以不用角色卡新建角色", created.status_code == 200, created.text[:160])
        characters_page.refresh()
        checker.check(
            "新建的角色出现在列表里",
            wait_until(
                app,
                lambda: any("界面新建角色" in item.name_label.text() for item in character_cards(characters_page)),
                timeout=25,
            ),
            str([item.name_label.text() for item in character_cards(characters_page)]),
        )
        # 内置默认角色
        before = len(character_cards(characters_page))
        characters_page._import_builtin()
        checker.check(
            "一键导入内置默认角色",
            wait_until(app, lambda: len(character_cards(characters_page)) >= before + 3, timeout=40),
            "%d -> %d" % (before, len(character_cards(characters_page))),
        )
        checker.check(
            "内置角色名可见",
            any(
                name in item.name_label.text()
                for item in character_cards(characters_page)
                for name in ("小栖", "阿元", "苏苏")
            ),
            str([item.name_label.text() for item in character_cards(characters_page)]),
        )
        # 新建对话框带示例模板（用户不会写人设时的兜底）
        from app.pages.characters import CharacterEditDialog, CharacterVoiceDialog

        dialog = CharacterEditDialog({}, characters_page, creating=True)
        dialog._fill_template()
        template_values = dialog.values()
        checker.check(
            "新建角色对话框可一键填入示例",
            bool(template_values.get("name")) and bool(template_values.get("system_prompt")),
            str(template_values.get("name")),
        )
        # 编辑对话框表单包在滚动区里（字段多时底部不再被裁掉）
        from PySide6.QtWidgets import QScrollArea as _QScrollArea

        checker.check(
            "角色编辑对话框带滚动条（长表单可滚动）",
            dialog.findChild(_QScrollArea) is not None,
        )
        # 占位符说明：{{char}} / {{user}} 是角色卡标准写法，避免被当成“空缺”
        from PySide6.QtWidgets import QLabel as _QLabel

        hint_texts = [item.text() for item in dialog.findChildren(_QLabel)]
        checker.check(
            "编辑对话框说明 {{char}}/{{user}} 占位符",
            any("{{char}}" in text and "{{user}}" in text for text in hint_texts),
        )
        # Chub 卡片「补充设定」是整页 HTML，要说明它不影响对话（用户实测当成乱码）
        html_dialog = CharacterEditDialog(
            {"name": "Chub 角色", "creator_notes": '<div style="max-width: 100%;">' + "备注内容 " * 100},
            characters_page,
        )
        html_hint_texts = [item.text() for item in html_dialog.findChildren(_QLabel)]
        checker.check(
            "编辑对话框说明 HTML 版补充设定不影响对话",
            any("HTML" in text and "不影响对话" in text for text in html_hint_texts),
        )
        # 音色设置独立对话框：音色下拉 + 试听 + 语速/音调/音量调节
        voice_dialog = CharacterVoiceDialog(
            {"id": "x1", "name": "音色测试", "tts_voice": "", "tts_rate": "+20%"},
            characters_page,
        )
        checker.check(
            "角色音色对话框含音色下拉 / 试听 / 音色调节",
            voice_dialog.combo_voice is not None
            and voice_dialog.combo_voice.count() >= 14
            and voice_dialog.btn_preview_voice is not None
            and voice_dialog.spin_rate is not None
            and voice_dialog.spin_pitch is not None
            and voice_dialog.spin_volume is not None
            and voice_dialog.dspin_speed is not None,
            "音色 %d 个" % voice_dialog.combo_voice.count(),
        )
        checker.check(
            "音色对话框回显角色已保存的语速调节",
            voice_dialog.spin_rate.value() == 20,
            str(voice_dialog.spin_rate.value()),
        )
        voice_values = voice_dialog.values()
        voice_dialog.deleteLater()
        checker.check(
            "音色对话框输出 tts_voice 与角色级调节值（0 值留空）",
            voice_values.get("tts_rate") == "+20%"
            and voice_values.get("tts_pitch") == ""
            and voice_values.get("tts_speed") == "",
            str(voice_values),
        )
        dialog.deleteLater()
        # 角色卡上有「音色」按钮与可点击头像
        cards = character_cards(characters_page)
        checker.check(
            "角色卡带「音色」按钮与可点击头像",
            all(card.voice_button is not None for card in cards)
            and all(card.avatar.cursor().shape() == Qt.CursorShape.PointingHandCursor for card in cards),
            str([getattr(c, "character_id", "") for c in cards]),
        )

        total_enabled = len(client.get("/api/characters", params={"enabled_only": True}).json())
        cards = character_cards(characters_page)
        cards[0].enable_box.setChecked(False)
        checker.check(
            "界面关闭角色开关后 Bot 侧同步禁用",
            wait_until(
                app,
                lambda: len(client.get("/api/characters", params={"enabled_only": True}).json())
                == total_enabled - 1,
                timeout=20,
            ),
            str(total_enabled),
        )
        wait_until(app, lambda: len(character_cards(characters_page)) > 0, timeout=20)
        character_cards(characters_page)[0].enable_box.setChecked(True)
        checker.check(
            "界面重新启用角色",
            wait_until(
                app,
                lambda: len(client.get("/api/characters", params={"enabled_only": True}).json())
                == total_enabled,
                timeout=20,
            ),
        )

        # ------------------------------------------------------- 主动消息
        window.show_page_by_key("proactive")
        proactive_page = page_by_key("proactive")
        proactive_page.refresh()
        pump(app, 0.5)
        checker.check(
            "主动消息页载入频率配置",
            proactive_page.spin_global.value() == 10,
            str(proactive_page.spin_global.value()),
        )
        checker.check(
            "主动消息页载入定时时间列表",
            proactive_page.times_edit.times() == ["09:00", "21:00"],
            str(proactive_page.times_edit.times()),
        )
        proactive_page.spin_global.setValue(8)
        proactive_page._save()

        def _limit_in_bot() -> Any:
            try:
                return int(client.get("/api/config").json()["config"]["proactive"]["global_daily_limit"])
            except Exception as exc:
                return "错误：%s" % exc

        saved = wait_until(app, lambda: _limit_in_bot() == 8, timeout=25)
        detail = "Bot 内值=%s，页面收集值=%s，文件值=%s" % (
            _limit_in_bot(),
            proactive_page._collect().get("global_daily_limit"),
            _limit_in_file(config_path),
        )
        checker.check("主动消息页保存配置生效", saved, detail)

        # ------------------------------------------------------- 对话查看
        before = len(mock.official_sent())
        mock.emit_c2c("在忙吗？")
        checker.check(
            "界面运行期间官方机器人单聊回复正常",
            wait_until(app, lambda: len(mock.official_sent()) > before, timeout=45),
            json.dumps(mock.official_sent()[-1:], ensure_ascii=False)[:200],
        )
        checker.check(
            "回复内容来自 mock LLM",
            REPLY_TEXT in str((mock.official_sent() or [{}])[-1].get("content") or ""),
            str((mock.official_sent() or [{}])[-1].get("content")),
        )

        window.show_page_by_key("conversations")
        conversations_page = page_by_key("conversations")

        def _row0_count() -> int:
            table = conversations_page.conversation_table
            try:
                return int(table.item(0, 1).text())
            except Exception:
                return -1

        # 宽度守卫巡检可能已在此页触发过一次旧加载（当时库里还没有消息），
        # 且「刷新」任务按 key 去重——旧任务还在跑时新的刷新会被跳过。
        # 因此循环刷新，直到表格第 0 行变成刚才的「在忙吗？」会话（2 条消息）
        listed = False
        for _attempt in range(3):
            conversations_page.refresh()
            listed = wait_until(app, lambda: _row0_count() >= 2, timeout=10)
            if listed:
                break
        checker.check(
            "对话页列出会话（第 0 行是刚才的会话）",
            listed,
            "行数=%d 第0行消息数=%d"
            % (conversations_page.conversation_table.rowCount(), _row0_count()),
        )
        # 先清空选择再选第 0 行：刷新重新填充表格会清掉选中，产品侧会按
        # 当前角色跟回；测试这里显式选一次，保证消息面板加载第 0 行
        conversations_page.conversation_table.clearSelection()
        conversations_page.conversation_table.selectRow(0)
        checker.check(
            "对话页加载消息内容",
            wait_until(app, lambda: conversations_page.message_table.rowCount() >= 2, timeout=25),
            "第0行=%s 当前角色=%s 消息行数=%d"
            % (
                str(conversations_page.conversation_table.item(0, 0).text())
                if conversations_page.conversation_table.rowCount()
                else "-",
                conversations_page.current_character_name,
                conversations_page.message_table.rowCount(),
            ),
        )

        # ------------------------------------------------------- 模型路由
        window.show_page_by_key("models")
        models_page = page_by_key("models")
        models_page.refresh()
        pump(app, 0.5)
        checker.check(
            "模型路由页有三个槽位表单（主模型已整合到系统设置，听语音用平台参考转写）",
            all(hasattr(models_page, "form_%s" % s) for s in ("vision", "image", "tts"))
            and not hasattr(models_page, "form_chat")
            and not hasattr(models_page, "form_asr"),
        )
        vision_form = models_page.form_vision
        checker.check(
            "槽位表单带服务商预设（同 LLM 表单）",
            vision_form.combo_preset is not None and vision_form.combo_preset.count() >= 4,
            str(vision_form.combo_preset.count() if vision_form.combo_preset else None),
        )
        # 预设 → 自动填 Base URL + 候选模型
        vision_form.combo_preset.setCurrentIndex(1)
        vision_form.combo_preset.setCurrentIndex(0)
        pump(app, 0.2)
        checker.check(
            "选预设后自动填好 Base URL 与候选模型",
            vision_form.edit_base.text() == "https://dashscope.aliyuncs.com/compatible-mode/v1"
            and vision_form.edit_model.currentText() == "qwen-vl-max",
            "%s | %s" % (vision_form.edit_base.text(), vision_form.edit_model.currentText()),
        )
        # 获取模型列表（mock /v1/models）+ 点选
        vision_form.edit_base.setText("%s/v1" % smoke_test.MOCK_URL)
        vision_form.btn_fetch.click()
        checker.check(
            "槽位表单可获取上游模型列表",
            wait_until(app, lambda: len(vision_form.all_models) == 5, timeout=30),
            str(vision_form.all_models),
        )
        vision_form.list_models.itemClicked.emit(vision_form.list_models.item(0))
        pump(app, 0.2)
        checker.check(
            "模型列表点选即填入模型输入框",
            vision_form.edit_model.currentText() == vision_form.all_models[0],
            vision_form.edit_model.currentText(),
        )
        # 测试线路按表单当前值（未保存）生效；视觉测试线路用内置红色测试图，
        # mock 需答「红色」才通过（验证真的把图发给了模型）
        mock.reset(vision_text="红色")
        vision_form.edit_key.setText("mock-key")
        vision_form.edit_model.setCurrentText("mock-model")
        vision_form.test()
        checker.check(
            "测试线路按表单当前值发起（未点保存也生效）",
            wait_until(
                app,
                lambda: bool(vision_form.last_result) and vision_form.last_result.get("ok") is True,
                timeout=60,
            ),
            str(vision_form.last_result),
        )
        mock.reset(vision_text="我看你发的图了（mock 视觉回复）。")
        checker.check(
            "视觉测试线路校验内置红色测试图（mock 答「红色」）",
            "红色" in str((vision_form.last_result or {}).get("message") or ""),
            str(vision_form.last_result),
        )
        # 测试期间/结束后按钮保持可用（焦点不跳到下方表单，防"窗口跳段"回归）
        checker.check(
            "测试线路完成后按钮保持可用（焦点不串段）",
            vision_form.btn_test.isEnabled(),
            "",
        )
        # 生图：双 Gemini 预设（OpenAI 兼容层 + 原生接口），原生预设自动切换引擎
        image_form = models_page.form_image
        checker.check(
            "生图槽位带引擎下拉（OpenAI 兼容 / Gemini 原生）",
            image_form.combo_engine is not None and image_form.combo_engine.count() == 2,
            str(image_form.combo_engine.count() if image_form.combo_engine else None),
        )
        compat_idx = image_form.combo_preset.findText("Google Gemini 生图（OpenAI 兼容层）")
        image_form.combo_preset.setCurrentIndex(1)
        image_form.combo_preset.setCurrentIndex(compat_idx)
        pump(app, 0.2)
        checker.check(
            "Gemini 兼容层预设填 Base URL 与文档点名模型",
            image_form.edit_base.text().endswith("/v1beta/openai")
            and image_form.edit_model.currentText() == "gemini-2.5-flash-image",
            "%s | %s" % (image_form.edit_base.text(), image_form.edit_model.currentText()),
        )
        native_idx = image_form.combo_preset.findText("Google Gemini 生图（原生接口，支持全部新模型）")
        image_form.combo_preset.setCurrentIndex(compat_idx)
        image_form.combo_preset.setCurrentIndex(native_idx)
        pump(app, 0.2)
        checker.check(
            "Gemini 原生预设自动切引擎 + 填 v1beta 地址 + nano banana 2 lite 模型",
            image_form.current_engine() == "gemini-native"
            and image_form.edit_base.text() == "https://generativelanguage.googleapis.com/v1beta"
            and image_form.edit_model.currentText() == "gemini-3.1-flash-lite-image",
            "%s | %s | %s"
            % (
                image_form.current_engine(),
                image_form.edit_base.text(),
                image_form.edit_model.currentText(),
            ),
        )
        # TTS：推荐引擎 dashscope 为默认，未配置时自动填好推荐组合（用户只需粘贴 Key）
        from common.providers import ENGINE_DASHSCOPE, SLOT_ENGINES, SLOT_TTS

        tts_form = models_page.form_tts
        _tts_engines = SLOT_ENGINES[SLOT_TTS]
        _i_ds = _tts_engines.index(ENGINE_DASHSCOPE)
        _i_edge = _tts_engines.index("edge-tts")
        checker.check(
            "TTS 默认推荐引擎 dashscope，自动填好推荐组合（模型/音色/地址）",
            tts_form.combo_engine is not None
            and tts_form.combo_engine.count() == 3
            and tts_form.current_engine() == ENGINE_DASHSCOPE
            and tts_form.edit_model.currentText() == "qwen-audio-3.1-tts-flash"
            and tts_form.combo_voice.currentText() == "yuxiaoyun_v3.1"
            and tts_form.edit_base.text() == "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "engine=%s model=%s voice=%s base=%s"
            % (
                tts_form.current_engine(),
                tts_form.edit_model.currentText(),
                tts_form.combo_voice.currentText(),
                tts_form.edit_base.text(),
            ),
        )
        checker.check(
            "TTS 引擎下拉含推荐标注",
            "推荐" in tts_form.combo_engine.itemText(_i_ds),
            tts_form.combo_engine.itemText(_i_ds),
        )
        # TTS 引擎：三选项（百炼 DashScope 推荐默认 / edge-tts / OpenAI 兼容）
        if tts_form.combo_engine is not None and tts_form.combo_engine.count() == 3:
            # 等初始（百炼候选）音色刷新落定，避免任务 key 占用导致新一轮被跳过
            wait_until(
                app,
                lambda: not context.runner.is_busy("media_voices_%s" % tts_form._key_tag),
                timeout=30,
            )
            tts_form.combo_engine.blockSignals(True)
            tts_form.combo_engine.setCurrentIndex(_i_ds)  # 百炼 DashScope（已是默认，强制再刷一次）
            tts_form.combo_engine.blockSignals(False)
            tts_form._on_engine_changed(_i_ds)
            got_dashscope_voices = wait_until(
                app,
                lambda: 5 < tts_form.combo_voice.count() < 200
                and "Cherry" in [tts_form.combo_voice.itemText(i) for i in range(tts_form.combo_voice.count())],
                timeout=30,
            )
            checker.check(
                "百炼引擎下音色清单为 Qwen-TTS / CosyVoice / Qwen-Audio 候选",
                got_dashscope_voices and tts_form.current_engine() == ENGINE_DASHSCOPE
                and tts_form.lbl_tune_dashscope is not None and tts_form.lbl_tune_dashscope.isVisible(),
                "音色数 %d" % tts_form.combo_voice.count(),
            )
            tts_form.combo_engine.blockSignals(True)
            tts_form.combo_engine.setCurrentIndex(_i_edge)  # 切到 edge-tts
            tts_form.combo_engine.blockSignals(False)
            tts_form._on_engine_changed(_i_edge)
            # 音色刷新任务按键去重：若切引擎时上一轮刷新还在跑，本轮会被跳过 → 等空闲后补一次
            wait_until(
                app,
                lambda: not context.runner.is_busy("media_voices_%s" % tts_form._key_tag),
                timeout=30,
            )
            if tts_form.combo_voice.count() <= 100:
                tts_form._refresh_voice_list()
            wait_until(app, lambda: tts_form.combo_voice.count() > 100, timeout=45)
            # 获取模型列表成功后，tts 槽位要联动刷新音色清单（edge-tts 时按钮置灰，先切到百炼）
            _orig_refresh = tts_form._refresh_voice_list
            _refresh_calls = {"n": 0}

            def _spy_refresh():
                _refresh_calls["n"] += 1
                _orig_refresh()

            tts_form._refresh_voice_list = _spy_refresh
            tts_form.combo_engine.blockSignals(True)
            tts_form.combo_engine.setCurrentIndex(_i_ds)
            tts_form.combo_engine.blockSignals(False)
            tts_form._on_engine_changed(_i_ds)
            # 等引擎切换引发的首次音色刷新落定，再清零计数
            wait_until(
                app,
                lambda: _refresh_calls["n"] >= 1
                and not context.runner.is_busy("media_voices_%s" % tts_form._key_tag),
                timeout=30,
            )
            pump(app, 1.0)
            _refresh_calls["n"] = 0
            tts_form.edit_base.setText("%s/v1" % smoke_test.MOCK_URL)
            tts_form.edit_key.setText("mock-key")
            tts_form.btn_fetch.click()
            _fetch_refreshed = wait_until(
                app,
                lambda: _refresh_calls["n"] >= 1 and len(tts_form.all_models) == 5,
                timeout=30,
            )
            tts_form._refresh_voice_list = _orig_refresh
            checker.check(
                "获取模型列表成功后联动刷新音色清单",
                _fetch_refreshed,
                "刷新调用 %d 次，模型 %d 个" % (_refresh_calls["n"], len(tts_form.all_models)),
            )
            tts_form.combo_engine.blockSignals(True)
            tts_form.combo_engine.setCurrentIndex(_i_edge)  # 收尾：切回 edge-tts
            tts_form.combo_engine.blockSignals(False)
            tts_form._on_engine_changed(_i_edge)
            # 音色刷新任务按键去重：若切引擎时上一轮刷新还在跑，本轮会被跳过 → 等空闲后补一次
            wait_until(
                app,
                lambda: not context.runner.is_busy("media_voices_%s" % tts_form._key_tag),
                timeout=30,
            )
            if tts_form.combo_voice.count() <= 100:
                tts_form._refresh_voice_list()
            wait_until(app, lambda: tts_form.combo_voice.count() > 100, timeout=45)
            checker.check(
                "TTS 表单带音色调节字段（语速/音调/音量/语速倍率）",
                tts_form.spin_rate is not None
                and tts_form.spin_pitch is not None
                and tts_form.spin_volume is not None
                and tts_form.dspin_speed is not None,
            )
            tts_form.spin_rate.setValue(20)
            tts_form.spin_pitch.setValue(-5)
            pump(app, 0.2)
            tts_values = tts_form.values()
            checker.check(
                "音色调节值按 edge-tts 格式输出（+N% / +NHz），0 值不输出",
                tts_values.get("rate") == "+20%"
                and tts_values.get("pitch") == "-5Hz"
                and "volume" not in tts_values
                and "speed" not in tts_values,
                str(tts_values),
            )
            tts_form.spin_rate.setValue(0)
            tts_form.spin_pitch.setValue(0)
            pump(app, 0.2)
            # 音色下拉：edge-tts 全量清单（300+，中文在前）
            checker.check(
                "音色下拉加载了 edge-tts 全量音色清单",
                wait_until(app, lambda: tts_form.combo_voice.count() > 100, timeout=45),
                "当前 %d 个" % tts_form.combo_voice.count(),
            )

        # ------------------------------------------------------- 系统设置
        window.show_page_by_key("settings")
        settings_page = page_by_key("settings")
        settings_page.refresh()
        pump(app, 0.5)
        checker.check(
            "设置页回显 LLM 配置",
            settings_page.llm_form.edit_base.text().endswith("/v1")
            and settings_page.llm_form.model() == "mock-model",
            "%s | %s" % (settings_page.llm_form.edit_base.text(), settings_page.llm_form.model()),
        )
        checker.check(
            "设置页的服务商下拉可识别当前地址",
            settings_page.llm_form.combo_preset.currentText() == "自定义 / 其他",
            settings_page.llm_form.combo_preset.currentText(),
        )
        settings_page.llm_form.btn_fetch.click()
        checker.check(
            "设置页可获取上游模型列表",
            wait_until(app, lambda: bool(settings_page.llm_form.last_models), timeout=30),
            str(settings_page.llm_form.last_models),
        )
        checker.check(
            "设置页模型列表可点选",
            wait_until(
                app,
                lambda: settings_page.llm_form.list_models.count() == 3,
                timeout=10,
            ),
            str(settings_page.llm_form.list_models.count()),
        )
        settings_page.llm_form.list_models.itemClicked.emit(settings_page.llm_form.list_models.item(0))
        checker.check(
            "设置页点选模型后当前模型随之更新",
            settings_page.llm_form.model() == "mock-model",
            settings_page.llm_form.model(),
        )

        # ------------------------------------------- 官方机器人连接设置（只有一种方式）
        qq_form = settings_page.qq_form
        checker.check(
            "设置页不再有 NapCat 分组 / 启动开关",
            not hasattr(settings_page, "napcat_panel") and not hasattr(settings_page, "chk_start_napcat"),
        )
        checker.check(
            "设置页 _collect() 只返回 llm / qq / app",
            set(settings_page._collect().keys()) == {"llm", "qq", "app"},
            str(sorted(settings_page._collect().keys())),
        )
        checker.check(
            "app 段里没有 start_napcat_on_launch",
            "start_napcat_on_launch" not in settings_page._collect()["app"],
            str(sorted(settings_page._collect()["app"].keys())),
        )
        checker.check(
            "QQ 控件只剩官方一套字段（没有连接方式下拉 / 分页）",
            not any(
                hasattr(qq_form, name)
                for name in (
                    "combo_mode",
                    "stack",
                    "set_mode",
                    "mode",
                    "in_target",
                    "in_nickname",
                    "in_self_id",
                    "in_napcat_url",
                    "in_access_token",
                    "chk_reply",
                    "chk_group",
                    "chk_typing",
                    "in_groups",
                    "spin_segments",
                    "spin_segment_len",
                    "spin_cps",
                    "dspin_typing_max",
                )
            ),
            str(sorted(k for k in vars(qq_form) if not k.startswith("__"))),
        )
        checker.check(
            "QQ 控件的 values() 只给出 official + group_reply_enabled",
            set(qq_form.values().keys()) == {"official", "group_reply_enabled"},
            str(sorted(qq_form.values().keys())),
        )
        checker.check(
            "官方字段齐全（AppID / Secret / 沙盒 / 目标 openid / 白名单 / markdown / 拆分）",
            {
                "app_id",
                "app_secret",
                "sandbox",
                "target_openid",
                "group_openid",
                "allow_all_users",
                "allowed_users",
                "allowed_groups",
                "markdown",
                "max_reply_segments",
                "reply_segment_max_len",
            }
            <= set(qq_form.values()["official"].keys()),
            str(sorted(qq_form.values()["official"].keys())),
        )
        from tests import mock_servers

        checker.check(
            "设置页回显 AppID / AppSecret / 主动消息 openid",
            qq_form.in_app_id.text() == mock_servers.OFFICIAL_APP_ID
            and qq_form.in_app_secret.text() == mock_servers.OFFICIAL_APP_SECRET
            and qq_form.in_target_openid.text() == TARGET_OPENID,
            "%s / %s / %s"
            % (qq_form.in_app_id.text(), "***" if qq_form.in_app_secret.text() else "", qq_form.in_target_openid.text()),
        )
        checker.check(
            "官方控件带「显示 AppSecret」开关（默认隐藏）",
            qq_form.in_app_secret.echoMode() == QLineEdit.EchoMode.Password,
        )
        qq_form.btn_show_secret.setChecked(True)
        checker.check(
            "点「显示」后 AppSecret 明文可见",
            qq_form.in_app_secret.echoMode() == QLineEdit.EchoMode.Normal,
        )
        qq_form.btn_show_secret.setChecked(False)
        def same_path(shown: str, expected) -> bool:
            """路径比较：两边都 resolve（展开 8.3 短名 / 符号链接）后忽略大小写比较。"""
            try:
                left = os.path.normcase(str(Path(shown).resolve()))
                right = os.path.normcase(str(Path(expected).resolve()))
            except Exception:  # 路径不存在等情况退化为纯字符串比较
                left = os.path.normcase(str(shown))
                right = os.path.normcase(str(expected))
            return right in left

        checker.check(
            "设置页显示数据目录",
            same_path(settings_page.label_data_dir.text(), data_dir),
            "标签=%s / 期望=%s" % (settings_page.label_data_dir.text(), data_dir),
        )
        checker.check(
            "设置页显示第 1 个机器人的身份与绑定情况",
            "官方机器人" in settings_page.lbl_bot_identity.text()
            and "共 1 个机器人" in settings_page.lbl_bot_identity.text(),
            settings_page.lbl_bot_identity.text(),
        )

        def _official_in_file(field: str) -> Any:
            """读 config.yaml 里 qq.official.<field>（排查“界面写了但没落盘”）。"""
            return (((yaml_load(config_path).get("qq") or {}).get("official") or {})).get(field)

        # 真实点击「测试连接」：会先保存当前填写内容，再让 Bot 去连（mock）开放平台
        qq_form.btn_test.click()
        tested = wait_until(
            app,
            lambda: bool(qq_form.last_test and qq_form.last_test.get("available")),
            timeout=60,
        )
        checker.check(
            "点「测试连接」真的连上了官方平台（mock）",
            tested,
            str(qq_form.last_test),
        )
        checker.check(
            "测试结果在界面上可见（带 ✓ 与机器人昵称）",
            "✓" in qq_form.lbl_status.text() and bool((qq_form.last_test or {}).get("nickname")),
            qq_form.lbl_status.text(),
        )

        # 「保存全部设置」：界面收集的官方字段要落到 config.yaml 并同步给 Bot
        qq_form.in_target_openid.setText("user-from-form")
        qq_form.chk_official_group.setChecked(True)
        settings_page.btn_save.click()
        checker.check(
            "「保存全部设置」把官方字段写进 config.yaml",
            wait_until(
                app,
                lambda: _official_in_file("target_openid") == "user-from-form"
                and bool(((yaml_load(config_path).get("qq") or {}).get("group_reply_enabled"))),
                timeout=40,
            ),
            json.dumps((yaml_load(config_path).get("qq") or {}), ensure_ascii=False)[:220],
        )

        def _official_in_bot(field: str) -> Any:
            try:
                data = client.get("/api/config").json().get("config") or {}
                return ((data.get("qq") or {}).get("official") or {}).get(field)
            except Exception as exc:
                return "错误：%s" % exc

        checker.check(
            "Bot 侧同步了官方字段（主动消息 openid 生效）",
            wait_until(app, lambda: _official_in_bot("target_openid") == "user-from-form", timeout=30),
            str(_official_in_bot("target_openid")),
        )

        # 把 openid 改回自检用的值，再点一次「测试连接」（它会先保存当前填写内容）
        qq_form.in_target_openid.setText(TARGET_OPENID)
        qq_form.chk_official_group.setChecked(False)
        qq_form.btn_test.click()
        checker.check(
            "「测试连接」会先保存当前填写内容（改回的 openid 落到配置里）",
            wait_until(app, lambda: _official_in_file("target_openid") == TARGET_OPENID, timeout=60),
            str(_official_in_file("target_openid")),
        )

        # 真实点击「重新连接」：重建官方网关连接。
        # 连接状态标签会被周期性的状态刷新很快覆盖（刷新成“已连接官方网关”），
        # 因此这里记录标签的每一次赋值，用它来断言按钮真的走完了重连流程。
        status_texts: List[str] = []
        original_set_status = qq_form._set_status

        def _spy_status(text: str, level: str = "muted") -> None:
            status_texts.append(str(text))
            original_set_status(text, level)

        qq_form._set_status = _spy_status  # type: ignore[assignment]
        try:
            qq_form.btn_reconnect.click()
            reconnected = wait_until(
                app,
                lambda: any("网关已重新连接" in item for item in status_texts),
                timeout=60,
            )
        finally:
            qq_form._set_status = original_set_status  # type: ignore[assignment]
        checker.check(
            "点「重新连接」重建了官方网关并给出结果",
            reconnected,
            " | ".join(status_texts[-4:]),
        )
        checker.check(
            "重连后界面状态稳定在「已连接官方网关」",
            wait_until(app, lambda: "已连接官方网关" in qq_form.lbl_status.text(), timeout=30),
            qq_form.lbl_status.text(),
        )

        # ------------------------------------------------------------ 日志
        window.show_page_by_key("logs")
        logs_page = page_by_key("logs")
        logs_page.refresh()
        pump(app, 1.2)
        content = logs_page.view.toPlainText()
        checker.check("日志页读取到 Bot 日志", ("回复" in content or "主动消息" in content), content[-150:])
        logs_page.combo_source.setCurrentIndex(1)
        pump(app, 0.6)
        checker.check("日志页可切换到界面日志", "gui.log" in logs_page.path_label.text(), logs_page.path_label.text())

        # --------------------------------------------- 机器人管理（多机器人）
        window.show_page_by_key("bots")
        bots_page = page_by_key("bots")
        bots_page.refresh()
        listed = wait_until(app, lambda: bots_page.list.count() == 1, timeout=40)
        checker.check("机器人页面列出当前机器人", listed, str(bots_page.list.count()))
        checker.check(
            "机器人页面显示绑定角色下拉（可指定“哪个机器人用哪个角色”）",
            bots_page.combo_character.count() >= 1,
            str(bots_page.combo_character.count()),
        )
        checker.check(
            "机器人的连接设置也只有官方一套字段",
            not hasattr(bots_page, "combo_mode")
            and bots_page.qq_form is not None
            and hasattr(bots_page.qq_form, "in_app_id"),
        )

        def bot_names() -> List[str]:
            return [
                bots_page.list.item(index).text().replace("\n", " ")
                for index in range(bots_page.list.count())
            ]

        bots_page.btn_add.click()
        added = wait_until(app, lambda: bots_page.list.count() == 2, timeout=60)
        checker.check("界面可以新增机器人", added, str(bot_names()))
        if added:
            bots_page.list.setCurrentRow(1)
            pump(app, 0.3)
            checker.check(
                "第 2 个机器人没有继承第 1 个的凭据（各自的 AppID 独立）",
                bots_page.qq_form is not None and bots_page.qq_form.in_app_id.text() == "",
                bots_page.qq_form.in_app_id.text() if bots_page.qq_form is not None else "（没有表单）",
            )
            bots_page.edit_name.setText("界面新增机器人")
            if bots_page.combo_character.count() > 1:
                bots_page.combo_character.setCurrentIndex(1)
            bots_page.btn_save.click()
            saved = wait_until(
                app,
                lambda: any("界面新增机器人" in text for text in bot_names()),
                timeout=60,
            )
            checker.check("保存后列表显示新机器人的名称", saved, str(bot_names()))
            saved_config = yaml_load(config_path)
            entries = saved_config.get("bots") or []
            checker.check(
                "新机器人写进了 config.yaml 的 bots 段",
                len(entries) == 1 and str(entries[0].get("name")) == "界面新增机器人",
                json.dumps(entries, ensure_ascii=False)[:200],
            )
            checker.check(
                "新机器人绑定角色被保存",
                bool(str(entries[0].get("character_id") or "")) if entries else False,
                json.dumps(entries, ensure_ascii=False)[:200],
            )
            checker.check(
                "bots 段里没有历史遗留键（NapCat / OneBot 字段）",
                all(key not in (entries[0] if entries else {}) for key in LEGACY_QQ_KEYS),
                json.dumps(entries, ensure_ascii=False)[:200],
            )

            # 仪表盘同步显示两个机器人
            window.show_page_by_key("dashboard")
            pump(app, 0.3)
            checker.check(
                "仪表盘列出 2 个机器人",
                wait_until(app, lambda: dashboard.bots_table.rowCount() == 2, timeout=30),
                str(dashboard.bots_table.rowCount()),
            )
            checker.check(
                "仪表盘的机器人下拉包含 2 个机器人与“全部”选项",
                dashboard.combo_trigger_bot.count() == 3,
                str([dashboard.combo_trigger_bot.itemText(i) for i in range(dashboard.combo_trigger_bot.count())]),
            )

            # 删除第 2 个机器人（弹窗自动确认）
            window.show_page_by_key("bots")
            bots_page.list.setCurrentRow(1)
            pump(app, 0.3)
            bots_page.btn_remove.click()
            removed = wait_until(app, lambda: bots_page.list.count() == 1, timeout=60)
            checker.check("界面可以删除第 2 个机器人", removed, str(bot_names()))

            # 第 1 个机器人不允许删除（按钮置灰 + 直接调用也会被拒绝）
            bots_page.list.setCurrentRow(0)
            pump(app, 0.3)
            checker.check(
                "选中第 1 个机器人时「删除机器人」按钮置灰",
                not bots_page.btn_remove.isEnabled(),
                str(bots_page.btn_remove.isEnabled()),
            )
            before_dialogs = len(dialogs)
            bots_page._remove_bot()
            pump(app, 0.6)
            checker.check(
                "直接调用删除第 1 个机器人会被拒绝并给出提示",
                len(dialogs) > before_dialogs
                and "不能删除" in dialogs[-1]
                and bots_page.list.count() == 1,
                dialogs[-1] if len(dialogs) > before_dialogs else "（没有弹窗）",
            )

        # --------------------------------------------- 从界面触发主动消息
        window.show_page_by_key("dashboard")
        dashboard.refresh()
        pump(app, 0.3)
        before = len(mock.official_sent())
        dashboard.btn_trigger.click()
        triggered = wait_until(app, lambda: len(mock.official_sent()) > before, timeout=90)
        checker.check(
            "界面按钮触发主动消息成功",
            triggered,
            json.dumps(mock.official_sent()[-1:], ensure_ascii=False)[:200],
        )
        if triggered:
            checker.check(
                "主动消息内容来自 LLM 的主动分支",
                "想问问" in str((mock.official_sent() or [{}])[-1].get("content") or ""),
                str((mock.official_sent() or [{}])[-1].get("content")),
            )
            checker.check(
                "主动消息发给了配置里的 openid",
                str((mock.official_sent() or [{}])[-1].get("openid") or "") == TARGET_OPENID,
                str((mock.official_sent() or [{}])[-1].get("openid")),
            )

        # ------------------------------------------- NapCat 通道彻底消失
        import app.bot_process as bot_process_module
        from app.bot_process import BotProcess, ManagedProcess, kill_process_tree

        checker.check(
            "Bot 进程模块只暴露官方通道需要的东西（NapCatProcess / port_open 已移除）",
            all(
                not hasattr(bot_process_module, name)
                for name in ("NapCatProcess", "port_open", "launch_installer", "read_launcher_log")
            )
            and all(item is not None for item in (BotProcess, ManagedProcess, kill_process_tree)),
        )
        checker.check("应用上下文里没有 napcat 服务", not hasattr(context, "napcat"))

        # ------------------------------------------- WebSocket 事件与托盘
        events: List[dict] = []
        context.event_received.connect(lambda payload: events.append(payload))
        checker.check(
            "界面 WebSocket 事件流已连接",
            wait_until(app, lambda: context.events_connected, timeout=20),
        )
        client.post("/api/proactive/trigger", json={"force": True}, timeout=90)
        checker.check(
            "界面收到推送并可用于托盘通知",
            wait_until(
                app,
                lambda: any(item.get("type") == "chat_activity" for item in events),
                timeout=60,
            ),
            str(events)[:200],
        )
        checker.check("托盘图标可设置状态", window.tray.set_running(True) is None)

        # ------------------------------------------- 每个页面的文字是否被裁切
        for key in ("dashboard", "bots", "characters", "models", "proactive", "conversations", "settings", "logs"):
            window.show_page_by_key(key)
            pump(app, 0.35)
            page = page_by_key(key)
            try:
                page.refresh()
            except Exception:
                pass
            pump(app, 0.35)
            offenders = truncation_report(page)
            checker.check(
                "「%s」页面没有文字被裁切" % page.page_title,
                not offenders,
                "；".join(offenders[:4]),
            )

        # ------------------------------------------------------------ 托盘行为
        # 放在最后：隐藏窗口会让页面上所有控件的 isVisible() 变成 False，
        # 会放水上面的「文字是否被裁切」检查，所以这里才做隐藏/恢复。
        window.hide_to_tray()
        pump(app, 0.3)
        checker.check("「最小化到托盘」后窗口隐藏", not window.isVisible())
        window.show_window()
        pump(app, 0.3)
        checker.check("从托盘恢复窗口", window.isVisible())

        # 官方平台 ID 是 20 位数字（超过 int64）：状态信号每 3 秒发一次，
        # 以前每个越界整数都会抛一次 OverflowError（堆栈为空，用户只看到弹窗）。
        huge_snapshot = dict(context.last_status or {})
        huge_snapshot["self_id"] = 10 ** 25
        huge_snapshot["qq"] = {**(huge_snapshot.get("qq") or {}), "user_id": 10 ** 25}
        bots = huge_snapshot.get("bots") or [{}]
        huge_snapshot["bots"] = [{**bots[0], "user_id": 10 ** 25}]
        before = len(unhandled)
        context.status_updated.emit(huge_snapshot)
        pump(app, 0.5)
        checker.check(
            "超大平台 ID（20 位数字）经过状态信号不再抛 OverflowError",
            not any("OverflowError" in item for item in unhandled[before:]),
            (unhandled[before].splitlines()[-1] if len(unhandled) > before else ""),
        )

        checker.check(
            "界面槽函数没有未处理异常",
            not unhandled,
            unhandled[0].splitlines()[-1] if unhandled else "",
        )

        # Bot 收尾放在 return 之前：/api/shutdown 后 bot 必须及时退出。
        # 之前只在 finally 里静默等 20 秒再强杀：bot 卡住时 EventStream 正处在
        # 重连退避里，stop() 的短暂等待不够，QThread 析构触发 Qt failfast
        # （0xC0000409）——自检 149 项全过、进程却以非 0 退出码结束（CI 报 1）。
        try:
            client.post("/api/shutdown", timeout=10)
        except Exception:
            pass
        deadline = time.time() + 15
        while time.time() < deadline and bot.poll() is None:
            time.sleep(0.3)
        if bot.poll() is None:
            checker.check(
                "Bot 进程在 /api/shutdown 后 15 秒内退出（无泄漏的后台任务）",
                False,
                "查 bot.log 定位卡住的后台任务（如未结束的 WebSocket 处理器）",
            )
            bot.terminate()
            time.sleep(1)
        bot_stopped = True
        return checker.summary()
    except Exception as exc:  # pragma: no cover - 自检自身异常
        import traceback

        traceback.print_exc()
        checker.check("GUI 自检未抛出异常", False, str(exc))
        return 1
    finally:
        # mock / bot 清理放在 Qt 析构（context.shutdown / processEvents）之前：
        # Qt 清理阶段偶发 abort（0xC0000409）时，后面的语句不会执行，
        # mock 子进程会留成孤儿（Windows 下 MockProcess 的 Job Object 是第二道保险）
        if not bot_stopped:
            try:
                client.post("/api/shutdown", timeout=10)
            except Exception:
                pass
            deadline = time.time() + 15
            while time.time() < deadline and bot.poll() is None:
                time.sleep(0.3)
            if bot.poll() is None:
                print("  [警告] Bot 进程收到 /api/shutdown 后 15 秒仍未退出，已强制结束（查 bot.log 定位卡住的后台任务）")
                bot.terminate()
                time.sleep(1)
        try:
            client.close()
        except Exception:
            pass
        try:
            mock.stop()
        except Exception:
            pass
        try:
            if context is not None:
                context.shutdown()
        except Exception:
            pass
        try:
            if window is not None:
                window.tray.hide()
        except Exception:
            pass
        try:
            app.processEvents()
        except Exception:
            pass


def yaml_load(path) -> Dict[str, Any]:
    """读取 config.yaml（自检里多次用到，单独抽出来便于排查）。"""
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


if __name__ == "__main__":
    raise SystemExit(main())
