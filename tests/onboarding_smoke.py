"""配置引导自检：验证首次运行会弹出引导、能走完、能跳过、不再重复弹出。

运行::

    python -m tests.onboarding_smoke

引导是**固定 6 步**的向导（欢迎 → LLM → QQ → 角色卡 → 主动消息 → 完成），
因为 QQ 只有「官方机器人」一种接入方式，步骤不再随连接方式增减。流程：

1. 用「未配置」的 config（空 API Key + 空 AppID）启动真实 Bot 与真实界面；
2. 断言引导自动弹出、步骤数与标题正确、左侧「每一步都能先跳过」提示排版正常；
3. LLM 步骤：服务商一键预设、拉取模型列表（含筛选）、当场「测试连接」（连 mock LLM）；
4. QQ 步骤：只有官方机器人一套字段（AppID / AppSecret / 沙盒 / openid / 白名单），
   并把第 1 个机器人绑定到一个角色；
5. 角色卡步骤：导入文件 + 一键导入内置角色；
6. 主动消息步骤：设置频率上限与触发方式；
7. 汇总页 → 点「完成」，断言配置写入 config.yaml、Bot 立即生效并连上（mock）官方平台；
8. 再从「系统设置 → 配置引导」重新打开并点「取消引导」，断言跳过行为正确、之后不再自动弹出。
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
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel  # noqa: E402

import httpx  # noqa: E402

from tests import card_factory, mock_servers, smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 固定 6 步的稳定标识与左侧导航标题（与 app.onboarding.STEP_KEYS / STEP_TITLES 对应）
EXPECTED_KEYS = ["welcome", "llm", "qq", "characters", "proactive", "finish"]
EXPECTED_RAIL_TITLES = ["欢迎", "LLM 接口", "QQ 配置", "角色卡", "主动消息", "完成"]
# 每一步页面上的大标题：6 步连续编号（欢迎页也是「第 1 步」，不会跳号）
EXPECTED_HEADS = {
    "welcome": "第 1 步：用 QQ 官方机器人就能玩，不需要小号",
    "llm": "第 2 步：选择服务商与模型",
    "qq": "第 3 步：填写 QQ 官方机器人凭据",
    "characters": "第 4 步：导入角色卡",
    "proactive": "第 5 步：主动消息设置",
    "finish": "第 6 步：确认一下",
}

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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def wait_ui(app, predicate, timeout: float = 25.0, interval: float = 0.05) -> bool:
    """等待界面条件成立（必须 pump Qt 事件，否则定时器与信号都不会被处理）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def install_excepthook(caught: List[str]) -> None:
    """收集 Qt 槽函数里的未处理异常（真实程序里会弹错误对话框，自检里会被吞掉）。"""
    import traceback

    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        caught.append("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    sys.excepthook = _hook


def patch_message_boxes(recorded: List[str]) -> None:
    """让模态弹窗自动应答，否则无人点击时会一直等下去。"""
    from PySide6.QtWidgets import QMessageBox

    def _make(answer):
        def _handler(_parent, title, text, *args, **kwargs):
            recorded.append("%s | %s" % (title, text))
            return answer

        return staticmethod(_handler)

    QMessageBox.information = _make(QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.warning = _make(QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.critical = _make(QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.question = _make(QMessageBox.Yes)  # type: ignore[assignment]


def prepare_config(config_path: Path, mock, api_port: int) -> None:
    """写出一份「首次运行」的配置：LLM 与 QQ 凭据都留空，引导才会自动弹出。

    * LLM 地址指向 mock（引导里的「获取模型列表 / 测试连接」要连得上）；
    * QQ 段只保留官方机器人的空壳（没有 AppID），并清掉历史遗留键。
    """
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    app = data.setdefault("app", {})
    app["start_bot_on_launch"] = False
    app["onboarding_done"] = False
    app.pop("start_napcat_on_launch", None)

    data.setdefault("api", {}).update({"host": "127.0.0.1", "port": int(api_port)})
    data.setdefault("llm", {}).update(
        {
            "base_url": "%s/v1" % mock.base_url,
            "api_key": "",
            "model": "mock-model",
            "max_tokens": 128,
            "timeout": 20,
            "max_retries": 1,
        }
    )

    qq = data.setdefault("qq", {})
    official = dict(qq.get("official") or {})
    official.update(
        {
            "app_id": "",
            "app_secret": "",
            "api_domain": mock.base_url,
            "token_url": "%s/app/getAppAccessToken" % mock.base_url,
            "sandbox": False,
            "target_openid": "",
            "group_openid": "",
            "allow_all_users": True,
            "allowed_users": [],
            "allowed_groups": [],
            "markdown": False,
            "max_reply_segments": 3,
            "reply_segment_max_len": 200,
        }
    )
    qq.update(
        {
            "official": official,
            "name": "机器人 1",
            "enabled": True,
            "character_id": "",
            "character_name": "",
            "user_nickname": "小可爱",
            "reply_enabled": True,
            "group_reply_enabled": False,
        }
    )
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
    data.setdefault("characters", {})["import_builtin"] = False
    data.pop("napcat", None)
    data.pop("onebot", None)

    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_config(path: Path) -> Dict[str, Any]:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def step_page(wizard, key: str):
    """取某一页的根控件（stack 里是 QScrollArea → 真正的页面）。"""
    return wizard.stack.widget(wizard.sequence().index(key)).widget()


def step_head(wizard, key: str) -> str:
    """取某一页最上方的大标题（objectName = OnbTitle）。"""
    labels = [item for item in step_page(wizard, key).findChildren(QLabel) if item.objectName() == "OnbTitle"]
    return labels[0].text() if labels else ""


def page_labels(wizard, key: str) -> str:
    return "\n".join(item.text() for item in step_page(wizard, key).findChildren(QLabel))


def summary_text(wizard) -> str:
    """汇总页上所有可见文字（用来断言汇总内容）。"""
    texts: List[str] = []
    for index in range(wizard.summary_layout.count()):
        widget = wizard.summary_layout.itemAt(index).widget()
        if widget is not None:
            texts.extend(item.text() for item in widget.findChildren(QLabel))
    return " ".join(texts)


def main() -> int:
    checker = smoke_test.Checker()
    print("配置引导自检开始（Python %s）" % sys.version.split()[0])

    mock_port = free_port()
    api_port = free_port()
    smoke_test.MOCK_URL = "http://127.0.0.1:%d" % mock_port
    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port

    mock = mock_servers.MockProcess(port=mock_port).start()
    mock.reset(reply_text="回复。", proactive_text="主动消息。")

    data_dir = Path(tempfile.mkdtemp(prefix="tavern-onb-"))
    config_path = smoke_test.write_bot_config(data_dir)
    prepare_config(config_path, mock, api_port)

    os.environ["QQAI_DATA_DIR"] = str(data_dir)
    os.environ["QQAI_HOME"] = str(ROOT)

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

    from PySide6.QtWidgets import QApplication

    from app.config_store import gui_config
    from app.context import AppContext
    from app.main_window import MainWindow
    from app.theme import apply_theme

    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=30.0)
    app = QApplication(sys.argv[:1])
    context = None
    window = None
    unhandled: List[str] = []
    dialogs: List[str] = []
    install_excepthook(unhandled)
    patch_message_boxes(dialogs)

    try:
        ready = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=150)
        checker.check("Bot 进程已启动", ready)
        if not ready:
            print("\n".join(bot_output[-40:]))
            return 1

        # ------------------------------------------------------------ 界面
        config = gui_config()
        apply_theme(app, "dark")
        context = AppContext(config)
        window = MainWindow(context, allow_onboarding=True)
        window.show()

        checker.check(
            "未配置时判定需要引导",
            window.should_onboard(),
            "onboarding_done=%s api_key=%r app_id=%r"
            % (
                config.get("app.onboarding_done"),
                config.get("llm.api_key"),
                config.get("qq.official.app_id"),
            ),
        )

        appeared = wait_ui(app, lambda: window.onboarding is not None, timeout=30)
        checker.check("首次运行自动弹出配置引导", appeared)
        if not appeared:
            return 1

        wizard = window.onboarding
        checker.check("引导共 6 步（固定步骤，不随连接方式增减）", wizard.step_count == 6, str(wizard.step_count))
        checker.check(
            "步骤序列固定为 欢迎 → LLM → QQ → 角色卡 → 主动消息 → 完成",
            wizard.sequence() == EXPECTED_KEYS,
            str(wizard.sequence()),
        )
        checker.check(
            "左侧导航列出 6 步标题",
            [item.title.text() for item in wizard.step_items] == EXPECTED_RAIL_TITLES,
            str([item.title.text() for item in wizard.step_items]),
        )
        checker.check("当前处于第 1 步（欢迎）", wizard.index == 0 and wizard.current_key == "welcome", wizard.current_key)
        checker.check(
            "每一步页面的大标题正确（连续编号，只有官方机器人一种方式）",
            {key: step_head(wizard, key) for key in EXPECTED_KEYS} == EXPECTED_HEADS,
            str({key: step_head(wizard, key) for key in EXPECTED_KEYS}),
        )
        checker.check(
            "步骤编号里没有 NapCat 步骤",
            all("NapCat" not in step_head(wizard, key) for key in EXPECTED_KEYS)
            and "napcat" not in wizard.sequence(),
            str([step_head(wizard, key) for key in EXPECTED_KEYS]),
        )
        checker.check(
            "底部保留「取消引导（我自己配置）」按钮",
            wizard.btn_cancel.text() == "取消引导（我自己配置）",
            wizard.btn_cancel.text(),
        )
        checker.check(
            "底部步骤提示显示第 1 / 6 步",
            wizard.lbl_footer_hint.text() == "第 1 / 6 步",
            wizard.lbl_footer_hint.text(),
        )

        # ---------------------------- 第 1 步：标语文案与"可跳过"提示排版
        welcome_text = page_labels(wizard, "welcome")
        checker.check(
            "欢迎页标语改为官方机器人优先",
            "官方机器人" in welcome_text and "准备三样东西" not in welcome_text,
            welcome_text.splitlines()[0] if welcome_text else "",
        )
        checker.check(
            "欢迎页明确说明不需要小号，也不再出现第三方协议与封号风险提示",
            "不需要小号" in welcome_text
            and "NapCat" not in welcome_text
            and "OneBot" not in welcome_text
            and "封号风险" not in welcome_text
            and "不推荐" not in welcome_text,
            [line for line in welcome_text.splitlines() if "小号" in line or "NapCat" in line][:2],
        )
        checker.check(
            "欢迎页给出官方机器人凭据的获取位置（q.qq.com）",
            "q.qq.com" in welcome_text,
            [line for line in welcome_text.splitlines() if "q.qq.com" in line][:1],
        )

        rail = wizard.rail_layout.parentWidget()
        wait_ui(app, lambda: wizard._rail_hint is not None and wizard._rail_hint.height() > 0, timeout=5)
        hint = wizard._rail_hint
        checker.check(
            "左侧提示已创建并加入布局",
            hint is not None and wizard.rail_layout.indexOf(hint) >= 0,
            str(None if hint is None else wizard.rail_layout.indexOf(hint)),
        )
        checker.check(
            "左侧提示使用了独立样式（OnbRailHint）",
            hint is not None and hint.objectName() == "OnbRailHint",
            "" if hint is None else hint.objectName(),
        )
        laid_out = [wizard.rail_layout.itemAt(i).widget() for i in range(wizard.rail_layout.count())]
        orphans = [
            child
            for child in rail.findChildren(QLabel)
            if child.parent() is rail and child not in laid_out
        ]
        checker.check(
            "左侧导航没有游离在布局外的标签（不再有左上角重影文字）",
            not orphans,
            str([child.text() for child in orphans]),
        )
        checker.check(
            "左侧提示排在步骤列表下方，不压住步骤条目",
            hint is not None
            and hint.height() > 0
            and hint.y() >= max(item.y() + item.height() for item in wizard.step_items),
            "hint.y=%s h=%s items=%s"
            % (
                None if hint is None else hint.y(),
                None if hint is None else hint.height(),
                [(item.y(), item.height()) for item in wizard.step_items],
            ),
        )
        checker.check(
            "左侧提示文字说明每一步都能跳过",
            hint is not None and "跳过" in hint.text(),
            "" if hint is None else hint.text(),
        )
        header_subs = [
            label.text()
            for label in wizard.findChildren(QLabel)
            if label.objectName() == "OnbSubtitle"
            and label.parent() is not None
            and label.parent().objectName() == "OnbHeader"
        ]
        checker.check(
            "头部副标题精简为一行（明确可以跳过）",
            len(header_subs) == 1 and len(header_subs[0]) <= 40 and "跳过" in header_subs[0],
            str(header_subs),
        )

        # ---------------------------------------------------- 第 2 步：LLM
        wizard.btn_next.click()
        checker.check("点击下一步进入 LLM 步骤", wizard.index == 1 and wizard.current_key == "llm", wizard.current_key)
        form = wizard.llm_form
        checker.check(
            "LLM 步骤预填了 Base URL",
            form.edit_base.text() == "%s/v1" % smoke_test.MOCK_URL,
            form.edit_base.text(),
        )
        checker.check(
            "服务商下拉内置了常用预设",
            form.combo_preset.count() >= 10,
            str(form.combo_preset.count()),
        )

        # 一键选择服务商 → 自动填好 Base URL 与候选模型
        from app.llm_presets import CUSTOM_LABEL, PRESETS

        deepseek = next(item for item in PRESETS if item.key == "deepseek")
        form.combo_preset.setCurrentText(deepseek.name)
        wait_ui(app, lambda: True, timeout=0.2)
        checker.check(
            "选择服务商后自动填入 Base URL",
            form.edit_base.text() == deepseek.base_url,
            form.edit_base.text(),
        )
        checker.check(
            "选择服务商后给出候选模型",
            form.model() == deepseek.models[0],
            form.model(),
        )
        checker.check(
            "显示申请 API Key 的入口",
            "http" in form.link_docs.text(),
            form.link_docs.text(),
        )

        # 切回自定义地址（指向 mock）后测试连接 + 拉取模型
        form.combo_preset.setCurrentText(CUSTOM_LABEL)
        form.edit_base.setText("%s/v1" % smoke_test.MOCK_URL)
        form.edit_key.setText("mock-key")
        form.combo_model.setCurrentText("mock-model")

        form.btn_fetch.click()
        fetched = wait_ui(app, lambda: bool(form.last_models), timeout=30)
        checker.check("可从上游获取模型列表", fetched, str(form.last_models))
        checker.check(
            "获取到的模型进入下拉框",
            "mock-model-mini" in [form.combo_model.itemText(i) for i in range(form.combo_model.count())],
            str([form.combo_model.itemText(i) for i in range(form.combo_model.count())]),
        )
        checker.check(
            "非对话模型已被过滤",
            all("embedding" not in name and "whisper" not in name for name in form.last_models),
            str(form.last_models),
        )
        checker.check(
            "获取结果有可读提示",
            "获取到" in form.lbl_status.text(),
            form.lbl_status.text(),
        )

        # 模型列表：显示、点选、筛选、切回全部
        checker.check(
            "获取后模型列表显示可选模型",
            form.list_models.count() == len(form.last_models) == 3,
            "%d / %s" % (form.list_models.count(), form.last_models),
        )
        checker.check(
            "模型列表内容正确",
            [form.list_models.item(i).text() for i in range(form.list_models.count())]
            == ["mock-model", "mock-model-mini", "mock-model-pro"],
            str([form.list_models.item(i).text() for i in range(form.list_models.count())]),
        )
        form.list_models.itemClicked.emit(form.list_models.item(1))
        checker.check(
            "点击列表项即选中该模型",
            form.model() == "mock-model-mini",
            form.model(),
        )
        form.edit_filter.setText("pro")
        wait_ui(app, lambda: True, timeout=0.2)
        checker.check(
            "筛选框可过滤模型",
            [form.list_models.item(i).text() for i in range(form.list_models.count())] == ["mock-model-pro"],
            str(form.list_models.count()),
        )
        form.edit_filter.clear()
        form.chk_show_all.setChecked(True)
        wait_ui(app, lambda: True, timeout=0.2)
        checker.check(
            "可切换显示非对话模型",
            form.list_models.count() == 5,
            str(form.list_models.count()),
        )
        form.chk_show_all.setChecked(False)
        wait_ui(app, lambda: True, timeout=0.2)

        form.combo_model.setCurrentText("mock-model")
        form.btn_test.click()
        tested = wait_ui(app, lambda: bool(form.last_result and form.last_result.get("ok")), timeout=30)
        checker.check("向导内「测试连接」成功", tested, str(form.last_result))
        checker.check(
            "测试结果在界面上可见",
            "✓" in form.lbl_status.text(),
            form.lbl_status.text(),
        )

        # ----------------------------------------------------- 第 3 步：QQ
        wizard.btn_next.click()
        checker.check("进入 QQ 配置步骤", wizard.index == 2 and wizard.current_key == "qq", wizard.current_key)
        qq_form = wizard.qq_form
        checker.check(
            "QQ 步骤只有官方机器人一套字段（没有连接方式下拉 / 分页 / NapCat 字段）",
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
            )
            and all(
                hasattr(qq_form, name)
                for name in (
                    "in_app_id",
                    "in_app_secret",
                    "chk_sandbox",
                    "in_target_openid",
                    "btn_forget_openid",
                    "in_group_openid",
                    "chk_allow_all",
                    "in_allowed_users",
                    "in_allowed_groups",
                    "chk_official_group",
                    "chk_markdown",
                    "spin_official_len",
                    "spin_official_segments",
                    "btn_test",
                    "btn_reconnect",
                    "lbl_status",
                    "lbl_learned",
                )
            ),
        )
        checker.check(
            "官方控件的 values() 只给出 official + group_reply_enabled",
            set(qq_form.values().keys()) == {"official", "group_reply_enabled"},
            str(sorted(qq_form.values().keys())),
        )
        checker.check(
            "官方模式的状态提示要求填凭据",
            "AppID" in qq_form.lbl_status.text() or "未配置" in qq_form.lbl_status.text(),
            qq_form.lbl_status.text(),
        )
        qq_form.in_app_id.setText(mock_servers.OFFICIAL_APP_ID)
        qq_form.in_app_secret.setText(mock_servers.OFFICIAL_APP_SECRET)
        qq_form.in_target_openid.setText("user-onboarding")
        qq_form.spin_official_segments.setValue(2)
        qq_form.spin_official_len.setValue(120)
        qq_form.chk_official_group.setChecked(True)
        patch_values = qq_form.values()
        checker.check(
            "官方模式的配置项完整（AppID / Secret / 沙盒 / 主动消息目标）",
            patch_values["official"]["app_id"] == mock_servers.OFFICIAL_APP_ID
            and patch_values["official"]["app_secret"] == mock_servers.OFFICIAL_APP_SECRET
            and patch_values["official"]["target_openid"] == "user-onboarding"
            and "sandbox" in patch_values["official"]
            and patch_values["official"]["max_reply_segments"] == 2
            and patch_values["official"]["reply_segment_max_len"] == 120
            and patch_values["group_reply_enabled"] is True,
            str(patch_values)[:220],
        )
        checker.check(
            "QQ 步骤提供「绑定角色」下拉（第一项是“不绑定”）",
            hasattr(wizard, "combo_character")
            and wizard.combo_character.count() >= 1
            and str(wizard.combo_character.itemData(0)) == "",
            str(
                [wizard.combo_character.itemText(i) for i in range(wizard.combo_character.count())]
                if hasattr(wizard, "combo_character")
                else "无"
            ),
        )

        # ------------------------------------------------ 第 4 步：角色卡
        wizard.btn_next.click()
        checker.check(
            "第 3 步之后直接进入角色卡（没有 NapCat 步骤）",
            wizard.current_key == "characters" and "napcat" not in wizard.sequence(),
            wizard.current_key,
        )
        checker.check(
            "角色卡步骤提供「导入内置角色」",
            wizard.btn_import_builtin.isEnabled(),
        )
        cards_dir = data_dir / "characters"
        cards_dir.mkdir(parents=True, exist_ok=True)
        card_factory.write_json_card(cards_dir / "引导角色.json", "引导角色")
        wizard.scan_directory()
        scanned = wait_ui(app, lambda: len(wizard._characters) == 1, timeout=40)
        checker.check("向导内可扫描导入角色卡", scanned, str([item.get("name") for item in wizard._characters]))
        checker.check(
            "角色列表控件显示角色",
            wizard.list_characters.count() == 1,
            str(wizard.list_characters.count()),
        )

        extra = card_factory.write_png_card(cards_dir / "引导角色2.png", "引导角色2")
        wizard.import_paths([extra])
        imported = wait_ui(app, lambda: len(wizard._characters) == 2, timeout=40)
        checker.check("向导内可导入角色卡文件", imported, str(len(wizard._characters)))

        # 一键导入内置角色（随程序分发的 3 张卡）
        wizard.btn_import_builtin.click()
        builtin = wait_ui(app, lambda: len(wizard._characters) == 5, timeout=60)
        checker.check(
            "向导内可一键导入 3 个内置角色",
            builtin,
            str([item.get("name") for item in wizard._characters]),
        )
        checker.check(
            "内置角色出现在向导的角色列表里",
            {"小栖", "阿元", "苏苏"} <= {str(item.get("name")) for item in wizard._characters},
            str(sorted(str(item.get("name")) for item in wizard._characters)),
        )

        # --------------------------------- 绑定角色（多机器人：机器人 ↔ 角色）
        checker.check(
            "导入角色后下拉自动出现这些角色",
            wait_ui(app, lambda: wizard.combo_character.count() == 6, timeout=20),
            str([wizard.combo_character.itemText(i) for i in range(wizard.combo_character.count())]),
        )
        position = wizard.combo_character.findText("引导角色")
        checker.check("绑定下拉里能按名字找到刚导入的角色", position > 0, str(position))
        wizard.combo_character.setCurrentIndex(position if position > 0 else 1)
        bound_id = str(wizard.combo_character.currentData() or "")
        bound_name = str(wizard.combo_character.currentText() or "")
        checker.check("可以在向导里指定机器人用哪个角色", bool(bound_id), "%s/%s" % (bound_id, bound_name))

        # ------------------------------------------------ 第 5 步：主动消息
        wizard.btn_next.click()
        checker.check("进入主动消息步骤", wizard.current_key == "proactive", wizard.current_key)
        wizard.spin_global.setValue(5)
        wizard.spin_per_char.setValue(2)
        wizard.chk_random.setChecked(True)

        # -------------------------------------------------- 第 6 步：汇总
        wizard.btn_next.click()
        checker.check("进入汇总步骤", wizard.current_key == "finish", wizard.current_key)
        checker.check(
            "汇总页列出 5 项检查",
            wizard.summary_layout.count() == 5,
            str(wizard.summary_layout.count()),
        )
        checker.check("汇总页按钮变为「完成」", wizard.btn_next.text() == "完成", wizard.btn_next.text())
        shown = summary_text(wizard)
        checker.check(
            "汇总页显示填写的 AppID",
            mock_servers.OFFICIAL_APP_ID in shown,
            shown[:220],
        )
        checker.check(
            "汇总页显示机器人绑定的角色",
            bound_name in shown and ("绑定" in shown),
            shown[:220],
        )
        checker.check(
            "汇总页不再出现 NapCat / 第三方协议字样",
            "NapCat" not in shown and "OneBot" not in shown,
            shown[:220],
        )

        # ------------------------------------------------------------ 完成
        wizard.btn_next.click()
        checker.check("完成后引导关闭", window.onboarding is None)
        checker.check("向导标记为已完成（非跳过）", wizard.skipped is False)

        saved = load_config(config_path)
        checker.check("保存了 API Key", saved["llm"]["api_key"] == "mock-key", str(saved["llm"]))
        checker.check(
            "保存了机器人绑定的角色（第 1 个机器人）",
            str((saved.get("qq") or {}).get("character_id") or "") == bound_id,
            str((saved.get("qq") or {}).get("character_id")),
        )
        checker.check(
            "保存了第 1 个机器人的名称与启用状态",
            str((saved.get("qq") or {}).get("name") or "") != ""
            and bool((saved.get("qq") or {}).get("enabled", False)),
            json.dumps({k: (saved.get("qq") or {}).get(k) for k in ("name", "enabled")}, ensure_ascii=False),
        )
        checker.check(
            "保存了选中的模型",
            saved["llm"]["model"] == "mock-model",
            str(saved["llm"].get("model")),
        )
        checker.check(
            "保存了自定义的 Base URL",
            saved["llm"]["base_url"] == "%s/v1" % smoke_test.MOCK_URL,
            str(saved["llm"].get("base_url")),
        )
        checker.check(
            "保存了官方机器人凭据（AppID / AppSecret / 目标 openid）",
            (saved["qq"].get("official") or {}).get("app_id") == mock_servers.OFFICIAL_APP_ID
            and (saved["qq"].get("official") or {}).get("app_secret") == mock_servers.OFFICIAL_APP_SECRET
            and (saved["qq"].get("official") or {}).get("target_openid") == "user-onboarding",
            str(saved["qq"].get("official")),
        )
        checker.check(
            "保存了官方模式的长消息拆分与群聊开关",
            int((saved["qq"].get("official") or {}).get("max_reply_segments") or 0) == 2
            and int((saved["qq"].get("official") or {}).get("reply_segment_max_len") or 0) == 120
            and bool(saved["qq"].get("group_reply_enabled")) is True,
            json.dumps(
                {
                    k: (saved["qq"].get("official") or {}).get(k)
                    for k in ("max_reply_segments", "reply_segment_max_len")
                },
                ensure_ascii=False,
            ),
        )
        checker.check(
            "配置里没有历史遗留键（mode / napcat / onebot 都已被清掉）",
            all(key not in saved["qq"] for key in LEGACY_QQ_KEYS)
            and "napcat" not in saved
            and "onebot" not in saved,
            json.dumps(sorted(saved["qq"].keys()), ensure_ascii=False),
        )
        checker.check(
            "保存了主动消息设置",
            int(saved["proactive"]["global_daily_limit"]) == 5
            and int(saved["proactive"]["per_character_daily_limit"]) == 2
            and bool(saved["proactive"]["random_enabled"]) is True,
            str(saved["proactive"]),
        )
        checker.check("写入 onboarding_done", bool(saved["app"]["onboarding_done"]) is True)

        applied = wait_ui(
            app,
            lambda: int(client.get("/api/config").json()["config"]["proactive"]["global_daily_limit"]) == 5,
            timeout=25,
        )
        checker.check("Bot 侧立即应用了新配置", applied)
        checker.check(
            "Bot 侧同步了官方机器人凭据",
            str(
                ((client.get("/api/config").json()["config"].get("qq") or {}).get("official") or {}).get("app_id")
                or ""
            )
            == mock_servers.OFFICIAL_APP_ID,
        )
        checker.check(
            "Bot 侧同步了机器人绑定的角色",
            str((client.get("/api/config").json()["config"].get("qq") or {}).get("character_id") or "") == bound_id,
        )
        gateway = wait_ui(
            app,
            lambda: bool((client.get("/api/status").json().get("qq") or {}).get("connected")),
            timeout=90,
        )
        checker.check(
            "完成后 Bot 用新凭据连上了官方网关（mock 开放平台）",
            gateway,
            json.dumps((client.get("/api/status").json().get("qq") or {}), ensure_ascii=False)[:200],
        )
        checker.check("不再需要引导（下次启动不弹）", window.should_onboard() is False)

        # ------------------------------------------- 重新打开 + 取消引导
        from app.main_window import NAV_KEYS

        window.show_page(NAV_KEYS.index("settings"))
        settings_page = window.pages[NAV_KEYS.index("settings")]
        settings_page.ensure_loaded()
        wait_ui(app, lambda: True, timeout=0.3)
        settings_page.btn_onboarding.click()
        reopened = wait_ui(app, lambda: window.onboarding is not None, timeout=15)
        checker.check("系统设置里可重新打开引导", reopened)
        if reopened:
            second = window.onboarding
            checker.check(
                "重新打开时回显已保存的配置",
                second.qq_form.in_app_id.text() == mock_servers.OFFICIAL_APP_ID
                and second.llm_form.edit_key.text() == "mock-key"
                and second.step_count == 6,
                "%s / %s / %s"
                % (second.qq_form.in_app_id.text(), second.llm_form.edit_key.text(), second.step_count),
            )
            second.btn_cancel.click()
            checker.check("取消引导后关闭", window.onboarding is None)
            checker.check("取消被标记为跳过", second.skipped is True)

        after_cancel = load_config(config_path)
        checker.check(
            "取消引导不会清掉已有配置",
            after_cancel["llm"]["api_key"] == "mock-key"
            and (after_cancel["qq"].get("official") or {}).get("app_id") == mock_servers.OFFICIAL_APP_ID,
            str(after_cancel["llm"]),
        )
        checker.check("取消后也不再自动弹出", window.should_onboard() is False)
        checker.check(
            "界面槽函数没有未处理异常",
            not unhandled,
            unhandled[0].splitlines()[-1] if unhandled else "",
        )
        return checker.summary()
    except Exception as exc:  # pragma: no cover
        import traceback

        traceback.print_exc()
        checker.check("配置引导自检未抛出异常", False, str(exc))
        return 1
    finally:
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
        app.processEvents()
        try:
            client.post("/api/shutdown", timeout=10)
        except Exception:
            pass
        if not wait_for(lambda: bot.poll() is not None, timeout=20):
            bot.terminate()
            wait_for(lambda: bot.poll() is not None, timeout=10)
        client.close()
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
