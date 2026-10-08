"""端到端自检脚本（QQ 官方机器人通道，不依赖真实 QQ 与真实 LLM）。

运行::

    python -m tests.smoke_test
    python -m tests.smoke_test --unit-only

它会：

1. 单元自检：角色卡解析、触发规则、文本处理（``common/text.py``）、提示词构建、
   数据库读写与统计、配置（含历史 NapCat/OneBot 字段清理）、官方事件解析与错误码；
2. 启动 mock QQ 官方平台 + mock LLM（独立进程）；
3. 用临时数据目录启动**真实的 Bot 进程**（``python -m bot.main --no-console``）；
4. 验证：官方网关连接、角色卡导入、单聊回复（带 msg_id/msg_seq）、群聊 @ 回复、
   openid 自动记忆、长期记忆、主动消息、频率限制、免打扰、多机器人配置、
   WebSocket 推送、配置热更新、优雅退出。

全部通过退出码为 0，否则为 1。其它自检脚本会复用本模块的
:class:`Checker`、:func:`drain`、:func:`write_bot_config` 以及
``API_PORT`` / ``BASE_URL`` / ``MOCK_URL``。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests import card_factory, mock_servers  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_PORT = 8799
MOCK_PORT = 3100
BASE_URL = "http://127.0.0.1:%d" % API_PORT
MOCK_URL = "http://127.0.0.1:%d" % MOCK_PORT

#: 官方通道的 intent（1<<25：群聊与单聊事件）
OFFICIAL_INTENTS = 33554432


# ============================================================== 断言工具 =====
class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []
        self.phase_name = ""

    def phase(self, title: str) -> None:
        self.phase_name = title
        print("\n" + "=" * 74)
        print("  %s" % title)
        print("=" * 74)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("  [PASS] %s" % name)
        else:
            self.failed.append("%s :: %s" % (self.phase_name, name))
            print("  [FAIL] %s%s" % (name, ("  ->  %s" % detail) if detail else ""))
        return bool(condition)

    def summary(self) -> int:
        print("\n" + "=" * 74)
        print("  自检结果：%d 项通过，%d 项失败" % (self.passed, len(self.failed)))
        for item in self.failed:
            print("   · 失败：%s" % item)
        print("=" * 74)
        return 0 if not self.failed else 1


def wait_for(predicate, timeout: float = 25.0, interval: float = 0.4) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ============================================================== 单元自检 =====
def phase_unit_logic(c: Checker) -> None:
    c.phase("1/4 单元自检：解析 / 规则 / 文本处理 / 官方通道")

    from bot.character_manager import CharacterCardError, load_card_bytes
    from bot.scheduler import triggers
    from common.text import sanitize_text, split_message, typing_delay

    # 官方平台的机器人 ID 是 20 位数字，超过 int64；Qt 的 QVariantMap 装不下它，
    # 直接塞进 Signal(dict) 会每 3 秒抛一次 OverflowError（真实用户报过）。
    from app.qt_safe import INT64_MAX, qt_safe

    huge = 10 ** 25
    safe = qt_safe({"self_id": huge, "qq": {"user_id": huge}, "bots": [{"user_id": huge}], "ok": 1})
    c.check(
        "qt_safe 把超出 int64 的整数转成字符串",
        safe["self_id"] == str(huge) and safe["qq"]["user_id"] == str(huge) and safe["bots"][0]["user_id"] == str(huge),
        str(safe)[:120],
    )
    c.check("qt_safe 保留范围内的整数", safe["ok"] == 1 and INT64_MAX == 2 ** 63 - 1)
    import math as _math

    c.check(
        "qt_safe 处理 nan / inf（Qt 也装不下）",
        qt_safe({"a": float("nan"), "b": float("inf")}) == {"a": "nan", "b": "inf"},
        str(qt_safe({"a": float("nan"), "b": float("inf")})),
    )
    c.check(
        "qt_safe 不改变字符串与布尔值",
        qt_safe({"s": "文本", "t": True, "n": None}) == {"s": "文本", "t": True, "n": None},
        "",
    )
    c.check(
        "_math 可用性（避免未使用导入告警）", _math.isfinite(1.0),
    )

    tmp = Path(tempfile.mkdtemp(prefix="tavern-unit-"))
    png_path = card_factory.write_png_card(tmp / "a.png", "深夜角色")
    json_path = card_factory.write_json_card(tmp / "b.json", "元气角色")
    yaml_path = card_factory.write_yaml_card(tmp / "c.yaml", "元气角色Y")

    png_card = load_card_bytes("a.png", png_path.read_bytes())
    c.check("PNG 角色卡解析出名称", png_card.name == "深夜角色", png_card.name)
    c.check("PNG 角色卡解析出性格", "温柔" in png_card.personality)
    c.check("PNG 角色卡带出头像字节", bool(png_card.avatar_bytes))
    c.check("PNG 角色卡识别 V2 规格", png_card.spec == "chara_card_v2", png_card.spec)

    json_card = load_card_bytes("b.json", json_path.read_bytes())
    c.check("JSON 角色卡解析成功", json_card.name == "元气角色", json_card.name)

    yaml_card = load_card_bytes("c.yaml", yaml_path.read_bytes())
    c.check("YAML 角色卡解析成功", yaml_card.name == "元气角色Y", yaml_card.name)

    try:
        load_card_bytes("bad.png", card_factory.tiny_png())
        c.check("无 chara 的 PNG 应报错", False, "未抛出异常")
    except CharacterCardError:
        c.check("无 chara 的 PNG 正确报错", True)

    # ------------------------------------------------ Chub 风格卡片
    # Chub 导出的 chara_card_v2 会把 avatar 写成**远程 URL**（非 V2 标准字段），
    # 早期版本把 URL 字符串当 base64 解出几十字节垃圾当头像保存（用户实测坏头像）。
    from http.server import BaseHTTPRequestHandler, HTTPServer

    _tiny_png = card_factory.tiny_png()

    class _AvatarHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/avatar.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_tiny_png)))
                self.end_headers()
                self.wfile.write(_tiny_png)
                return
            if self.path == "/note.txt":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"hello")
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    _avatar_srv = HTTPServer(("127.0.0.1", 0), _AvatarHandler)
    _avatar_port = _avatar_srv.server_address[1]
    threading.Thread(target=_avatar_srv.serve_forever, daemon=True).start()
    try:
        # 1) avatar 为远程 URL：下载并按图片魔数验证
        chub_card = card_factory.card_v2(
            "Chub 角色",
            avatar="http://127.0.0.1:%d/avatar.png" % _avatar_port,
            creator_notes='</div><div><style>body::before{content:""}</style>备注',
            extensions={"chub": {"id": 123456, "full_path": "someone/raven"}},
        )
        chub_loaded = load_card_bytes(
            "chub.json", json.dumps(chub_card, ensure_ascii=False).encode("utf-8")
        )
        c.check(
            "Chub 卡片：avatar 为远程 URL 时下载头像（魔数验证）",
            chub_loaded.name == "Chub 角色"
            and chub_loaded.avatar_bytes == _tiny_png
            and chub_loaded.avatar_suffix == ".png",
            "avatar=%s" % (len(chub_loaded.avatar_bytes) if chub_loaded.avatar_bytes else 0),
        )
        c.check(
            "Chub 卡片：非标准字段（extensions.chub / HTML 备注）不破坏解析",
            chub_loaded.creator_notes.startswith("</div>")
            and (chub_loaded.raw.get("data") or {}).get("extensions", {}).get("chub", {}).get("id") == 123456,
            str(chub_loaded.raw.get("data", {}).get("extensions"))[:80],
        )
        # Chub 卡常见只写「描述 + 开场白」，其余核心字段为空——导入后要能报出
        # 哪些字段是卡片本身没写的（空框提示，用户实测误以为解析丢了内容）
        sparse_card = card_factory.card_v2(
            "Chub 空字段",
            personality="",
            scenario="",
            mes_example="",
            system_prompt="",
        )
        sparse_loaded = load_card_bytes("sparse.json", json.dumps(sparse_card, ensure_ascii=False).encode("utf-8"))
        c.check(
            "报告卡片本身未写的核心字段",
            sparse_loaded.missing_core_fields() == ["性格", "场景", "示例对话", "系统指令"],
            str(sparse_loaded.missing_core_fields()),
        )
        c.check(
            "字段齐全的卡片不报缺失",
            chub_loaded.missing_core_fields() == [],
            str(chub_loaded.missing_core_fields()),
        )
        # 2) avatar 为普通文本（既非 URL 也非合法 base64）：不留垃圾字节
        text_avatar = card_factory.card_v2("文本头像", avatar="just some note, not base64")
        text_loaded = load_card_bytes("ta.json", json.dumps(text_avatar).encode("utf-8"))
        c.check(
            "avatar 为普通文本时不产生垃圾头像字节",
            text_loaded.avatar_bytes is None,
            "avatar=%r" % (text_loaded.avatar_bytes[:8] if text_loaded.avatar_bytes else None),
        )
        # 3) avatar 为 data URL（原有能力保持）
        data_avatar = card_factory.card_v2(
            "数据头像",
            avatar="data:image/png;base64,%s" % base64.b64encode(_tiny_png).decode("ascii"),
        )
        data_loaded = load_card_bytes("da.json", json.dumps(data_avatar).encode("utf-8"))
        c.check(
            "avatar 为 data URL 时正确解码",
            data_loaded.avatar_bytes == _tiny_png,
            "",
        )
        # 4) avatar 为 URL 但返回非图片内容：丢弃而不是存垃圾
        url_text_avatar = card_factory.card_v2(
            "坏头像", avatar="http://127.0.0.1:%d/note.txt" % _avatar_port
        )
        bad_loaded = load_card_bytes("ba.json", json.dumps(url_text_avatar).encode("utf-8"))
        c.check(
            "avatar URL 返回非图片时丢弃（不存垃圾字节）",
            bad_loaded.avatar_bytes is None,
            "avatar=%r" % (bad_loaded.avatar_bytes[:8] if bad_loaded.avatar_bytes else None),
        )
    finally:
        _avatar_srv.shutdown()

    # 5) PNG 卡的 chara 值写成 URL-safe base64（- 与 _ 替代 + 与 /）
    _card_data = card_factory.card_v2("urlsafe 角色")
    _card_json = json.dumps(_card_data, ensure_ascii=False).encode("utf-8")
    _urlsafe_b64 = base64.urlsafe_b64encode(_card_json)
    _urlsafe_png = (
        card_factory.PNG_SIGNATURE
        + card_factory._chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + card_factory._chunk(b"tEXt", b"chara\x00" + _urlsafe_b64)
        + card_factory._chunk(b"IEND", b"")
    )
    urlsafe_loaded = load_card_bytes("us.png", _urlsafe_png)
    c.check("PNG 卡 chara 为 URL-safe base64 时能解析", urlsafe_loaded.name == "urlsafe 角色", urlsafe_loaded.name)

    # 6) 个别工具把明文 JSON 直接写进 tEXt（不 base64）
    _plain_png = (
        card_factory.PNG_SIGNATURE
        + card_factory._chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + card_factory._chunk(b"tEXt", b"chara\x00" + _card_json)
        + card_factory._chunk(b"IEND", b"")
    )
    plain_loaded = load_card_bytes("plain.png", _plain_png)
    c.check("PNG 卡 chara 为明文 JSON 时能解析", plain_loaded.name == "urlsafe 角色", plain_loaded.name)

    # ---------------------------------------------------------- 触发规则
    class _Cfg:
        def __init__(self, data: Dict[str, Any]):
            self.data = data

        def get(self, path: str, default: Any = None) -> Any:
            node: Any = self.data
            for part in path.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return default
            return node

    config = _Cfg(
        {
            "proactive": {
                "enabled": True,
                "probability": 1.0,
                "global_daily_limit": 2,
                "per_character_daily_limit": 1,
                "min_interval_minutes": 60,
                "avoid_repeat": True,
                "idle_hours": 6,
                "active_hours": {"enabled": True, "start": "08:00", "end": "23:00"},
                "dnd_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
                "random_min_interval_minutes": 60,
                "random_max_interval_minutes": 120,
            }
        }
    )

    import datetime as dt

    c.check(
        "跨零点时段判断（23:30 落在 23:00-08:00）",
        triggers.check_dnd(config, dt.datetime(2024, 1, 1, 23, 30)).allowed is False,
    )
    c.check(
        "跨零点时段判断（12:00 不在免打扰）",
        triggers.check_dnd(config, dt.datetime(2024, 1, 1, 12, 0)).allowed is True,
        triggers.check_dnd(config, dt.datetime(2024, 1, 1, 12, 0)).reason,
    )
    c.check(
        "活跃时段外拒绝发送",
        triggers.check_active_hours(config, dt.datetime(2024, 1, 1, 3, 0)).allowed is False,
    )
    c.check(
        "活跃时段内允许发送",
        triggers.check_active_hours(config, dt.datetime(2024, 1, 1, 10, 0)).allowed is True,
    )
    c.check("全局上限生效", triggers.check_global_limit(config, 2).allowed is False)
    c.check(
        "最小间隔生效",
        triggers.check_min_interval(config, dt.datetime.now().isoformat(timespec="seconds")).allowed
        is False,
    )
    c.check(
        "空闲触发：刚说过话应被拒绝",
        triggers.check_idle(config, dt.datetime.now().isoformat(timespec="seconds")).allowed is False,
    )
    c.check(
        "空闲触发：超过 6 小时应允许",
        triggers.check_idle(
            config, (dt.datetime.now() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
        ).allowed
        is True,
    )
    c.check("主动消息总开关生效", triggers.check_proactive_enabled(_Cfg({"proactive": {"enabled": False}})).allowed is False)
    c.check("概率为 1 时允许发送", triggers.check_probability(config).allowed is True)
    c.check("概率为 0 时拒绝发送", triggers.check_probability(_Cfg({"proactive": {"probability": 0.0}})).allowed is False)
    c.check("强制发送时跳过概率判定", triggers.check_probability(_Cfg({"proactive": {"probability": 0.0}}), force=True).allowed is True)

    candidates = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}, {"id": "c", "name": "C"}]
    filtered = triggers.filter_candidates(candidates, config, {"a": 1}, "b")
    c.check(
        "避免连续同一角色 + 单角色上限过滤",
        [item["id"] for item in filtered] == ["c"],
        str([item["id"] for item in filtered]),
    )
    later = triggers.next_active_datetime(config, dt.datetime(2024, 1, 1, 3, 0))
    c.check("随机时间被校正进活跃时段", later.hour == 8, later.isoformat())
    nxt = triggers.next_scheduled_time(["09:00", "21:00"], dt.datetime(2024, 1, 1, 10, 0))
    c.check("下一个定时时间计算正确", nxt is not None and nxt.hour == 21, str(nxt))
    random_when = triggers.next_random_time(config, dt.datetime(2024, 1, 1, 10, 0))
    c.check(
        "随机触发时间落在配置区间之后的活跃时段内",
        8 <= random_when.hour <= 23 and random_when > dt.datetime(2024, 1, 1, 10, 0),
        random_when.isoformat(),
    )

    # ---------------------------------------------------------- 文本处理
    # 这三个函数原先住在已删除的 bot/qq_adapter/sender.py，现在归 common/text.py
    segments = split_message("第一句话。" * 30, max_len=60, max_segments=3)
    c.check("长消息拆分为多条", 1 < len(segments) <= 3, str(len(segments)))
    c.check("拆分后每条不超过上限", all(len(item) <= 80 for item in segments))
    cleaned = sanitize_text('角色名：你好呀 [CQ:at,qq=1] "', character_name="角色名")
    c.check("清理角色名前缀", not cleaned.startswith("角色名"))
    c.check("转义 CQ 码", "[CQ:at" not in cleaned, cleaned)
    cleaned_md = sanitize_text("你好 ![](https://files.kammii.org/8208954494024Kz2/x.webp) 世界")
    c.check("清理 Markdown 图片链接", "![](" not in cleaned_md and "kammii" not in cleaned_md, cleaned_md)
    c.check("打字延迟有上限", typing_delay("a" * 500, cps=10, max_delay=5) <= 5.0)

    # ---------------------------------------------------------- 提示词
    from bot.ai_engine.prompt_builder import build_proactive_messages, build_system_prompt

    system_prompt = build_system_prompt(
        {"name": "深夜角色", "description": "{{char}} 喜欢 {{user}}", "personality": "温柔"},
        user_name="小可爱",
        memories_text="- 用户喜欢深夜写代码",
    )
    c.check("提示词替换 {{char}}", "深夜角色" in system_prompt and "{{char}}" not in system_prompt)
    c.check("提示词替换 {{user}}", "小可爱" in system_prompt and "{{user}}" not in system_prompt)
    c.check("提示词包含长期记忆", "喜欢深夜写代码" in system_prompt)
    # Chub 卡片会把整张展示页 HTML 塞进 creator_notes（V2 规范：不进 prompt）
    html_notes = (
        '<div style="max-width: 100%;"><style>body::before{content:""}</style>'
        '<span>SubscribeStar promo</span>' + "<p>filler text </p>" * 200
    )
    chub_prompt = build_system_prompt(
        {"name": "Raven", "description": "Raven is your younger sister.", "creator_notes": html_notes},
    )
    c.check("HTML 版 creator_notes 不进提示词", "<style>" not in chub_prompt and "SubscribeStar" not in chub_prompt)
    c.check("HTML 版 creator_notes 不吞描述", "younger sister" in chub_prompt)
    plain_prompt = build_system_prompt(
        {"name": "小栖", "description": "描述内容", "creator_notes": "适合深夜陪伴、情绪低落时需要有人在的场景。"},
    )
    c.check("短纯文本 creator_notes 保留在提示词", "适合深夜陪伴" in plain_prompt)
    proactive_messages = build_proactive_messages(
        {"name": "深夜角色", "description": "测试"}, config, [], []
    )
    c.check(
        "主动消息携带隐藏指令",
        any("主动" in item["content"] for item in proactive_messages if item["role"] == "user"),
    )

    # ---------------------------------------------------------- 记忆与数据库
    from bot.database import Database, crud
    from bot.memory.long_term import LongTermMemory

    memory = LongTermMemory(None, 5, True)  # type: ignore[arg-type]
    extracted = memory.extract_candidates("我叫小明，我喜欢在深夜写代码。今天天气不错。")
    c.check("抽取值得记住的句子", any("深夜写代码" in item for item in extracted), str(extracted))

    async def _db_checks() -> None:
        db = Database(tmp / "unit.db")
        await db.connect()
        row = await crud.insert_character(
            db, {"id": "abc", "name": "测试角色", "description": "描述", "enabled": 1}
        )
        c.check("数据库写入角色", row.get("name") == "测试角色")
        await crud.add_message(db, "abc", "user", "你好")
        await crud.add_message(db, "abc", "assistant", "在的")
        rows = await crud.recent_messages(db, "abc", 10)
        c.check("数据库读取消息顺序正确", [item["role"] for item in rows] == ["user", "assistant"])
        await crud.log_proactive(db, "abc", "manual", "测试主动消息", "sent")
        c.check("今日主动消息计数", await crud.proactive_count_today(db) == 1)
        stats = await crud.stats_today(db)
        c.check("今日统计包含角色维度", stats["proactive_by_character"][0]["count"] == 1)
        # openid 记忆是官方通道发主动消息的前提，走 bot_state / settings 两张表
        await crud.set_bot_state(db, "bot1", "official.last_user_openid", "openid-xyz")
        c.check(
            "机器人状态表能记住 openid",
            await crud.get_bot_state(db, "bot1", "official.last_user_openid", "") == "openid-xyz",
        )
        await db.close()

    asyncio.run(_db_checks())

    # ---------------------------------------------------------- 配置
    from common.config import LEGACY_QQ_KEYS, ConfigManager

    manager = ConfigManager(tmp / "config.yaml")
    manager.load(force=True)
    manager.patch({"llm": {"model": "test-model"}, "proactive": {"probability": 0.42}})
    reloaded = ConfigManager(tmp / "config.yaml")
    reloaded.load(force=True)
    c.check("配置持久化与默认值合并", reloaded.get("llm.model") == "test-model")
    c.check("配置深层字段保留默认值", reloaded.get("proactive.global_daily_limit") == 10)
    c.check("配置概率写入正确", abs(float(reloaded.get("proactive.probability")) - 0.42) < 1e-6)
    c.check(
        "qq 段的官方凭据字段完整",
        all(
            key in (reloaded.get("qq.official", {}) or {})
            for key in ("app_id", "app_secret", "sandbox", "intents", "target_openid", "group_openid")
        ),
        str(sorted((reloaded.get("qq.official", {}) or {}).keys())),
    )

    # 历史遗留字段：旧版 NapCat / OneBot 的键必须被自动清掉并留 .bak
    legacy_path = tmp / "legacy.yaml"
    legacy_path.write_text(
        "qq:\n"
        "  mode: napcat\n"
        "  napcat_api_url: http://127.0.0.1:3000\n"
        "  access_token: secret\n"
        "  self_id: 20002\n"
        "  target_user_id: 10001\n"
        "  reply_max_segments: 3\n"
        "  official:\n"
        "    app_id: keep-me\n"
        "napcat:\n"
        "  dir: D:/napcat\n"
        "onebot:\n"
        "  path: /onebot/v11/http\n"
        "app:\n"
        "  start_napcat_on_launch: true\n"
        "  theme: dark\n",
        encoding="utf-8",
    )
    legacy = ConfigManager(legacy_path)
    legacy.load(force=True)
    cleaned_text = legacy_path.read_text(encoding="utf-8")
    c.check(
        "升级时自动清掉 NapCat / OneBot 遗留字段",
        all(key not in cleaned_text for key in LEGACY_QQ_KEYS)
        and "napcat:" not in cleaned_text
        and "onebot:" not in cleaned_text
        and "start_napcat_on_launch" not in cleaned_text,
        cleaned_text[:200],
    )
    c.check("清理前保留一份 .bak 备份", legacy_path.with_suffix(".yaml.bak").is_file())
    c.check("清理时保留官方凭据与其它配置", legacy.get("qq.official.app_id") == "keep-me")
    c.check("清理后 mode 字段不再出现在配置里", legacy.get("qq.mode", "MISSING") == "MISSING")

    # ---------------------------------------------------------- 多机器人配置解析
    from common.bots import MODE_OFFICIAL, bot_specs

    multi_path = tmp / "multi.yaml"
    multi_path.write_text(
        "qq:\n"
        "  id: bot1\n"
        "  name: 主机器人\n"
        "  character_id: charA\n"
        "  official:\n"
        "    app_id: app-1\n"
        "bots:\n"
        "  - id: bot2\n"
        "    name: 二号机器人\n"
        "    character_id: charB\n"
        "    official:\n"
        "      app_id: app-2\n"
        "      sandbox: true\n",
        encoding="utf-8",
    )
    multi = ConfigManager(multi_path)
    multi.load(force=True)
    specs = bot_specs(multi)
    c.check("多机器人配置能解析出 2 个机器人", len(specs) == 2, str(len(specs)))
    c.check(
        "第 1 个机器人来自 qq: 段，第 2 个来自 bots: 段",
        [item.id for item in specs] == ["bot1", "bot2"] and [item.name for item in specs] == ["主机器人", "二号机器人"],
        str([(item.id, item.name) for item in specs]),
    )
    c.check(
        "每个机器人的官方凭据与绑定角色互不干扰",
        specs[0].official("app_id") == "app-1"
        and specs[1].official("app_id") == "app-2"
        and specs[0].character_id == "charA"
        and specs[1].character_id == "charB",
        str([(item.official("app_id"), item.character_id) for item in specs]),
    )
    c.check(
        "机器人的连接方式只有官方一种",
        all(item.mode == MODE_OFFICIAL and item.is_official for item in specs),
        str([item.mode for item in specs]),
    )

    # ---------------------------------------------------------- LLM 预设与模型拉取
    from app.llm_check import filter_chat_models
    from app.llm_presets import CUSTOM_LABEL, PRESETS, by_base_url, by_label, labels

    c.check("内置了常用 LLM 服务商预设", len(PRESETS) >= 10, str(len(PRESETS)))
    c.check("下拉框包含「自定义 / 其他」", CUSTOM_LABEL in labels())
    c.check(
        "预设地址都是 http(s) 开头",
        all(item.base_url.startswith("http") for item in PRESETS),
    )
    c.check(
        "预设名称唯一（避免下拉框重名）",
        len({item.name for item in PRESETS}) == len(PRESETS),
    )
    c.check(
        "选择服务商能填入正确地址",
        by_label("DeepSeek 官方") is not None
        and by_label("DeepSeek 官方").base_url == "https://api.deepseek.com/v1",
    )
    c.check(
        "能按已存地址反查服务商",
        (by_base_url("https://api.deepseek.com/v1/") or None) is not None,
    )
    c.check("未知地址不匹配任何预设", by_base_url("http://example.com/v1") is None)
    filtered = filter_chat_models(
        ["gpt-4o-mini", "text-embedding-3-small", "whisper-1", "bge-large-zh"]
    )
    c.check("模型列表会过滤非对话模型", filtered == ["gpt-4o-mini"], str(filtered))

    # ---------------------------------------------------------- 官方通道单元项
    from bot.chat_router import strip_mentions
    from bot.qq_official.client import OfficialQQClient, describe_error, segments_for_official
    from bot.qq_official.gateway import INTENT_GROUP_AND_C2C
    from bot.qq_official.receiver import parse_event
    from common.config import DEFAULTS

    c.check("官方群聊与单聊 intent 为 1<<25", INTENT_GROUP_AND_C2C == OFFICIAL_INTENTS, str(INTENT_GROUP_AND_C2C))
    c.check(
        "默认配置里没有 mode 字段（连接方式只剩官方一种）",
        "mode" not in DEFAULTS["qq"],
        str(sorted(DEFAULTS["qq"].keys())),
    )
    c.check(
        "默认配置里已移除 NapCat / OneBot 字段",
        all(key not in DEFAULTS["qq"] for key in LEGACY_QQ_KEYS),
        str([key for key in LEGACY_QQ_KEYS if key in DEFAULTS["qq"]]),
    )

    c2c = parse_event(card_factory.official_c2c_event("你好呀", openid="openid-abc", message_id="msg-1"))
    c.check("能解析单聊事件", c2c is not None and c2c.peer_id == "openid-abc", str(c2c))
    c.check("单聊事件带上 msg_id（被动回复要用）", c2c is not None and c2c.message_id == "msg-1")
    c.check("单聊事件被标记为私聊", c2c is not None and c2c.is_group is False)
    c.check("单聊事件的会话 key 带 official 来源", c2c is not None and c2c.key() == "official:private:openid-abc", str(c2c and c2c.key()))

    group = parse_event(
        card_factory.official_group_at_event("在吗", group_openid="group-xyz", member_openid="member-1")
    )
    c.check(
        "能解析群聊 @ 事件并剥离 @ 占位",
        group is not None and group.is_group and group.group_id == "group-xyz" and group.text == "在吗",
        "%s / %r" % (getattr(group, "group_id", ""), getattr(group, "text", "")),
    )
    c.check("非消息事件被忽略", parse_event({"type": "READY", "data": {}}) is None)
    c.check(
        "缺少 openid 的事件被忽略",
        parse_event({"type": "C2C_MESSAGE_CREATE", "data": {"content": "hi"}}) is None,
    )
    c.check(
        "会话 key 区分来源与群聊",
        group.key() == "official:group:group-xyz:member-1",
        group.key(),
    )
    c.check("strip_mentions 能处理多种 @ 形式", strip_mentions("<@!123> hi <@456>") == "hi")

    # 多机器人群：全量群消息（GROUP_MESSAGE_CREATE）要精确判断 @ 的是不是本机器人
    _full_self = parse_event(
        {
            "type": "GROUP_MESSAGE_CREATE",
            "data": {
                "id": "msg-full-1",
                "content": "<@app-a> 你好",
                "group_openid": "group-xyz",
                "author": {"member_openid": "member-1"},
            },
        },
        self_id="app-a",
    )
    c.check(
        "全量群消息里 @ 占位匹配自己的 AppID → 判定为 @ 了自己",
        _full_self is not None and _full_self.mentioned is True and _full_self.any_mentioned is True,
        str(_full_self),
    )
    c.check(
        "@ 了别的机器人的全量消息 → 判定为没有 @ 自己（但 any_mentioned 为真）",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-2",
                    "content": "<@app-b> 你好",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                },
            },
            self_id="app-a",
        ).mentioned
        is False
        and parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-2b",
                    "content": "<@app-b> 你好",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                },
            },
            self_id="app-a",
        ).any_mentioned
        is True,
        "",
    )
    c.check(
        "mentions 字段匹配自己的 AppID 也算 @ 了自己",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-3",
                    "content": "你好呀",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                    "mentions": [{"id": "app-a", "bot": True}],
                },
            },
            self_id="app-a",
        ).mentioned
        is True,
        "",
    )
    c.check(
        "没有 @ 任何人的全量群消息 → mentioned / any_mentioned 都是假",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-4",
                    "content": "普通聊天",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                },
            },
            self_id="app-a",
        ).mentioned
        is False
        and parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-4b",
                    "content": "普通聊天",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                },
            },
            self_id="app-a",
        ).any_mentioned
        is False,
        "",
    )
    c.check(
        "@ 事件（GROUP_AT_MESSAGE_CREATE）仍然恒为 @ 了自己",
        parse_event(
            {
                "type": "GROUP_AT_MESSAGE_CREATE",
                "data": {
                    "id": "msg-at-1",
                    "content": "在吗",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                },
            },
            self_id="app-a",
        ).mentioned
        is True,
        "",
    )

    # V0.2.2：平台 2026-09 起群 @ 消息 content 已去掉 @ 前缀，mentions 带 is_you /
    # 多种身份字段 —— 全量模式下必须靠这些字段识别「@ 的是不是我」
    c.check(
        "全量群消息 mentions 里 is_you=True → 判定为 @ 了自己（新版平台格式，content 无 @ 占位）",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-5",
                    "content": "你好呀",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                    "mentions": [{"id": "openid-bot-a", "bot": True, "is_you": True}],
                },
            },
            self_id="app-a",
        ).mentioned
        is True,
        "",
    )
    c.check(
        "全量群消息 mentions 的 user_openid 匹配自己的身份 → 判定为 @ 了自己",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-6",
                    "content": "在吗",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                    "mentions": [{"id": "other-format-id", "bot": True, "user_openid": "app-a"}],
                },
            },
            self_id="app-a",
        ).mentioned
        is True,
        "",
    )
    c.check(
        "多身份（AppID + READY openid）任一匹配都算 @ 了自己",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-7",
                    "content": "在吗",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                    "mentions": [{"id": "openid-from-ready", "bot": True}],
                },
            },
            self_id="app-a",
            self_ids=["app-a", "openid-from-ready"],
        ).mentioned
        is True,
        "",
    )
    c.check(
        "全量群消息 @ 了别的机器人（is_you 缺失 / 身份不匹配）→ 让路不回复",
        parse_event(
            {
                "type": "GROUP_MESSAGE_CREATE",
                "data": {
                    "id": "msg-full-8",
                    "content": "你好",
                    "group_openid": "group-xyz",
                    "author": {"member_openid": "member-1"},
                    "mentions": [{"id": "openid-bot-b", "bot": True, "is_you": False}],
                },
            },
            self_id="app-a",
        ).mentioned
        is False,
        "",
    )

    # V0.2.2 回归修复：同一 msg_id 的 @ 事件与全量事件先后到达（顺序不保证），
    # 去重不能把「先让路的全量事件」后到的 @ 事件吃掉，否则群 @ 彻底不回复
    from bot.qq_official.receiver import OfficialReceiver

    _dedupe_receiver = OfficialReceiver(runtime=None, bot=None)
    c.check(
        "先到的全量事件没 @ 自己（让路）→ 后到的 @ 事件仍要处理",
        _dedupe_receiver._check_duplicate("dup-1", mentioned=False) is False
        and _dedupe_receiver._check_duplicate("dup-1", mentioned=True) is False,
        str(_dedupe_receiver._seen_messages),
    )
    c.check(
        "标记已回复后，同 msg_id 的再推送一律忽略",
        (_dedupe_receiver._mark_replied("dup-2"), _dedupe_receiver._check_duplicate("dup-2", mentioned=True) is True)[1],
        str(_dedupe_receiver._seen_messages),
    )
    c.check(
        "先到的事件就是 @ 自己（标记回复中）→ 同 msg_id 重推忽略",
        _dedupe_receiver._check_duplicate("dup-3", mentioned=True) is False
        and _dedupe_receiver._check_duplicate("dup-3", mentioned=True) is True,
        str(_dedupe_receiver._seen_messages),
    )
    c.check(
        "让路过两次的全量重推 → 第二次仍忽略",
        _dedupe_receiver._check_duplicate("dup-4", mentioned=False) is False
        and _dedupe_receiver._check_duplicate("dup-4", mentioned=False) is True,
        str(_dedupe_receiver._seen_messages),
    )

    # V0.2.2：按机器人覆盖的生效段（全局 + 该机器人条目按键覆盖）
    from common.bots import BotSpec, effective_section
    from common.config import ConfigManager

    _cfg = ConfigManager.__new__(ConfigManager)
    _cfg.path = Path("<memory>:config.yaml")
    _cfg._data = {
        "proactive": {"enabled": True, "global_daily_limit": 10, "probability": 0.7},
        "media": {"voice_reply_probability": 0.05, "image_style": "auto"},
        "qq": {"id": "bot1", "name": "主号", "enabled": True, "character_id": ""},
        "bots": [
            {"id": "bot2", "name": "二号", "enabled": True, "character_id": "", "proactive": {"global_daily_limit": 3}},
        ],
    }
    _specs = [
        BotSpec(0, "qq", {"id": "bot1", "name": "主号", "enabled": True, "character_id": ""}),
        BotSpec(1, "bots.0", {"id": "bot2", "name": "二号", "enabled": True, "character_id": "", "proactive": {"global_daily_limit": 3}}),
    ]
    c.check(
        "生效段：机器人条目没覆盖的键回落到全局值",
        effective_section(_cfg, _specs[1], "proactive").get("enabled") is True
        and effective_section(_cfg, _specs[1], "proactive").get("probability") == 0.7,
        str(effective_section(_cfg, _specs[1], "proactive")),
    )
    c.check(
        "生效段：机器人条目覆盖的键用机器人自己的值",
        effective_section(_cfg, _specs[1], "proactive").get("global_daily_limit") == 3,
        str(effective_section(_cfg, _specs[1], "proactive")),
    )
    c.check(
        "生效段：无覆盖的机器人拿到完整全局段",
        effective_section(_cfg, _specs[0], "media") == {"voice_reply_probability": 0.05, "image_style": "auto"},
        str(effective_section(_cfg, _specs[0], "media")),
    )

    # V0.2.2：桌面快捷方式「有时候创建不出来」——桌面被迁移到 OneDrive 时
    # 硬编码 %USERPROFILE%\Desktop 是错的目录；兜底 .lnk 写完后读回验证
    # 又被杀软拦住时会把刚写好的文件删掉。两个修法都在这里验证
    from installer import common as _ic

    _real_shell_folders = _ic._user_shell_folders
    try:
        _ic._user_shell_folders = lambda: {"Desktop": r"%USERPROFILE%\OneDrive\Desktop"}
        _one = _ic.desktop_dir()
        c.check(
            "桌面目录按注册表 Known Folders 解析（OneDrive 重定向时落在真实桌面）",
            str(_one) == str(Path(os.environ.get("USERPROFILE") or Path.home()) / "OneDrive" / "Desktop"),
            str(_one),
        )
        _ic._user_shell_folders = lambda: {}
        _fallback = _ic.desktop_dir()
        c.check(
            "注册表没有覆盖时回落到 %USERPROFILE%\\Desktop",
            str(_fallback) == str(Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"),
            str(_fallback),
        )
    finally:
        _ic._user_shell_folders = _real_shell_folders

    import tempfile as _tempfile

    _lnk_dir = Path(_tempfile.mkdtemp(prefix="tavern-lnk-"))
    _lnk_file = _lnk_dir / "test.lnk"
    _ok_target = _lnk_dir / "fake.exe"
    _ok_target.write_bytes(b"MZ")
    c.check(
        "兜底写出的 .lnk 文件头魔数校验通过（COM 读回被杀软拦截时也能保留文件）",
        _ic.write_lnk(_lnk_file, _ok_target) and _ic._lnk_looks_valid(_lnk_file),
        str(_lnk_file),
    )
    _bad_file = _lnk_dir / "bad.lnk"
    _bad_file.write_bytes(b"\x00\x01\x02\x03" * 8)
    c.check("文件头损坏的 .lnk 被识别为无效（会删掉半成品）", not _ic._lnk_looks_valid(_bad_file), "")
    _trunc_file = _lnk_dir / "trunc.lnk"
    _trunc_file.write_bytes(_ic.build_lnk_bytes(_ok_target)[:12])
    c.check("截断的 .lnk 被识别为无效", not _ic._lnk_looks_valid(_trunc_file), "")
    _missing_file = _lnk_dir / "missing.lnk"
    c.check("不存在的文件按无效处理", not _ic._lnk_looks_valid(_missing_file), "")

    c.check(
        "官方错误码有可读提示（AppID/AppSecret 不正确）",
        "100016" in describe_error({"code": 100016, "message": "invalid appid or secret"})
        and "AppSecret" in describe_error({"code": 100016, "message": "invalid appid or secret"}),
        describe_error({"code": 100016, "message": "invalid appid or secret"}),
    )
    c.check(
        "AppID 无效的错误码有可读提示",
        "100007" in describe_error({"code": 100007, "message": "appid invalid"}),
        describe_error({"code": 100007, "message": "appid invalid"}),
    )
    c.check(
        "主动消息额度的错误也有提示",
        "上限" in describe_error({"code": 22009, "message": "reach limit"}),
        describe_error({"code": 22009, "message": "reach limit"}),
    )
    c.check("未配置凭据时官方客户端判定为不可用", OfficialQQClient().configured() is False)
    c.check(
        "配置了 AppID/AppSecret 即视为可用",
        OfficialQQClient("app", "secret").configured() is True,
    )
    c.check(
        "沙盒模式会切换域名",
        OfficialQQClient("a", "b", sandbox=True).api_domain.startswith("https://sandbox."),
        OfficialQQClient("a", "b", sandbox=True).api_domain,
    )
    c.check(
        "自定义域名不会被沙盒开关改写",
        OfficialQQClient("a", "b", api_domain="http://127.0.0.1:3100", sandbox=True).api_domain
        == "http://127.0.0.1:3100",
        OfficialQQClient("a", "b", api_domain="http://127.0.0.1:3100", sandbox=True).api_domain,
    )
    c.check(
        "发送前会清理并拆分文本",
        segments_for_official("角色名：呜……" + "好长的一句话。" * 40, max_len=60, max_segments=3)[:1][0]
        and len(segments_for_official("好长的一句话。" * 40, max_len=60, max_segments=3)) <= 3,
    )

    # ---------------------------------------------------------- 模型槽位 / 主模型整合
    from common.providers import (
        ENGINE_OPENAI,
        ProviderSpec,
        SLOT_CHAT,
        load_slot,
    )
    from common.config import strip_legacy_keys

    # 主模型整合：llm 段已配置时以 llm 段为准（providers.chat 只作旧配置兜底）
    _cfg_llm = {
        "llm.base_url": "https://api.deepseek.com/v1",
        "llm.api_key": "sk-llm",
        "llm.model": "deepseek-chat",
        "providers.chat": {
            "base_url": "https://old.example.com/v1",
            "api_key": "sk-old",
            "model": "old-model",
        },
    }
    chat_spec = load_slot(_cfg_llm, SLOT_CHAT)
    c.check(
        "主模型整合：llm 段已配置时以 llm 段为准（不再读 providers.chat）",
        chat_spec.base_url == "https://api.deepseek.com/v1"
        and chat_spec.api_key == "sk-llm"
        and chat_spec.model == "deepseek-chat",
        chat_spec.describe(),
    )
    # llm 段未配置 → 回退 providers.chat（老配置零迁移）
    _cfg_legacy = {
        "llm.base_url": "",
        "llm.api_key": "",
        "llm.model": "",
        "providers.chat": {
            "base_url": "https://old.example.com/v1",
            "api_key": "sk-old",
            "model": "old-model",
        },
    }
    legacy_chat = load_slot(_cfg_legacy, SLOT_CHAT)
    c.check(
        "主模型整合：llm 段未配置时回退 providers.chat（老配置兼容）",
        legacy_chat.base_url == "https://old.example.com/v1" and legacy_chat.model == "old-model",
        legacy_chat.describe(),
    )
    c.check("chat 槽位引擎仍是 openai 兼容", chat_spec.engine == ENGINE_OPENAI)

    # ASR 槽位已删除：语音转文字直接用 QQ 官方平台随消息推送的参考转写
    try:
        load_slot({}, "asr")
        c.check("asr 不再是有效槽位（语音转文字零配置）", False, "未抛出异常")
    except ValueError:
        c.check("asr 不再是有效槽位（语音转文字零配置）", True)
    _cleaned, _removed = strip_legacy_keys({"providers": {"asr": {"engine": "local"}, "tts": {"engine": "edge-tts"}}})
    c.check(
        "旧配置的 providers.asr 段启动时自动清理",
        "providers.asr" in _removed and "asr" not in (_cleaned.get("providers") or {}),
        str(_removed),
    )
    c.check(
        "tts 槽位不接受 local 引擎（缺省回退到推荐引擎 dashscope）",
        load_slot({"providers.tts": {"engine": "local"}}, "tts").engine == "dashscope",
    )
    from common.providers import SLOT_DEFAULT_ENGINE, SLOT_ENGINES, SLOT_TTS

    c.check(
        "TTS 缺省引擎为推荐引擎 dashscope，且排在引擎列表首位（界面默认选中）",
        SLOT_DEFAULT_ENGINE[SLOT_TTS] == "dashscope"
        and SLOT_ENGINES[SLOT_TTS][0] == "dashscope",
        str(SLOT_ENGINES[SLOT_TTS]),
    )

    # ProviderSpec 判定
    c.check(
        "远程 openai 端点没有 Key 不算已配置",
        ProviderSpec(slot="vision", engine=ENGINE_OPENAI, base_url="https://api.example.com/v1").configured is False,
    )
    c.check(
        "本地 openai 端点没有 Key 也算已配置",
        ProviderSpec(slot="vision", engine=ENGINE_OPENAI, base_url="http://127.0.0.1:9999/v1").configured is True,
    )

    # 测试线路：表单当前值叠在已保存配置上（未点保存也能按界面所见测试）
    from bot.api import _SlotOverrideConfig

    class _FakeCfg:
        def get(self, key, default=None):
            data = {
                "providers.vision": {
                    "base_url": "https://saved.example.com/v1",
                    "api_key": "sk-saved",
                    "model": "saved-model",
                },
                "llm.base_url": "https://llm.example.com/v1",
            }
            return data.get(key, default)

    merged = _SlotOverrideConfig(
        _FakeCfg(),
        "vision",
        {"base_url": "", "api_key": "sk-form", "model": "form-model"},
    )
    merged_node = merged.get("providers.vision")
    c.check(
        "测试线路合并表单值：空字段按未填、其余字段覆盖",
        isinstance(merged_node, dict)
        and merged_node.get("api_key") == "sk-form"
        and merged_node.get("model") == "form-model"
        and "base_url" not in merged_node,
        str(merged_node),
    )
    c.check(
        "测试线路合并不影响其他配置键（llm 段透传）",
        merged.get("llm.base_url") == "https://llm.example.com/v1",
    )

    # 生图：端点对参数挑剔（Gemini OpenAI 兼容层）→ 400 时自动降级参数重试
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from bot.media.images import ImageGenerator

    _PNG_1X1 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    _strict_state = {"calls": 0}

    class _StrictImagesHandler(BaseHTTPRequestHandler):
        """模拟 Gemini 兼容层：size 不支持 → 400；response_format 不支持 → 400；最小参数 → 200。"""

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            _strict_state["calls"] += 1
            if "size" in body:
                self._reply(400, {"error": {"message": "size is not supported"}})
            elif "response_format" in body:
                self._reply(400, {"error": {"message": "response_format is not supported"}})
            else:
                self._reply(200, {"data": [{"b64_json": _PNG_1X1}]})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _strict_srv = HTTPServer(("127.0.0.1", 0), _StrictImagesHandler)
    threading.Thread(target=_strict_srv.serve_forever, daemon=True).start()
    try:
        strict_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _strict_srv.server_address[1],
            api_key="sk-strict",
            model="gemini-2.5-flash-image",
        )
        loop = asyncio.new_event_loop()
        try:
            gen_data, gen_ext = loop.run_until_complete(
                ImageGenerator(strict_spec, timeout=10).generate("测试", size="512x512")
            )
        finally:
            loop.close()
        c.check(
            "Gemini 类端点：400 时自动降级参数（size → response_format）后成功",
            len(gen_data) > 8 and gen_ext == "png" and _strict_state["calls"] == 3,
            "calls=%d bytes=%d" % (_strict_state["calls"], len(gen_data)),
        )
    finally:
        _strict_srv.shutdown()

    # 生图：Gemini 原生接口（/models/{model}:generateContent?key=…，key 走 query，
    # 返回 inlineData base64；新一代 nano banana 2 系列模型只走这条路径）
    from common.providers import ENGINE_GEMINI

    _native_state = {"path": "", "key": "", "modalities": None}

    class _GeminiNativeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            from urllib.parse import parse_qs, urlparse

            parsed = urlparse(self.path)
            _native_state["path"] = parsed.path
            _native_state["key"] = parse_qs(parsed.query).get("key", [""])[0]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            gen = body.get("generationConfig") or {}
            _native_state["modalities"] = gen.get("responseModalities")
            self._reply(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"inlineData": {"mimeType": "image/jpeg", "data": _PNG_1X1}}
                                ]
                            }
                        }
                    ]
                },
            )

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _native_srv = HTTPServer(("127.0.0.1", 0), _GeminiNativeHandler)
    threading.Thread(target=_native_srv.serve_forever, daemon=True).start()
    try:
        native_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_GEMINI,
            base_url="http://127.0.0.1:%d/v1beta" % _native_srv.server_address[1],
            api_key="test-key-123",
            # 故意带 /models 返回的「models/」前缀：请求路径必须剥掉它
            model="models/gemini-3.1-flash-lite-image",
        )
        loop = asyncio.new_event_loop()
        try:
            native_data, native_ext = loop.run_until_complete(
                ImageGenerator(native_spec, timeout=10).generate("一只小猫")
            )
        finally:
            loop.close()
        c.check(
            "Gemini 原生接口：key 走 query、剥 models/ 前缀、出图按 mimeType 定扩展名",
            len(native_data) > 8
            and native_ext == "jpg"
            and _native_state["path"] == "/v1beta/models/gemini-3.1-flash-lite-image:generateContent"
            and _native_state["key"] == "test-key-123"
            and _native_state["modalities"] == ["TEXT", "IMAGE"],
            "path=%s key=%s ext=%s" % (_native_state["path"], _native_state["key"], native_ext),
        )
    finally:
        _native_srv.shutdown()

    # chat 出图 modalities 大小写兜底：Gemini 兼容层现在要求小写，旧层要大写
    _case_state = {"images_calls": 0, "chat_calls": 0}

    class _CaseModalitiesHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/images/generations"):
                _case_state["images_calls"] += 1
                self._reply(400, {"error": {"message": "image generation not available"}})
                return
            if self.path.endswith("/chat/completions"):
                _case_state["chat_calls"] += 1
                if body.get("modalities") == ["text", "image"]:
                    self._reply(
                        400,
                        {"error": {"message": "Invalid modality type, expected one of [text, image, audio]"}},
                    )
                    return
                self._reply(200, {"choices": [{"message": {"images": [{"b64_json": _PNG_1X1}]}}]})
                return
            self._reply(404, {"error": {"message": "not found"}})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _case_srv = HTTPServer(("127.0.0.1", 0), _CaseModalitiesHandler)
    threading.Thread(target=_case_srv.serve_forever, daemon=True).start()
    try:
        case_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _case_srv.server_address[1],
            api_key="sk-case",
            model="gemini-3.1-flash-image",
        )
        loop = asyncio.new_event_loop()
        try:
            case_data, case_ext = loop.run_until_complete(
                ImageGenerator(case_spec, timeout=10).generate("一只小猫")
            )
        finally:
            loop.close()
        c.check(
            "chat 出图 modalities：小写被拒（400 modality）时自动换大写重试",
            len(case_data) > 8 and _case_state["chat_calls"] == 2,
            "chat_calls=%d" % _case_state["chat_calls"],
        )
    finally:
        _case_srv.shutdown()

    # chat 出图 content 格式兜底：有的 OpenAI 兼容层（DeepSeek 风格 Pydantic 校验）
    # 要求 messages[].content 是内容数组，字符串会 400 "Input should be a valid list"
    _list_state = {"images_calls": 0, "chat_calls": 0, "last_content_type": ""}

    class _CaseContentListHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/images/generations"):
                _list_state["images_calls"] += 1
                self._reply(400, {"error": {"message": "image generation not available"}})
                return
            if self.path.endswith("/chat/completions"):
                _list_state["chat_calls"] += 1
                content = (body.get("messages") or [{}])[0].get("content")
                _list_state["last_content_type"] = type(content).__name__
                if isinstance(content, str):
                    self._reply(
                        400,
                        {
                            "error": {
                                "message": "Input should be a valid list: input.messages[0].content",
                                "type": "invalid_request_error",
                                "param": None,
                                "code": "invalid_parameter_error",
                            }
                        },
                    )
                    return
                self._reply(200, {"choices": [{"message": {"images": [{"b64_json": _PNG_1X1}]}}]})
                return
            self._reply(404, {"error": {"message": "not found"}})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _list_srv = HTTPServer(("127.0.0.1", 0), _CaseContentListHandler)
    threading.Thread(target=_list_srv.serve_forever, daemon=True).start()
    try:
        list_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _list_srv.server_address[1],
            api_key="sk-list",
            model="some-image-model",
        )
        loop = asyncio.new_event_loop()
        try:
            list_data, list_ext = loop.run_until_complete(
                ImageGenerator(list_spec, timeout=10).generate("一只小猫")
            )
        finally:
            loop.close()
        c.check(
            "chat 出图：端点要求 content 为内容数组时（400 'valid list'）自动换数组格式重试",
            len(list_data) > 8
            and _list_state["chat_calls"] == 2
            and _list_state["last_content_type"] == "list",
            "chat_calls=%d last_content_type=%s" % (_list_state["chat_calls"], _list_state["last_content_type"]),
        )
    finally:
        _list_srv.shutdown()

    # chat 出图返回形状兜底：部分端点在 chat 接口直接返回 images/generations 形状
    # （顶层 data[]），或把 b64 放在非标准键里；都认不出的错误要带响应体便于排查
    _shape_state = {"calls": 0}
    _shape_payload = {"data": [{"b64_json": _PNG_1X1}]}

    class _CaseShapeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if self.path.endswith("/images/generations"):
                self._reply(400, {"error": {"message": "image generation not available"}})
                return
            if self.path.endswith("/chat/completions"):
                _shape_state["calls"] += 1
                self._reply(200, _shape_payload)
                return
            self._reply(404, {"error": {"message": "not found"}})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _shape_srv = HTTPServer(("127.0.0.1", 0), _CaseShapeHandler)
    threading.Thread(target=_shape_srv.serve_forever, daemon=True).start()
    _shape_spec = ProviderSpec(
        slot="image",
        engine=ENGINE_OPENAI,
        base_url="http://127.0.0.1:%d/v1" % _shape_srv.server_address[1],
        api_key="sk-shape",
        model="some-image-model",
    )
    try:
        loop = asyncio.new_event_loop()
        try:
            shape_data, _shape_ext = loop.run_until_complete(
                ImageGenerator(_shape_spec, timeout=10).generate("一只小猫")
            )
        finally:
            loop.close()
        c.check(
            "chat 出图：端点在 chat 接口返回顶层 data[]（images/generations 形状）时能取出图",
            len(shape_data) > 8 and _shape_state["calls"] == 1,
            "calls=%d" % _shape_state["calls"],
        )
    finally:
        _shape_srv.shutdown()

    # 认不出的形状：错误信息要带响应体
    from bot.media.images import ImageError as _ImgErr

    _junk = {"choices": [{"message": {"content": "画好了！"}}]}

    class _CaseJunkHandler(_CaseShapeHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if self.path.endswith("/chat/completions"):
                self._reply(200, _junk)
                return
            super().do_POST()

    _junk_srv = HTTPServer(("127.0.0.1", 0), _CaseJunkHandler)
    threading.Thread(target=_junk_srv.serve_forever, daemon=True).start()
    try:
        _junk_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _junk_srv.server_address[1],
            api_key="sk-junk",
            model="some-image-model",
        )
        _junk_raised = False
        _junk_msg = ""
        loop2 = asyncio.new_event_loop()
        try:
            try:
                loop2.run_until_complete(ImageGenerator(_junk_spec, timeout=10).generate("一只小猫"))
            except _ImgErr as exc:
                _junk_raised = True
                _junk_msg = str(exc)
        finally:
            loop2.close()
        c.check(
            "chat 出图：返回里认不出图片数据时报错带响应体（便于对照端点实际返回）",
            _junk_raised and "画好了" in _junk_msg and "响应" in _junk_msg,
            _junk_msg[:160] if _junk_raised else "未抛错",
        )
    finally:
        _junk_srv.shutdown()

    # vLLM-Omni（Qwen-Image）完整流程：content 必须是数组；不带 extra_body 时
    # 200 但响应里没有图（推理白跑）；补 extra_body 后图放在 message.content 的
    # image_url 部件里（data URL）。
    _omni_state = {"images_calls": 0, "chat_calls": 0, "last_extra_body": None, "last_prompt": ""}

    class _CaseOmniHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/images/generations"):
                _omni_state["images_calls"] += 1
                self._reply(404, {"error": {"message": "not found"}})
                return
            if self.path.endswith("/chat/completions"):
                _omni_state["chat_calls"] += 1
                _omni_state["last_extra_body"] = body.get("extra_body")
                content = (body.get("messages") or [{}])[0].get("content")
                _omni_state["last_prompt"] = "".join(
                    str(part.get("text") if isinstance(part, dict) else part)
                    for part in (content if isinstance(content, list) else [content])
                )
                if isinstance(content, str):
                    self._reply(
                        400,
                        {
                            "error": {
                                "message": "Input should be a valid list: input.messages.0.content",
                                "type": "invalid_request_error",
                                "param": None,
                                "code": "invalid_parameter_error",
                            }
                        },
                    )
                    return
                if not isinstance(body.get("extra_body"), dict):
                    # 与用户实测一致：推理照跑，但响应里没有图
                    self._reply(
                        200,
                        {
                            "choices": [{"message": {"role": "assistant"}, "finish_reason": "stop", "index": 0}],
                            "usage": None,
                            "created": 1791256140,
                            "system_fingerprint": None,
                            "model": "qwen-image-3.0",
                            "id": "chatcmpl-omni-empty",
                        },
                    )
                    return
                self._reply(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "image_url",
                                            "image_url": {"url": "data:image/png;base64,%s" % _PNG_1X1},
                                        }
                                    ],
                                },
                                "finish_reason": "stop",
                                "index": 0,
                            }
                        ],
                        "usage": None,
                        "created": 1791256140,
                        "system_fingerprint": None,
                        "model": "qwen-image-3.0",
                        "id": "chatcmpl-omni-ok",
                    },
                )
                return
            self._reply(404, {"error": {"message": "not found"}})

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _omni_srv = HTTPServer(("127.0.0.1", 0), _CaseOmniHandler)
    threading.Thread(target=_omni_srv.serve_forever, daemon=True).start()
    try:
        _omni_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _omni_srv.server_address[1],
            api_key="sk-omni",
            model="qwen-image-3.0",
        )
        loop = asyncio.new_event_loop()
        try:
            omni_data, omni_ext = loop.run_until_complete(
                ImageGenerator(_omni_spec, timeout=10).generate("一只可爱的小猫", size="1024x1024")
            )
        finally:
            loop.close()
        c.check(
            "chat 出图：vLLM-Omni 流程（数组 content + 200 无图时补 extra_body + content 部件取图 + 提示词带画布尺寸）",
            len(omni_data) > 8
            and _omni_state["chat_calls"] == 3
            and isinstance(_omni_state["last_extra_body"], dict)
            and int(_omni_state["last_extra_body"].get("num_outputs_per_prompt") or 0) == 1
            and int(_omni_state["last_extra_body"].get("height") or 0) == 1000
            and "画布尺寸 1000×1000" in _omni_state["last_prompt"],
            "chat_calls=%d extra_body=%s prompt=%r"
            % (
                _omni_state["chat_calls"],
                json.dumps(_omni_state["last_extra_body"]),
                _omni_state["last_prompt"][:80],
            ),
        )
    finally:
        _omni_srv.shutdown()

    # 生图像素限制在生图请求时生效：/images/generations 路径的 size 参数与提示词都带画布尺寸
    _classic_state = {"body": None}

    class _CaseClassicHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            _classic_state["body"] = json.loads(self.rfile.read(length) or b"{}")
            payload = {"data": [{"b64_json": _PNG_1X1}]}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    _classic_srv = HTTPServer(("127.0.0.1", 0), _CaseClassicHandler)
    threading.Thread(target=_classic_srv.serve_forever, daemon=True).start()
    try:
        _classic_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/v1" % _classic_srv.server_address[1],
            api_key="sk-classic",
            model="some-image-model",
        )
        loop_classic = asyncio.new_event_loop()
        try:
            loop_classic.run_until_complete(
                ImageGenerator(_classic_spec, timeout=10).generate("月下街道，一个人撑伞", size="1024x1024")
            )
        finally:
            loop_classic.close()
        _cb = _classic_state["body"] or {}
        c.check(
            "生图请求即带画布尺寸：size 参数钳到 1000x1000 + 提示词写「画布尺寸 1000×1000 像素」",
            _cb.get("size") == "1000x1000"
            and "画布尺寸 1000×1000 像素" in str(_cb.get("prompt") or "")
            and str(_cb.get("prompt") or "").startswith("月下街道"),
            "size=%r prompt=%r" % (_cb.get("size"), str(_cb.get("prompt") or ""))[:120],
        )
    finally:
        _classic_srv.shutdown()

    # is_local：局域网私网地址不强制 API Key（用户本地 vLLM 走 10.x 内网）
    _local_cases = [
        ("http://10.0.0.7:8000/v1", True),
        ("http://192.168.1.5:8000/v1", True),
        ("http://172.16.0.1/v1", True),
        ("http://172.32.0.1/v1", False),
        ("http://8.8.8.8/v1", False),
        ("http://127.0.0.1:8000/v1", True),
        ("https://localhost:8000/v1", True),
        ("http://mygpu.local:8000/v1", True),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", False),
    ]
    _local_ok = True
    for _url, _expect in _local_cases:
        _spec = ProviderSpec(slot="vision", engine=ENGINE_OPENAI, base_url=_url, model="m")
        if _spec.is_local != _expect:
            _local_ok = False
    # 局域网 + 模型名、无 Key → 视为已配置
    _lan_spec = ProviderSpec(
        slot="vision", engine=ENGINE_OPENAI, base_url="http://10.0.0.7:8000/v1", api_key="", model="local-model"
    )
    c.check(
        "局域网私网地址（10.x / 192.168 / 172.16-31 / .local）识别为本地，不强制 API Key",
        _local_ok and _lan_spec.configured,
        "lan_configured=%s" % _lan_spec.configured,
    )
    # 实际回复链路（LLMClient.configured）必须与上面的判定一致——
    # 用户实测：LLM 填 10.x 局域网地址、Key 留空，界面说"可留空"，
    # 聊天却报"尚未配置 LLM 的 base_url / api_key"
    from bot.ai_engine.llm_client import LLMClient

    _lan_client = LLMClient(base_url="http://10.0.0.7:8000/v1", api_key="", model="local-model")
    _remote_client = LLMClient(base_url="https://api.deepseek.com/v1", api_key="", model="deepseek-chat")
    _remote_keyed = LLMClient(base_url="https://api.deepseek.com/v1", api_key="sk-test", model="deepseek-chat")
    c.check(
        "回复链路：局域网端点无 Key 视为已配置（与测试线路判定一致）",
        _lan_client.configured()
        and not _remote_client.configured()
        and _remote_keyed.configured(),
    )
    # 推理类模型：思考占满 max_tokens 时正文为空 → 重试自动翻倍长度直到有正文
    from types import SimpleNamespace

    class _TokFakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs.get("max_tokens"))
            if len(self.calls) < 2:
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="   "))])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="正常"))])

    class _TokFakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=_TokFakeCompletions())

        async def close(self):
            pass

    _tok_fake = _TokFakeClient()
    _tok_client = LLMClient(base_url="http://10.0.0.7:8000/v1", api_key="", model="m", max_tokens=100, max_retries=2)
    _tok_client._client = _tok_fake
    _tok_client._signature = (_tok_client.base_url, _tok_client.api_key, _tok_client.timeout, _tok_client.model)
    _tok_reply = asyncio.run(_tok_client.chat([{"role": "user", "content": "hi"}]))
    c.check(
        "推理模型空正文：重试时翻倍 max_tokens 直到出正文",
        _tok_reply == "正常" and _tok_fake.chat.completions.calls == [100, 200],
        str(_tok_fake.chat.completions.calls),
    )

    # missing_fields：未配置提示要精确到缺哪个字段
    _mf_ok = (
        ProviderSpec(slot="vision", engine=ENGINE_OPENAI, base_url="https://api.deepseek.com/v1", model="")
        .missing_fields()
        == ["模型名"]
        and ProviderSpec(slot="vision", engine=ENGINE_OPENAI, base_url="https://api.deepseek.com/v1", model="m")
        .missing_fields()
        == ["API Key"]
        and _lan_spec.missing_fields() == []
        and ProviderSpec(slot="vision", engine=ENGINE_OPENAI).missing_fields() == ["Base URL", "模型名"]
    )
    c.check("未配置提示按字段精确列出缺失项（Base URL / 模型名 / API Key）", _mf_ok, "")

    # DashScope 原生协议兜底：兼容模式 /images/generations 404 时改走
    # {host}/api/v1/services/aigc/multimodal-generation/generation，
    # 图片取 output.choices[0].message.content[].image（URL 需下载）
    _ds_state = {"images_calls": 0, "native_calls": 0, "chat_calls": 0, "native_body": None}
    _ds_png = base64.b64decode(_PNG_1X1)

    class _CaseDashScopeHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.endswith("/images/generations"):
                _ds_state["images_calls"] += 1
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.endswith("/services/aigc/multimodal-generation/generation"):
                _ds_state["native_calls"] += 1
                _ds_state["native_body"] = body
                self._reply(
                    200,
                    {
                        "request_id": "req-ds",
                        "output": {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {
                                        "role": "assistant",
                                        "content": [
                                            {
                                                "image": "http://127.0.0.1:%d/img.png" % _ds_port,
                                                "type": "image",
                                            }
                                        ],
                                    },
                                }
                            ]
                        },
                        "usage": {"width": 512, "height": 512},
                    },
                )
                return
            if self.path.endswith("/chat/completions"):
                _ds_state["chat_calls"] += 1
                self._reply(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
                return
            self._reply(404, {})

        def do_GET(self):
            if self.path == "/img.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_ds_png)))
                self.end_headers()
                self.wfile.write(_ds_png)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _reply(self, code, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    global _ds_port
    _ds_srv = HTTPServer(("127.0.0.1", 0), _CaseDashScopeHandler)
    _ds_port = _ds_srv.server_address[1]
    threading.Thread(target=_ds_srv.serve_forever, daemon=True).start()
    try:
        _ds_spec = ProviderSpec(
            slot="image",
            engine=ENGINE_OPENAI,
            base_url="http://127.0.0.1:%d/compatible-mode/v1" % _ds_port,
            api_key="sk-ds",
            model="qwen-image-3.0",
        )
        loop = asyncio.new_event_loop()
        try:
            ds_data, ds_ext = loop.run_until_complete(
                ImageGenerator(_ds_spec, timeout=10).generate("一只小猫", size="512x512")
            )
        finally:
            loop.close()
        _body = _ds_state["native_body"] or {}
        c.check(
            "百炼生图：兼容模式 images 404 时走原生协议（content 部件 image 键 + URL 下载）",
            ds_data == _ds_png
            and _ds_state["images_calls"] == 1
            and _ds_state["native_calls"] == 1
            and _ds_state["chat_calls"] == 0
            and (_body.get("parameters") or {}).get("size") == "512*512",
            "images=%d native=%d chat=%d body=%s"
            % (
                _ds_state["images_calls"],
                _ds_state["native_calls"],
                _ds_state["chat_calls"],
                json.dumps(_body, ensure_ascii=False)[:160],
            ),
        )
    finally:
        _ds_srv.shutdown()

    # 图像理解自检用内置测试图：独立于图像生成，64x64 纯红 PNG
    from bot.media.images import builtin_test_image_data_url

    _test_url = builtin_test_image_data_url()
    _test_ok = False
    _test_is_red = False
    try:
        from PIL import Image

        _img = Image.open(io.BytesIO(base64.b64decode(_test_url.split(",", 1)[1])))
        _test_ok = _img.format == "PNG" and _img.size == (64, 64)
        _test_is_red = _img.getpixel((0, 0))[:3] == (255, 0, 0)
    except Exception:
        pass
    c.check(
        "图像理解内置测试图为纯红 PNG（测试线路不依赖图像生成）",
        _test_ok and _test_is_red and _test_url.startswith("data:image/png;base64,"),
        _test_url[:60],
    )

    # 模型路由的服务商预设
    from app import provider_presets
    from app import llm_presets

    c.check(
        "三个槽位都有成规模的服务商预设（末位为「自定义 / 其他」）",
        all(
            len(provider_presets.labels(s)) >= 4 and provider_presets.labels(s)[-1] == "自定义 / 其他"
            for s in ("vision", "image", "tts")
        )
        and len(provider_presets.labels("vision")) >= 12
        and len(provider_presets.labels("image")) >= 14
        and len(provider_presets.labels("tts")) >= 8,
        "vision=%d image=%d tts=%d"
        % tuple(len(provider_presets.labels(s)) for s in ("vision", "image", "tts")),
    )
    c.check(
        "看图预设含 DeepSeek（官方 deepseek-flash 看图 + 硅基流动托管 DeepSeek-VL2）",
        any(item.key == "deepseek-vl" for item in provider_presets.SLOT_PRESETS.get("vision", ()))
        and any(
            item.key == "deepseek-flash-vision"
            and "deepseek-flash" in item.models
            for item in provider_presets.SLOT_PRESETS.get("vision", ())
        ),
    )
    c.check(
        "生图含 Gemini 原生接口预设（gemini-native 引擎 / v1beta / nano banana 2 模型）",
        provider_presets.by_label("image", "Google Gemini 生图（原生接口，支持全部新模型）") is not None
        and provider_presets.by_label("image", "Google Gemini 生图（原生接口，支持全部新模型）").engine
        == "gemini-native"
        and "gemini-3.1-flash-lite-image"
        in provider_presets.by_label("image", "Google Gemini 生图（原生接口，支持全部新模型）").models
        and provider_presets.by_label("image", "Google Gemini 生图（原生接口，支持全部新模型）").base_url
        == "https://generativelanguage.googleapis.com/v1beta",
    )
    _gemini_compat = provider_presets.by_label("image", "Google Gemini 生图（OpenAI 兼容层）")
    c.check(
        "Gemini 兼容层预设只列官方文档点名的模型",
        _gemini_compat is not None
        and set(_gemini_compat.models) == {"gemini-2.5-flash-image", "gemini-3-pro-image-preview"},
        str(_gemini_compat.models if _gemini_compat else None),
    )
    c.check(
        "LLM 预设覆盖主流服务商（30+ 家）",
        len(llm_presets.PRESETS) >= 30 and llm_presets.labels()[-1] == "自定义 / 其他",
        "共 %d 家" % len(llm_presets.PRESETS),
    )
    c.check(
        "新服务商预设可反查（Anthropic / 百度千帆 / 通义）",
        llm_presets.by_base_url("https://api.anthropic.com/v1") is not None
        and provider_presets.by_base_url("image", "https://qianfan.baidubce.com/v2") is not None
        and provider_presets.by_base_url("vision", "https://dashscope.aliyuncs.com/compatible-mode/v1") is not None,
    )
    _gemini_preset = provider_presets.by_base_url(
        "image", "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    c.check(
        "生图预设含 Gemini nano banana（OpenAI 兼容层 / gemini-2.5-flash-image）",
        _gemini_preset is not None and "gemini-2.5-flash-image" in _gemini_preset.models,
        str(_gemini_preset),
    )

    # ---------------------------------------------------------- 入站语音：平台参考转写（零配置）
    from common import config as _cfg_mod
    from bot.media.hub import MediaHub

    _hits = {"count": 0}

    class _VoiceFileHandler(BaseHTTPRequestHandler):
        """任意 GET 都返回一小段字节（模拟 CDN 上的语音附件）。"""

        def do_GET(self):
            _hits["count"] += 1
            body = b"RIFF-fake-audio-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    _voice_srv = HTTPServer(("127.0.0.1", 0), _VoiceFileHandler)
    threading.Thread(target=_voice_srv.serve_forever, daemon=True).start()
    _vport = _voice_srv.server_address[1]
    try:

        def _run_hub(attachments):
            loop2 = asyncio.new_event_loop()
            try:
                return loop2.run_until_complete(
                    MediaHub(SimpleNamespace(config={"media.enabled": True})).inbound_media(
                        SimpleNamespace(raw={"attachments": attachments})
                    )
                )
            finally:
                loop2.close()

        # 1) 平台给了 asr_refer_text → 转写成功（V0.2.2：同时把音频下载存档）
        _hits["count"] = 0
        res = _run_hub(
            [{"url": "http://127.0.0.1:%d/x" % _vport, "content_type": "voice", "asr_refer_text": "平台参考"}]
        )
        c.check(
            "语音转文字用 QQ 平台参考转写（asr_refer_text，零配置）且音频落盘存档（V0.2.2）",
            res.voice_texts == ["平台参考"]
            and len(res.voice_paths) == 1
            and Path(res.voice_paths[0]).is_file()
            and _hits["count"] >= 1,
            "hits=%d %s" % (_hits["count"], str(res.__dict__)),
        )

        # 2) 带 url / voice_wav_url 的语音附件 → 优先下载 wav 存档（V0.2.2）
        _hits["count"] = 0
        res = _run_hub(
            [
                {
                    "url": "http://127.0.0.1:%d/s.silk" % _vport,
                    "voice_wav_url": "http://127.0.0.1:%d/w.wav" % _vport,
                    "content_type": "voice",
                    "asr_refer_text": "我想你了",
                }
            ]
        )
        c.check(
            "语音音频下载存档：有 voice_wav_url 时存 wav（V0.2.2 对话记录保存语音）",
            res.voice_texts == ["我想你了"]
            and len(res.voice_paths) == 1
            and res.voice_paths[0].endswith("wav")
            and _hits["count"] >= 1,
            "hits=%d %s" % (_hits["count"], str(res.__dict__)),
        )

        # 3) 平台没给参考转写但音频下载失败 → 明确提示听不清（不静默失败）
        res = _run_hub(
            [{"url": "http://127.0.0.1:1/x", "content_type": "voice"}]
        )
        c.check(
            "平台没提供参考转写时给出可读提示",
            not res.voice_texts and any("参考转写" in e for e in res.errors),
            str(res.__dict__),
        )
    finally:
        _voice_srv.shutdown()

    # ---------------------------------------------------------- 角色级音色调节（V0.2）
    class _FakeTtsCfg:
        def get(self, key, default=None):
            data = {
                "providers.tts": {
                    "engine": "edge-tts",
                    "rate": "+5%",
                    "volume": "-10%",
                    "speed": "1.1",
                },
                "media.enabled": True,
            }
            return data.get(key, default)

    _style_hub = MediaHub(SimpleNamespace(config=_FakeTtsCfg()))
    c.check(
        "角色未设调节值时跟随全局（rate / volume / speed）",
        _style_hub._tts_style({}) == {"rate": "+5%", "volume": "-10%", "speed": 1.1},
        str(_style_hub._tts_style({})),
    )
    c.check(
        "角色级调节覆盖全局（留空项仍用全局）",
        _style_hub._tts_style(
            {"tts_rate": "+20%", "tts_pitch": "+5Hz", "tts_volume": "", "tts_speed": ""}
        )
        == {"rate": "+20%", "pitch": "+5Hz", "volume": "-10%", "speed": 1.1},
        str(_style_hub._tts_style({"tts_rate": "+20%", "tts_pitch": "+5Hz"})),
    )

    # 头像上传：registry.update_avatar 落盘 + 旧头像清理 + schema 迁移字段
    from bot.character_manager.registry import CharacterRegistry
    from bot.database import Database as _Database
    from bot.database import crud as _crud
    from bot.database.models import MIGRATION_COLUMNS as _MIG

    c.check(
        "角色级音色调节四字段纳入旧库迁移",
        {col for table, col, _ddl in _MIG if table == "characters"}
        >= {"tts_rate", "tts_pitch", "tts_volume", "tts_speed"},
        str([col for table, col, _ddl in _MIG if table == "characters"]),
    )
    _avatar_dir = Path(tempfile.mkdtemp(prefix="tavern-avatar-"))
    _avatar_db_path = _avatar_dir / "test.db"
    _loop3 = asyncio.new_event_loop()
    try:
        _avatar_db = _Database(_avatar_db_path)
        _loop3.run_until_complete(_avatar_db.connect())
        _reg = CharacterRegistry(_avatar_db, storage_dir=_avatar_dir)
        _loop3.run_until_complete(_crud.insert_character(_avatar_db, {"id": "av1", "name": "头像测试"}))
        row = _loop3.run_until_complete(_reg.update_avatar("av1", b"fake-png-1", ".png"))
        c.check(
            "update_avatar 写入 avatars 目录并更新 avatar_path",
            row is not None
            and row.get("avatar_path")
            and (_avatar_dir / "avatars" / "av1.png").exists(),
            str(row.get("avatar_path")),
        )
        row = _loop3.run_until_complete(_reg.update_avatar("av1", b"fake-jpg-2", ".jpg"))
        c.check(
            "更换不同扩展名头像时旧头像文件被清理",
            (_avatar_dir / "avatars" / "av1.jpg").exists()
            and not (_avatar_dir / "avatars" / "av1.png").exists()
            and row.get("avatar_path"),
            str(row.get("avatar_path")),
        )
        row = _loop3.run_until_complete(_reg.update_avatar("av1", b"pic", ".heic"))
        c.check("未知头像扩展名回退 .png", row.get("avatar_path") and row.get("avatar_path").endswith(".png"), str(row.get("avatar_path")))
        try:
            _loop3.run_until_complete(_reg.update_avatar("av1", b"", ".png"))
            c.check("空头像文件报 CharacterCardError", False, "未抛出异常")
        except Exception:
            c.check("空头像文件报 CharacterCardError", True)
        _loop3.run_until_complete(_avatar_db.close())
    finally:
        _loop3.close()

    # ---------------------------------------------------------- [IMG] 标记放宽
    _hub = MediaHub(SimpleNamespace(config={"media.enabled": True, "media.allow_image": True}))
    _rest, _prompt = _hub.parse_image_prompt("画好了！[IMG] 一只在月球上喝茶的猫")
    c.check(
        "[IMG] 写在句中/行尾也能识别（不再要求单独成行）",
        _prompt == "一只在月球上喝茶的猫" and _rest.strip() == "画好了！",
        "rest=%r prompt=%r" % (_rest, _prompt),
    )
    _rest2, _prompt2 = _hub.parse_image_prompt("看，我给你画好了\n[IMG] 一朵会飞的云")
    c.check("[IMG] 独立成行仍然有效", _prompt2 == "一朵会飞的云", "rest2=%r" % _rest2)

    # ---------------------------------------------------- 生图风格按角色人设匹配
    from bot.media.images import detect_character_style, image_prompt_with_style

    _anime_char = {
        "name": "小月",
        "description": "{{char}} 是异世界的魔法少女，猫耳，傲娇。",
        "personality": "傲娇，二次元",
    }
    _real_char = {
        "name": "沈青",
        "description": "{{char}} 是一位写实风格的都市摄影师，常拍街拍写真。",
        "personality": "冷静，喜欢胶片摄影",
    }
    _neutral_char = {"name": "阿哲", "description": "{{char}} 是普通青年。"}
    c.check(
        "二次元角色（魔法少女/猫耳）识别为动漫风格",
        detect_character_style(_anime_char) == "anime",
        detect_character_style(_anime_char),
    )
    c.check(
        "真实风格角色（摄影师/街拍/写真）识别为写实风格",
        detect_character_style(_real_char) == "realistic",
        detect_character_style(_real_char),
    )
    c.check(
        "没有风格特征的角色不强加风格",
        detect_character_style(_neutral_char) == "" and detect_character_style({}) == "",
        repr(detect_character_style(_neutral_char)),
    )
    _styled, _style = image_prompt_with_style("一只在窗边喝茶的猫", _anime_char, "auto")
    c.check(
        "二次元角色的绘图描述自动追加动漫风格短语",
        _style == "anime" and "二次元动漫风格" in _styled and _styled.startswith("一只在窗边喝茶的猫"),
        "style=%r prompt=%r" % (_style, _styled[:80]),
    )
    _styled2, _style2 = image_prompt_with_style("清晨的街头", _real_char, "auto")
    c.check(
        "写实角色的绘图描述自动追加写实风格短语",
        _style2 == "realistic" and "写实摄影风格" in _styled2,
        "style=%r prompt=%r" % (_style2, _styled2[:80]),
    )
    _styled3, _style3 = image_prompt_with_style("一只猫", _neutral_char, "anime")
    c.check(
        "全局开关可强制指定风格（anime 覆盖 auto 识别结果）",
        _style3 == "anime" and "二次元动漫风格" in _styled3,
        "style=%r" % _style3,
    )
    _styled4, _style4 = image_prompt_with_style("一只猫", _anime_char, "off")
    c.check("全局开关 off 时不追加风格短语", _style4 == "" and _styled4 == "一只猫", "style=%r" % _style4)

    # V0.2.2 第五批：自定义风格关键词 + 角色本人参考段
    _styled5, _style5 = image_prompt_with_style(
        "一只在窗边喝茶的猫", _anime_char, "custom", "吉卜力风格，水彩质感"
    )
    c.check(
        "custom 风格：用户自定义关键词拼进绘图描述",
        _style5 == "custom" and _styled5.endswith("，画面风格：吉卜力风格，水彩质感"),
        "style=%r prompt=%r" % (_style5, _styled5[:100]),
    )
    _styled6, _style6 = image_prompt_with_style("一只在窗边喝茶的猫", _anime_char, "custom", "   ")
    c.check(
        "custom 但没填关键词时回落自动识别",
        _style6 == "anime" and "二次元动漫风格" in _styled6,
        "style=%r prompt=%r" % (_style6, _styled6[:80]),
    )
    # V0.2.2 第六批：主模型融合绘图描述 + 生图像素限制 1000×1000
    from bot.media.images import (
        build_image_fuse_messages,
        clamp_image_size,
        fuse_image_prompt,
        parse_image_fuse,
    )

    _with_char = {
        "name": "苏苏",
        "description": "粉色长发，水手服，眼睛是琥珀色的，总戴一顶针织帽。",
        "personality": "软萌、爱撒娇、怕黑",
    }
    _fused_msgs = build_image_fuse_messages("画一张我的照片", _with_char)
    c.check(
        "融合消息：system 教融合规则，user 带角色描述/性格/背景与绘图描述",
        "绘图提示词写手" in _fused_msgs[0]["content"]
        and "不要" in _fused_msgs[0]["content"]
        and "粉色长发" in _fused_msgs[1]["content"]
        and "画一张我的照片" in _fused_msgs[1]["content"],
        _fused_msgs[1]["content"][:120],
    )
    c.check(
        "融合输出清洗：围栏 / 前导语 / 引号 / 短标签首行都能剥掉",
        parse_image_fuse('```n"最终绘图描述：粉色长发少女站在水手服里"') == "粉色长发少女站在水手服里"
        and parse_image_fuse("好的，绘图描述是：「月下街道」") == "月下街道"
        and parse_image_fuse("") == "",
        repr(parse_image_fuse("好的，绘图描述是：「月下街道」")),
    )

    class _FakeFuseLLM:
        def __init__(self, reply: str, ok: bool = True) -> None:
            self._reply = reply
            self._ok = ok
            self.calls: List[str] = []

        def configured(self) -> bool:
            return self._ok

        async def chat(self, messages, **kwargs):
            self.calls.append(str(messages[1].get("content") or ""))
            return self._reply

    _loop4 = asyncio.new_event_loop()
    try:
        _fake_ok = _FakeFuseLLM("粉色长发少女穿着水手服站在樱花树下，琥珀色眼睛，戴针织帽，柔和光影。")
        _fused_prompt = _loop4.run_until_complete(
            fuse_image_prompt(_fake_ok, "画一张我的照片", _with_char)
        )
        c.check(
            "主模型融合成功：用融合后的完整描述替代原描述（人设细节进画面）",
            _fused_prompt == "粉色长发少女穿着水手服站在樱花树下，琥珀色眼睛，戴针织帽，柔和光影。"
            and len(_fake_ok.calls) == 1,
            "calls=%d prompt=%r" % (len(_fake_ok.calls), _fused_prompt[:60]),
        )
        _fake_bad = _FakeFuseLLM("")
        _fused_bad = _loop4.run_until_complete(fuse_image_prompt(_fake_bad, "一只猫", _with_char))
        c.check("融合输出为空时回落原描述（生图照常）", _fused_bad == "一只猫", repr(_fused_bad))
        _fake_unconf = _FakeFuseLLM("x", ok=False)
        _fused_unconf = _loop4.run_until_complete(fuse_image_prompt(_fake_unconf, "一只猫", _with_char))
        c.check("主模型未配置时跳过融合直接用原描述", _fused_unconf == "一只猫" and not _fake_unconf.calls, "")
        _fused_none = _loop4.run_until_complete(fuse_image_prompt(None, "一只猫", _with_char))
        c.check("没有主模型实例时跳过融合", _fused_none == "一只猫", "")
    finally:
        _loop4.close()

    # 像素限制：请求尺寸钳制
    c.check(
        "clamp_image_size：默认 1024x1024 钳到 1000x1000，横图等比缩放，小图不变",
        clamp_image_size("1024x1024") == "1000x1000"
        and clamp_image_size("1536x1024") == "1000x667"
        and clamp_image_size("1200x800") == "1000x667"
        and clamp_image_size("800x600") == "800x600"
        and clamp_image_size("abc") == "abc"
        and clamp_image_size("") == "",
        "1024->%s 1536->%s" % (clamp_image_size("1024x1024"), clamp_image_size("1536x1024")),
    )
    # 像素限制：返回图片缩放
    from bot.media.images import _downscale_if_needed, MAX_IMAGE_PIXEL

    import io as _io

    from PIL import Image as _PILImage

    _big_buf = _io.BytesIO()
    _PILImage.new("RGB", (1200, 900), (200, 30, 30)).save(_big_buf, format="PNG")
    _small_buf = _io.BytesIO()
    _PILImage.new("RGB", (800, 600), (30, 200, 30)).save(_small_buf, format="PNG")
    _scaled_data, _scaled_ext = _downscale_if_needed(_big_buf.getvalue(), "png")
    with _PILImage.open(_io.BytesIO(_scaled_data)) as _scaled_img:
        _sw, _sh = _scaled_img.size
    c.check(
        "返回图片超过 1000×1000 时等比缩放（1200x900 → 1000x750）",
        max(_sw, _sh) <= MAX_IMAGE_PIXEL and (_sw, _sh) == (1000, 750) and _scaled_ext == "png",
        "scaled=%dx%d" % (_sw, _sh),
    )
    _kept_data, _kept_ext = _downscale_if_needed(_small_buf.getvalue(), "png")
    c.check("返回图片不超限时原样返回", _kept_data == _small_buf.getvalue(), "ext=%s" % _kept_ext)
    _jpg_buf = _io.BytesIO()
    _PILImage.new("RGB", (1024, 1024), (10, 10, 10)).save(_jpg_buf, format="JPEG")
    _scaled_jpg, _scaled_jpg_ext = _downscale_if_needed(_jpg_buf.getvalue(), "jpg")
    with _PILImage.open(_io.BytesIO(_scaled_jpg)) as _jpg_img:
        _jw, _jh = _jpg_img.size
    c.check(
        "JPEG 大图同样缩放且保持 JPEG 编码",
        max(_jw, _jh) <= MAX_IMAGE_PIXEL and _scaled_jpg[:3] == b"\xff\xd8\xff",
        "scaled=%dx%d" % (_jw, _jh),
    )
    _garbage_out, _garbage_ext = _downscale_if_needed(b"not-an-image", "png")
    c.check("不是图片的字节缩放失败时原样返回（不阻断生图）", _garbage_out == b"not-an-image", "")

    # ---------------------------------------------------------- TTS 风格参数
    from common.providers import ENGINE_EDGE_TTS

    from bot.media import voice as _voice_pkg
    from bot.media.voice import TTS as _TTS

    c.check(
        "edge-tts 参数归一化（语速/音调）",
        _voice_pkg._normalize_edge_rate("10") == "+10%"
        and _voice_pkg._normalize_edge_rate("-20%") == "-20%"
        and _voice_pkg._normalize_edge_rate("0") == ""
        and _voice_pkg._normalize_edge_pitch("5Hz") == "+5Hz"
        and _voice_pkg._normalize_edge_pitch("-3") == "-3Hz",
    )
    _edge_calls: Dict[str, Any] = {}

    class _FakeEdgeCommunicate:
        def __init__(self, text, voice, **kwargs):
            _edge_calls.clear()
            _edge_calls.update(kwargs)
            _edge_calls["text"] = text
            _edge_calls["voice"] = voice

        async def save(self, path):
            Path(path).write_bytes(b"ID3fake-mp3")

    _orig_edge = _voice_pkg.edge_tts
    _voice_pkg.edge_tts = SimpleNamespace(Communicate=_FakeEdgeCommunicate)
    try:
        _tts = _TTS(ProviderSpec(slot="tts", engine=ENGINE_EDGE_TTS, voice="zh-CN-XiaoxiaoNeural"))
        loop3 = asyncio.new_event_loop()
        try:
            _out = loop3.run_until_complete(
                _tts.synthesize("你好", rate="+10%", pitch="+5Hz", volume="-10%", speed=1.2)
            )
        finally:
            loop3.close()
        c.check(
            "edge-tts 合成携带 rate/pitch/volume（speed 仅 OpenAI 引擎用）",
            _edge_calls.get("rate") == "+10%"
            and _edge_calls.get("pitch") == "+5Hz"
            and _edge_calls.get("volume") == "-10%"
            and "speed" not in _edge_calls
            and _out["format"] == "mp3",
            str(_edge_calls),
        )
        c.check(
            "试听文案池随机取句（12+ 句）",
            len(_voice_pkg.PREVIEW_TEXTS) >= 12 and _voice_pkg.random_preview_text() in _voice_pkg.PREVIEW_TEXTS,
            "共 %d 句" % len(_voice_pkg.PREVIEW_TEXTS),
        )
    finally:
        _voice_pkg.edge_tts = _orig_edge

    # ---------------------------------------------------------- TTS 风格参数入配置
    _cfg_tts = {"providers.tts": {"engine": "edge-tts", "voice": "v", "rate": "+10%", "pitch": "-5Hz", "speed": "1.2"}}
    _tts_spec = load_slot(_cfg_tts, "tts")
    c.check(
        "load_slot 把 TTS 风格参数读进 extra",
        _tts_spec.extra.get("rate") == "+10%" and _tts_spec.extra.get("pitch") == "-5Hz" and _tts_spec.extra.get("speed") == "1.2",
        str(_tts_spec.extra),
    )
    c.check(
        "语音回复默认概率为 5%",
        _cfg_mod.DEFAULTS["media"]["voice_reply_probability"] == 0.05,
    )

    # ---------------------------------------------------------- 百炼（DashScope）TTS 引擎
    c.check(
        "百炼端点按模型系列路由（cosyvoice/qwen-audio 走 SpeechSynthesizer，Qwen-TTS 走多模态接口，不能混用）",
        _voice_pkg.TTS._dashscope_path("cosyvoice-v3-flash") == "/api/v1/services/audio/tts/SpeechSynthesizer"
        and _voice_pkg.TTS._dashscope_path("qwen-audio-3.0-tts-flash") == "/api/v1/services/audio/tts/SpeechSynthesizer"
        and _voice_pkg.TTS._dashscope_path("qwen3-tts-flash") == "/api/v1/services/aigc/multimodal-generation/generation"
        and _voice_pkg.TTS._dashscope_path("qwen-tts") == "/api/v1/services/aigc/multimodal-generation/generation",
    )
    c.check(
        "百炼 Base URL 归一（带不带 /compatible-mode/v1 都能剥成服务根地址）",
        _voice_pkg.TTS._dashscope_root("https://dashscope.aliyuncs.com/compatible-mode/v1") == "https://dashscope.aliyuncs.com"
        and _voice_pkg.TTS._dashscope_root("https://ws-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/")
        == "https://ws-xxx.cn-beijing.maas.aliyuncs.com"
        and _voice_pkg.TTS._dashscope_root("https://dashscope.aliyuncs.com") == "https://dashscope.aliyuncs.com",
    )
    _ds_cfg_spec = load_slot(
        {"providers.tts": {"engine": "dashscope", "base_url": "https://dashscope.aliyuncs.com", "api_key": "sk", "model": "qwen3-tts-flash"}},
        "tts",
    )
    c.check(
        "load_slot：dashscope 引擎合法，未填音色时默认 yuxiaoyun_v3.1（voice 参数必填）",
        _ds_cfg_spec.engine == "dashscope" and _ds_cfg_spec.voice == "yuxiaoyun_v3.1",
        "engine=%s voice=%s" % (_ds_cfg_spec.engine, _ds_cfg_spec.voice),
    )
    _ds_voice_items = _voice_pkg.dashscope_voices()
    c.check(
        "百炼候选音色清单含 Qwen（Cherry）/ CosyVoice（longanyang）/ Qwen-Audio（yuxiaoyun_v3.1）三族",
        any(v["short_name"] == "Cherry" for v in _ds_voice_items)
        and any(v["short_name"] == "longanyang" for v in _ds_voice_items)
        and any(v["short_name"] == "yuxiaoyun_v3.1" for v in _ds_voice_items)
        and len(_ds_voice_items) >= 60,
        "共 %d 个" % len(_ds_voice_items),
    )

    # ---------------------------------------------------------- TTS 指令风格（内置 SKILL）
    from bot.media.instruct import (
        build_qwen_audio_tag_messages,
        build_tts_instruction_messages,
        generate_qwen_audio_tags,
        generate_tts_instruction,
        is_qwen_audio_tts_model,
        is_tts_instruct_model,
        local_qwen_audio_tags,
        parse_tagged_output,
        parse_tagged_text,
        parse_tts_instruction,
    )

    c.check(
        "指令风格门控：仅 Qwen3-TTS-Instruct 系列（非实时）生效",
        is_tts_instruct_model("qwen3-tts-instruct-flash") is True
        and is_tts_instruct_model("qwen3-tts-instruct-flash-2026-01-26") is True
        and is_tts_instruct_model("qwen3-tts-instruct-flash-realtime") is False
        and is_tts_instruct_model("qwen3-tts-flash") is False
        and is_tts_instruct_model("cosyvoice-v3-flash") is False
        and is_tts_instruct_model("") is False,
    )
    _ins_msgs = build_tts_instruction_messages(
        "下班后一起去试试那家新开的火锅店？",
        {"name": "小满", "description": "活泼爱笑的上班族", "personality": "喜欢开玩笑"},
    )
    _ins_system = _ins_msgs[0]["content"]
    c.check(
        "SKILL 提示词教的是百炼官方格式（五原则 / 描述维度 / 官方示例）",
        "具体而非模糊" in _ins_system
        and "多维而非单一" in _ins_system
        and "原创而非模仿" in _ins_system
        and "语速" in _ins_system and "情感" in _ins_system
        and "吐字清晰精准" in _ins_system  # 官方示例原文
        and "不要" in _ins_system,
    )
    c.check(
        "SKILL 提示词带角色人设与待合成文本",
        "小满" in _ins_msgs[1]["content"]
        and "喜欢开玩笑" in _ins_msgs[1]["content"]
        and "火锅店" in _ins_msgs[1]["content"],
    )
    c.check(
        "指令解析：去代码围栏 / 引号 / 前缀说明，超长截断",
        parse_tts_instruction("```用温柔放缓的语气，语速偏慢```") == "用温柔放缓的语气，语速偏慢"
        and parse_tts_instruction('“指令：语气轻快，语速偏快，带点俏皮”') == "语气轻快，语速偏快，带点俏皮"
        and parse_tts_instruction("以下是指令：\n温柔一点，慢一点\n（就这样）") == "温柔一点，慢一点"
        and len(parse_tts_instruction("语" * 500)) == 300
        and parse_tts_instruction("   \n  ") == ""
        # 正文中间的强调引号不能被当成包裹引号截掉
        and parse_tts_instruction("语气温柔关切，强调「记得」和「别跑太快」") == "语气温柔关切，强调「记得」和「别跑太快」",
    )

    class _FakeInstrLLM:
        def __init__(self, reply: str):
            self._reply = reply
            self.calls = 0
            self.max_tokens_seen = 0

        def configured(self) -> bool:
            return True

        async def chat(self, messages, **_kw):
            self.calls += 1
            self.max_tokens_seen = int(_kw.get("max_tokens") or 0)
            return self._reply

    _ins_gen_loop = asyncio.new_event_loop()
    try:
        _fake_llm = _FakeInstrLLM('好的，指令是："温柔关切地，语速稍慢，像在叮嘱好朋友"')
        _ins_gen = _ins_gen_loop.run_until_complete(
            generate_tts_instruction(
                _fake_llm,
                "明天早上八点记得带伞",
                {"name": "小满", "description": "温柔细心", "personality": "爱操心"},
            )
        )
        _ins_gen_none = _ins_gen_loop.run_until_complete(generate_tts_instruction(None, "测试"))
        _ins_gen_empty = _ins_gen_loop.run_until_complete(generate_tts_instruction(_FakeInstrLLM("  \n  "), "测试"))
        # 思考类模型偶发空内容：第一次空、第二次有 → 重试后成功
        _flaky_replies = ["", "俏皮一点，语速偏快"]

        class _FlakyLLM:
            def __init__(self):
                self.n = 0

            def configured(self) -> bool:
                return True

            async def chat(self, messages, **_kw):
                self.n += 1
                return _flaky_replies[min(self.n - 1, len(_flaky_replies) - 1)]

        _flaky_llm = _FlakyLLM()
        _ins_flaky = _ins_gen_loop.run_until_complete(generate_tts_instruction(_flaky_llm, "测试"))
    finally:
        _ins_gen_loop.close()
    c.check(
        "指令生成走主模型并清洗输出；LLM 缺失 / 空输出时返回空串（合成照常）",
        _ins_gen == "温柔关切地，语速稍慢，像在叮嘱好朋友"
        and _ins_gen_none == ""
        and _ins_gen_empty == "",
        "生成=%r" % _ins_gen,
    )
    c.check(
        "指令生成给思考类模型不限制思考长度（max_tokens 上限 4096），且空内容会重试一次",
        _fake_llm.max_tokens_seen >= 4096
        and _ins_flaky == "俏皮一点，语速偏快"
        and _flaky_llm.n == 2,
        "max_tokens=%d 空内容重试后=%r" % (_fake_llm.max_tokens_seen, _ins_flaky),
    )

    # ---------------------------------------------------- Qwen-Audio 情感/富语言标签（内置 SKILL）
    c.check(
        "情感标签门控：仅 Qwen-Audio-TTS 系列（非实时，排除 ASR / realtime）",
        is_qwen_audio_tts_model("qwen-audio-3.1-tts-flash") is True
        and is_qwen_audio_tts_model("qwen-audio-3.0-tts-plus") is True
        and is_qwen_audio_tts_model("qwen-audio-3.0-tts-flash") is True
        and is_qwen_audio_tts_model("qwen-audio-3.1-realtime-plus") is False
        and is_qwen_audio_tts_model("qwen-audio-3.0-asr-flash") is False
        and is_qwen_audio_tts_model("qwen3-tts-instruct-flash") is False
        and is_qwen_audio_tts_model("cosyvoice-v3-flash") is False
        and is_qwen_audio_tts_model("") is False,
    )
    _tag_msgs = build_qwen_audio_tag_messages(
        "哈哈，你居然信了？",
        {"name": "小满", "description": "活泼爱笑的上班族", "personality": "喜欢开玩笑"},
    )
    _tag_system = _tag_msgs[0]["content"]
    c.check(
        "标签 SKILL 提示词内置官方标签表（控制类 + 富语言类）且约束只插标签不改字",
        "[whispers]" in _tag_system and "[giggles]" in _tag_system and "[angry]" in _tag_system
        and "[empathetic]" in _tag_system and "[very fast]" in _tag_system and "[sighing]" in _tag_system
        and "一字不改" in _tag_system,
    )
    c.check(
        "标签 SKILL v3：官方语义 + 官方示例 + 30 个标签全覆盖精细映射 + 禁止中文自创标签",
        "直到遇到下一个控制类标签" in _tag_system
        and "或因句子较长被自动切分为止" in _tag_system
        and "不影响前后文本的情感风格" in _tag_system
        and "[excited]今天的天气真不错！[laughing]我们一起出去玩吧！" in _tag_system
        and "[serious]请注意安全事项。[excited]好了，现在让我们开始吧！" in _tag_system
        and "[like dracula]" in _tag_system and "[asmr]" in _tag_system and "[panicked]" in _tag_system
        and "[reluctantly]" in _tag_system and "[clears throat]" in _tag_system and "[snorts]" in _tag_system
        and "每句开头" in _tag_system
        and "不许自创、不许用中文标签" in _tag_system
        and "文本最开头" in _tag_system
        and "40 字以内" in _tag_system,
    )
    c.check(
        "标签文本解析：去围栏 / 前导语 / 引号；只保留官方标签；中文方括号视为正文",
        parse_tagged_text("```[whispers]嘘，小声点```") == "[whispers]嘘，小声点"
        and parse_tagged_text("改写后文本：[angry]快说！[happy]太好了") == "[angry]快说！[excited]太好了"
        and parse_tagged_text("[empathetic]别怕，有我呢。") == "[empathetic]别怕，有我呢。"
        and parse_tagged_text("文本：[哈哈]哈哈") == "[哈哈]哈哈"
        and parse_tagged_text('"[excited]走！"') == "[excited]走！"
        and parse_tagged_text("") == "",
    )
    c.check(
        "双输出解析：标签文本与整体语气指令分开提取（行内 / 跨行）",
        parse_tagged_output("文本：[empathetic]别怕，有我呢。\n指令：语速偏慢，音调放低，语气温柔关切")
        == ("[empathetic]别怕，有我呢。", "语速偏慢，音调放低，语气温柔关切")
        and parse_tagged_output("文本：[angry]快说！指令：语气强硬，语速偏快")
        == ("[angry]快说！", "语气强硬，语速偏快")
        and parse_tagged_output("[whispers]嘘，小声点") == ("[whispers]嘘，小声点", ""),
    )
    c.check(
        "标签校验：近似拼写归一 + 官方标签在场时剥除编造中文标签（防念出来）",
        parse_tagged_text("[laugh]哈哈哈") == "[laughing]哈哈哈"
        and parse_tagged_text("[Whispers]嘘，小声点") == "[whispers]嘘，小声点"
        and parse_tagged_text("[excited]太好了！[温柔催促]我们快走吧") == "[excited]太好了！我们快走吧"
        and parse_tagged_text("[happy]好呀") == "[excited]好呀"
        and parse_tagged_text("文本：[哈哈]哈哈") == "[哈哈]哈哈",
    )
    c.check(
        "本地兜底：高置信线索给标签，无线索用中性底色，永远给中性指令",
        local_qwen_audio_tags("哈哈，你居然信了？")[0].startswith("[giggles]")
        and local_qwen_audio_tags("快一点！要迟到了！")[0].startswith("[very fast]")
        and local_qwen_audio_tags("别怕，有我呢。")[0].startswith("[empathetic]")
        and local_qwen_audio_tags("哈哈，你居然信了？")[1]
        and local_qwen_audio_tags("好的，明天九点开会。")[0].startswith("[serious]")
        and local_qwen_audio_tags("好的，明天九点开会。")[1],
    )
    c.check(
        "qwen-audio 参数映射：GUI 调节值 → 官方 rate/pitch/volume（边界夹紧）",
        _voice_pkg.TTS._dashscope_qwen_audio_params("+10%", "", "+20Hz", 0.0)
        == {"rate": 1.1, "pitch": 1.1}
        and _voice_pkg.TTS._dashscope_qwen_audio_params("+10%", "+50%", "", 1.3)
        == {"rate": 1.3, "volume": 100}
        and _voice_pkg.TTS._dashscope_qwen_audio_params("-90%", "-90%", "-90Hz", 0.2)
        == {"rate": 0.5, "pitch": 0.55, "volume": 0}
        and _voice_pkg.TTS._dashscope_qwen_audio_params("", "", "", 0.0) == {},
    )

    class _FakeTagLLM:
        def __init__(self, reply: str):
            self._reply = reply
            self.max_tokens_seen = 0

        def configured(self) -> bool:
            return True

        async def chat(self, messages, **_kw):
            self.max_tokens_seen = int(_kw.get("max_tokens") or 0)
            return self._reply

    _fake_tag_llm = _FakeTagLLM("文本：[giggles]哈哈，你居然信了？\n指令：语速常速，音调轻快，带着调侃的笑意")
    _tag_loop = asyncio.new_event_loop()
    try:
        _tagged_gen = _tag_loop.run_until_complete(
            generate_qwen_audio_tags(
                _fake_tag_llm,
                "哈哈，你居然信了？",
                {"name": "小满", "personality": "喜欢开玩笑"},
            )
        )
        _tagged_none = _tag_loop.run_until_complete(generate_qwen_audio_tags(None, "测试"))
    finally:
        _tag_loop.close()
    c.check(
        "标签生成走主模型：标签文本与指令一起产出；不限制思考长度（max_tokens 4096）；LLM 缺失时返回空（走本地兜底）",
        _tagged_gen.get("text") == "[giggles]哈哈，你居然信了？"
        and _tagged_gen.get("instruction") == "语速常速，音调轻快，带着调侃的笑意"
        and _fake_tag_llm.max_tokens_seen >= 4096
        and _tagged_none == {"text": "", "instruction": ""},
        "生成=%r max_tokens=%d" % (_tagged_gen, _fake_tag_llm.max_tokens_seen),
    )

    # 本地 HTTP 服务冒充百炼：验证真实请求路径 / 参数 / 两种返回分支（Base64 与 audio.url）
    import base64 as _b64
    import http.server as _http_server
    import threading as _threading

    _mock_wav = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100
    _ds_captured: Dict[str, Any] = {}

    class _DashHandler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            _ds_captured["path"] = self.path
            _ds_captured["body"] = body
            model = str(body.get("model") or "")
            if model.startswith("urltest"):
                payload = {"output": {"audio": {"url": "http://127.0.0.1:%d/a.wav" % _ds_port, "data": ""}}}
            elif model.startswith("errormodel411"):
                out = json.dumps(
                    {"code": "InvalidParameter", "message": "[cosyvoice:]Engine error [411]: TTS speak operation failed"}
                ).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            elif model.startswith("errormodel"):
                out = json.dumps({"code": "InvalidParameter", "message": "mock error"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            else:
                payload = {"output": {"audio": {"url": "", "data": _b64.b64encode(_mock_wav).decode("ascii")}}}
            out = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(_mock_wav)))
            self.end_headers()
            self.wfile.write(_mock_wav)

    _ds_srv = _http_server.ThreadingHTTPServer(("127.0.0.1", 0), _DashHandler)
    _ds_port = _ds_srv.server_address[1]
    _threading.Thread(target=_ds_srv.serve_forever, daemon=True).start()
    try:
        _ds_tts = _TTS(
            ProviderSpec(
                slot="tts",
                engine="dashscope",
                base_url="http://127.0.0.1:%d/compatible-mode/v1" % _ds_port,
                api_key="sk-mock",
                model="qwen3-tts-flash",
                voice="Cherry",
            ),
            timeout=15,
        )
        loop4 = asyncio.new_event_loop()
        try:
            _ds_out = loop4.run_until_complete(_ds_tts.synthesize("你好，测试"))
        finally:
            loop4.close()
        c.check(
            "百炼合成走多模态原生端点（不是 /audio/speech），参数正确，Base64 分支解出音频",
            _ds_captured.get("path") == "/api/v1/services/aigc/multimodal-generation/generation"
            and (_ds_captured.get("body") or {}).get("model") == "qwen3-tts-flash"
            and ((_ds_captured.get("body") or {}).get("input") or {}).get("voice") == "Cherry"
            and ((_ds_captured.get("body") or {}).get("input") or {}).get("text") == "你好，测试"
            and _ds_out["data"] == _mock_wav
            and _ds_out["format"] == "wav",
            str(_ds_captured.get("path")),
        )
        _ds_tts2 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="cosyvoice-v3-flash", voice="longanyang"),
            timeout=15,
        )
        loop5 = asyncio.new_event_loop()
        try:
            loop5.run_until_complete(_ds_tts2.synthesize("测试"))
        finally:
            loop5.close()
        c.check(
            "cosyvoice 模型自动改走 SpeechSynthesizer 端点",
            _ds_captured.get("path") == "/api/v1/services/audio/tts/SpeechSynthesizer",
            str(_ds_captured.get("path")),
        )
        _ds_tts3 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="urltest-flash", voice="Cherry"),
            timeout=15,
        )
        loop6 = asyncio.new_event_loop()
        try:
            _ds_out3 = loop6.run_until_complete(_ds_tts3.synthesize("测试"))
        finally:
            loop6.close()
        c.check(
            "audio.url 分支：跟随临时地址下载音频",
            _ds_out3["data"] == _mock_wav,
        )
        _ds_tts4 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="errormodel-x", voice="Cherry"),
            timeout=15,
        )
        loop7 = asyncio.new_event_loop()
        try:
            _ds_err = ""
            try:
                loop7.run_until_complete(_ds_tts4.synthesize("测试"))
            except _voice_pkg.VoiceError as exc:
                _ds_err = str(exc)
        finally:
            loop7.close()
        c.check(
            "百炼返回业务错误时带出 code / message",
            "InvalidParameter" in _ds_err and "mock error" in _ds_err,
            _ds_err[:120],
        )
        # 指令风格：instruct 模型带 instructions + optimize_instructions + language_type
        _ds_tts5 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="qwen3-tts-instruct-flash", voice="Cherry"),
            timeout=15,
        )
        loop8 = asyncio.new_event_loop()
        try:
            loop8.run_until_complete(_ds_tts5.synthesize("今天天气真好，出去走走吧！", instruction="语气轻快明朗，语速偏快，带点兴奋"))
        finally:
            loop8.close()
        _ins_body = (_ds_captured.get("body") or {}).get("input") or {}
        c.check(
            "instruct 模型请求体带 instructions + optimize_instructions，中文自动带 language_type",
            _ins_body.get("instructions") == "语气轻快明朗，语速偏快，带点兴奋"
            and _ins_body.get("optimize_instructions") is True
            and _ins_body.get("language_type") == "Chinese",
            str(_ds_captured.get("body")),
        )
        _ds_tts6 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="qwen3-tts-flash", voice="Cherry"),
            timeout=15,
        )
        loop9 = asyncio.new_event_loop()
        try:
            loop9.run_until_complete(_ds_tts6.synthesize("测试", instruction="不该被发送的指令"))
        finally:
            loop9.close()
        _flash_body = (_ds_captured.get("body") or {}).get("input") or {}
        c.check(
            "非 instruct 模型不发 instructions（官方：仅 Instruct 系列支持），language_type 仍自动判定",
            "instructions" not in _flash_body
            and "optimize_instructions" not in _flash_body
            and _flash_body.get("language_type") == "Chinese",
            str(_ds_captured.get("body")),
        )
        # 411（音色与模型不同系列）错误带排查提示
        _ds_tts7 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="errormodel411-x", voice="Cherry"),
            timeout=15,
        )
        loop10 = asyncio.new_event_loop()
        try:
            _e411 = ""
            try:
                loop10.run_until_complete(_ds_tts7.synthesize("测试"))
            except _voice_pkg.VoiceError as exc:
                _e411 = str(exc)
        finally:
            loop10.close()
        c.check(
            "411 错误提示三族音色不能混用",
            "Engine error [411]" in _e411 and "不同系列" in _e411 and "yuxiaoyun_v3.1" in _e411,
            _e411[:160],
        )
        # 情感标签 + 全量官方参数：qwen-audio 模型的标签文本进 text，
        # instruction（单数）/ rate / format / sample_rate / language_hints 一并发送
        _ds_tts8 = _TTS(
            ProviderSpec(slot="tts", engine="dashscope", base_url="http://127.0.0.1:%d" % _ds_port, api_key="sk-mock", model="qwen-audio-3.1-tts-flash", voice="yuxiaoyun_v3.1"),
            timeout=15,
        )
        loop11 = asyncio.new_event_loop()
        try:
            loop11.run_until_complete(
                _ds_tts8.synthesize(
                    "[whispers]嘘，小声点，别让她听见",
                    instruction="语速偏慢，音调压低，像在叮嘱",
                    speed=1.2,
                )
            )
        finally:
            loop11.close()
        _qa_body = (_ds_captured.get("body") or {}).get("input") or {}
        c.check(
            "qwen-audio 请求体：标签随 text + instruction（单数）+ rate + format/sample_rate + language_hints",
            _qa_body.get("text") == "[whispers]嘘，小声点，别让她听见"
            and _qa_body.get("instruction") == "语速偏慢，音调压低，像在叮嘱"
            and "instructions" not in _qa_body
            and _qa_body.get("rate") == 1.2
            and _qa_body.get("format") == "wav"
            and _qa_body.get("sample_rate") == 24000
            and _qa_body.get("language_hints") == ["zh"],
            str(_ds_captured.get("body")),
        )
    finally:
        _ds_srv.shutdown()

    # ---------------------------------------------------------- 视觉图片挂接位置 + MIME 识别
    from bot.ai_engine.engine import AIEngine
    from bot.media.images import image_data_url

    _jpeg_tmp = Path(tempfile.mkdtemp(prefix="tavern-vision-")) / "noext"
    _jpeg_tmp.write_bytes(b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10)
    _png_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10
    _gif_bytes = b"GIF89a" + b"fake-gif" * 10
    _webp_bytes = b"RIFF\x00\x00\x00\x00WEBP" + b"fake" * 10

    c.check(
        "图片 MIME 按文件头识别（无点文件名不再误标）",
        image_data_url(str(_jpeg_tmp)).startswith("data:image/jpeg;base64,")
        and image_data_url(None, _png_bytes).startswith("data:image/png;base64,")
        and image_data_url(None, _gif_bytes).startswith("data:image/gif;base64,")
        and image_data_url(None, _webp_bytes).startswith("data:image/webp;base64,"),
    )

    _messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "早期的历史消息"},
        {"role": "assistant", "content": "嗯嗯"},
        {"role": "user", "content": "【图片】（用户发来了 1 张图片，请看图内容）"},
    ]
    _upgraded = AIEngine._with_image(_messages, "（用户发来了 1 张图片，请看图内容）", [str(_jpeg_tmp)])
    c.check(
        "图片挂在最后一条 user 消息（当前消息），早期历史 user 消息不被改动",
        _upgraded[1]["content"] == "早期的历史消息"
        and isinstance(_upgraded[3]["content"], list)
        and any(part.get("type") == "image_url" for part in _upgraded[3]["content"])
        and any("【图片】" not in str(part.get("text") or "") for part in _upgraded[3]["content"]),
        str(_upgraded[3]["content"])[:200],
    )
    c.check(
        "挂接后当前消息文本去掉【图片】前缀并保留文字",
        [part.get("text") for part in _upgraded[3]["content"] if part.get("type") == "text"]
        == ["（用户发来了 1 张图片，请看图内容）"],
        str(_upgraded[3]["content"])[:200],
    )

    # ---------------------------------------------------------- 旧通道痕迹
    c.check(
        "NapCat / OneBot 相关的模块目录已被删除",
        not (ROOT / "bot" / "qq_adapter").exists()
        and not (ROOT / "common" / "napcat_download.py").exists()
        and not (ROOT / "app" / "napcat_ui.py").exists(),
    )
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    c.check(
        "依赖里不再有 nonebot / onebot",
        "nonebot" not in requirements and "onebot" not in requirements,
        requirements[:200],
    )
    bot_main_source = (ROOT / "bot" / "main.py").read_text(encoding="utf-8").lower()
    c.check(
        "bot/main.py 不再依赖 nonebot（改为 FastAPI + uvicorn）",
        "nonebot" not in bot_main_source and "fastapi" in bot_main_source,
    )


# ============================================================== 配置写入 =====
def write_bot_config(data_dir: Path) -> Path:
    """写入测试用配置（QQ 官方机器人通道，指向 mock 官方平台与 mock LLM）。

    官方平台只能按 openid 发消息，因此 ``target_openid`` / ``group_openid`` 留空：
    自检会先给机器人发一条单聊消息，让它自动记住 openid，再验证主动消息。
    """
    import yaml

    config = {
        "llm": {
            "base_url": "%s/v1" % MOCK_URL,
            "api_key": "mock-key",
            "model": "mock-model",
            "max_tokens": 128,
            "temperature": 0.5,
            "timeout": 20,
            "max_retries": 1,
            "fallback_messages": ["兜底话术"],
        },
        "qq": {
            "id": "bot1",
            "name": "自检机器人",
            "enabled": True,
            "character_id": "",
            "user_nickname": "小可爱",
            "reply_enabled": True,
            "group_reply_enabled": False,
            "official": {
                "app_id": mock_servers.OFFICIAL_APP_ID,
                "app_secret": mock_servers.OFFICIAL_APP_SECRET,
                "sandbox": False,
                "intents": OFFICIAL_INTENTS,
                "api_domain": MOCK_URL,
                "token_url": "%s/app/getAppAccessToken" % MOCK_URL,
                "gateway_path": "/gateway",
                "target_openid": "",
                "group_openid": "",
                "allow_all_users": True,
                "allowed_users": [],
                "allowed_groups": [],
                "markdown": False,

                "max_reply_segments": 3,
                "reply_segment_max_len": 60,
            },
        },
        "proactive": {
            "enabled": True,
            "scheduled_enabled": False,
            "idle_enabled": False,
            "random_enabled": False,
            "min_interval_minutes": 0,
            "probability": 1.0,
            "global_daily_limit": 10,
            "per_character_daily_limit": 3,
            "avoid_repeat": True,
            "active_hours": {"enabled": False, "start": "00:00", "end": "23:59"},
            "dnd_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
            "max_message_chars": 60,
            "context_messages": 6,
        },
        "memory": {"short_term_max": 10, "long_term_retrieve": 5, "auto_extract": True},
        "characters": {"import_builtin": False},
        # V0.2 富媒体：默认关语音回复概率，保证自检的回复是确定性的纯文字；
        # 需要富媒体行为的用例（official_smoke 富媒体段）再单独把概率调上去。
        "media": {"enabled": True, "voice_reply_probability": 0.0, "allow_image": True},
        "api": {"host": "127.0.0.1", "port": API_PORT, "token": ""},
        "logging": {"level": "INFO", "console": True},
    }
    path = data_dir / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def start_bot(data_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["QQAI_DATA_DIR"] = str(data_dir)
    env["QQAI_HOME"] = str(ROOT)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = 0x08000000 if os.name == "nt" else 0
    return subprocess.Popen(
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
        creationflags=creationflags,
    )


def drain(process: subprocess.Popen) -> List[str]:
    """后台读取子进程输出，返回收集列表（用于失败时排查）。"""
    lines: List[str] = []

    def _reader() -> None:
        try:
            for line in process.stdout:  # type: ignore[union-attr]
                lines.append(line.rstrip())
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    return lines


def clear_logs(data_dir: Path) -> None:
    """启动 Bot 前先删掉数据目录里的旧日志，避免读到上一次运行的残留。"""
    for name in ("bot.log", "bot.log.1", "gui.log"):
        try:
            (Path(data_dir) / "logs" / name).unlink()
        except Exception:
            pass


def dump_diagnostics(data_dir: Path, bot_output: List[str], mock, label: str) -> None:
    """失败时打印 Bot 日志与 mock 状态，便于定位问题。"""
    print("\n---- 诊断信息（%s）----" % label)
    log_file = data_dir / "logs" / "bot.log"
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = [line for line in lines if "[DEBUG] aiosqlite" not in line]
        print("\n".join(lines[-25:]))
    else:
        print("（未找到 bot.log）")
    print("---- bot 进程输出 ----")
    print("\n".join(bot_output[-25:]))
    try:
        state = mock.state()
        print("---- mock 状态 ----")
        print(
            "llm_calls=%s token_calls=%s official_ws=%s official_sent=%s"
            % (
                state.get("llm_calls"),
                state.get("token_calls"),
                state.get("official_ws"),
                json.dumps(state.get("official_sent"), ensure_ascii=False),
            )
        )
    except Exception as exc:
        print("无法读取 mock 状态: %s" % exc)
    print("---- 诊断结束 ----\n")


def free_port() -> int:
    """申请一个空闲端口（避免固定端口在不同运行之间互相干扰）。"""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def phase_e2e(c: Checker) -> None:
    c.phase("2/4 启动 mock 官方平台与 Bot 进程")

    global API_PORT, MOCK_PORT, BASE_URL, MOCK_URL
    API_PORT = free_port()
    MOCK_PORT = free_port()
    BASE_URL = "http://127.0.0.1:%d" % API_PORT
    MOCK_URL = "http://127.0.0.1:%d" % MOCK_PORT
    print("  本次使用端口：控制接口 %d，mock 平台 %d" % (API_PORT, MOCK_PORT))

    mock = mock_servers.MockProcess(port=MOCK_PORT).start()
    mock.reset(reply_text="在的呀，今天怎么样？", proactive_text="突然有点想你了。")

    c.check("mock 官方平台已启动（独立进程）", True)

    data_dir = Path(tempfile.mkdtemp(prefix="tavern-e2e-"))
    write_bot_config(data_dir)
    clear_logs(data_dir)
    process = start_bot(data_dir)
    bot_output = drain(process)
    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    healthy = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=90)
    c.check("Bot 进程启动并响应 /api/health", healthy)
    if not healthy:
        print("---- bot 输出 ----")
        print("\n".join(bot_output[-50:]))
        process.terminate()
        wait_for(lambda: process.poll() is not None, timeout=10)
        client.close()
        mock.stop()
        return

    try:
        _run_e2e_checks(c, client, mock, data_dir, process, bot_output)
    finally:
        if process.poll() is None:
            process.terminate()
            wait_for(lambda: process.poll() is not None, timeout=10)
        client.close()
        mock.stop()


def _run_e2e_checks(
    c: Checker,
    client: httpx.Client,
    mock: Any,
    data_dir: Path,
    process: subprocess.Popen,
    bot_output: List[str],
) -> None:
    # ------------------------------------------------------------- 网关连接
    c.check(
        "官方机器人网关已连接（Hello → Identify → READY）",
        wait_for(lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")), timeout=90),
        "mock 未收到 Identify：%s" % (mock.state().get("official_identify"),),
    )

    # ------------------------------------------------------------- 状态
    c.phase("3/4 官方通道：状态 / 角色 / 单聊回复 / 记忆 / 接口")
    status = client.get("/api/status").json()
    c.check("状态显示 Bot 在线", bool(status.get("online")), json.dumps(status.get("proactive"), ensure_ascii=False))
    c.check("默认连接方式为官方机器人", status.get("qq_mode") == "official", str(status.get("qq_mode")))
    c.check(
        "状态里带有可读的模式名",
        "官方" in str(status.get("qq_mode_label")),
        str(status.get("qq_mode_label")),
    )
    c.check("状态里已没有 napcat 字段", "napcat" not in status, str(sorted(status.keys())))
    c.check(
        "状态里已没有 target_user_id / 旧的 QQ 号字段",
        "target_user_id" not in status and "target_user_id" not in (status.get("qq") or {}),
        str(sorted((status.get("qq") or {}).keys())),
    )
    c.check(
        "自动读到官方机器人身份（READY 里的 user）",
        int(status.get("self_id") or 0) == 1000000001,
        str(status.get("self_id")),
    )
    c.check("LLM 配置被识别", bool((status.get("llm") or {}).get("configured")))
    c.check("状态里报告机器人数量", int(status.get("bot_count") or 0) == 1, str(status.get("bot_count")))

    qq = client.get("/api/qq/status").json()
    c.check(
        "网关会话与事件计数可读",
        bool((qq.get("gateway") or {}).get("session_id")) and int((qq.get("gateway") or {}).get("last_seq", 0)) >= 1,
        str(qq.get("gateway")),
    )
    c.check(
        "鉴权时上报了官方 intents",
        int(mock.state().get("official_intents") or 0) == OFFICIAL_INTENTS,
        str(mock.state().get("official_intents")),
    )

    # ------------------------------------------------------------- HTTP 接口面
    get_endpoints = [
        "/api/health",
        "/api/status",
        "/api/stats/today",
        "/api/logs?source=bot&lines=20",
        "/api/bots",
        "/api/qq/status",
        "/api/characters",
        "/api/conversations",
        "/api/config",
        "/api/proactive/status",
        "/api/proactive/logs",
    ]
    bad = []
    for path in get_endpoints:
        try:
            if client.get(path, timeout=20).status_code != 200:
                bad.append(path)
        except Exception as exc:
            bad.append("%s(%s)" % (path, exc))
    c.check("HTTP 查询接口全部可用", not bad, str(bad))

    legacy_probe = client.get("/api/napcat/probe", timeout=20)
    c.check(
        "旧的 NapCat 探测接口已下线（404）",
        legacy_probe.status_code == 404,
        str(legacy_probe.status_code),
    )
    probe = client.post("/api/qq/test", timeout=30).json()
    c.check("「测试连接」返回可用", bool(probe.get("available")), json.dumps(probe, ensure_ascii=False)[:200])
    c.check("测试结果标注为官方模式", probe.get("mode") == "official", str(probe.get("mode")))
    c.check(
        "测试结果带上机器人昵称与 openid 目标说明",
        bool(probe.get("nickname")),
        json.dumps(probe, ensure_ascii=False)[:200],
    )
    stopped = client.post("/api/bot/stop", timeout=20).json()
    offline = client.get("/api/status").json()
    restarted_bot = client.post("/api/bot/start", timeout=20).json()
    online = client.get("/api/status").json()
    c.check(
        "Bot 停止 / 启动接口能切换调度器状态",
        bool(stopped.get("ok"))
        and not bool(offline.get("online"))
        and bool(restarted_bot.get("ok"))
        and bool(online.get("online")),
        "stop=%s online=%s start=%s online=%s"
        % (stopped.get("ok"), offline.get("online"), restarted_bot.get("ok"), online.get("online")),
    )
    c.check("调度器已重新运行", bool(client.get("/api/status").json().get("online")))
    c.check("配置重载接口可用", bool(client.post("/api/config/reload", timeout=20).json().get("ok")))
    c.check(
        "重连接口可用（重建官方网关连接）",
        bool(client.post("/api/qq/reconnect", timeout=30).json().get("ok")),
    )
    c.check(
        "重连后网关重新连上",
        wait_for(lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")), timeout=60),
    )

    # ------------------------------------------------------------- 角色导入
    cards_dir = data_dir / "cards"
    cards_dir.mkdir(exist_ok=True)
    png_path = card_factory.write_png_card(cards_dir / "深夜角色.png", "深夜角色")
    json_path = card_factory.write_json_card(cards_dir / "元气角色.json", "元气角色")
    yaml_path = card_factory.write_yaml_card(cards_dir / "吐槽角色.yaml", "吐槽角色")

    for path in (png_path, json_path, yaml_path):
        with open(path, "rb") as handle:
            response = client.post(
                "/api/characters/import",
                files={"file": (path.name, handle.read(), "application/octet-stream")},
            )
        c.check("导入角色卡 %s" % path.name, response.status_code == 200, response.text[:200])

    characters = client.get("/api/characters").json()
    c.check("角色列表包含 3 个角色", len(characters) == 3, str(len(characters)))
    c.check("角色卡带出头像文件", any(item.get("avatar_path") for item in characters))

    # ---------------------------------------------------- 自定义角色 / 内置角色
    created = client.post(
        "/api/characters",
        json={
            "name": "自己写的角色",
            "description": "{{char}} 是用户自己写的测试角色",
            "personality": "话少",
            "first_mes": "在的",
            "system_prompt": "简短回复",
        },
        timeout=30,
    )
    c.check("可以不用角色卡直接新建角色", created.status_code == 200, created.text[:200])
    if created.status_code == 200:
        row = created.json()["character"]
        c.check("新建的角色默认启用", int(row.get("enabled") or 0) == 1, str(row.get("enabled")))
        c.check("新建角色标记为 custom", str(row.get("card_spec")) == "custom", str(row.get("card_spec")))
        modified = client.put(
            "/api/characters/%s" % row["id"],
            json={"personality": "其实话很多"},
            timeout=30,
        )
        c.check("新建的角色可以直接编辑", modified.status_code == 200, modified.text[:160])
        duplicate = client.post("/api/characters", json={"name": "自己写的角色"}, timeout=30)
        c.check("重名会被拒绝并给出提示", duplicate.status_code == 422, str(duplicate.status_code))
        client.delete("/api/characters/%s" % row["id"], timeout=30)

    builtin = client.post("/api/characters/import-builtin", timeout=60).json()
    builtin_names = [str((item.get("character") or {}).get("name")) for item in (builtin.get("imported") or [])]
    c.check("可以导入内置默认角色", len(builtin_names) == 3, str(builtin_names))
    c.check(
        "内置角色名字符合预期",
        set(builtin_names) == {"小栖", "阿元", "苏苏"},
        str(sorted(builtin_names)),
    )
    c.check(
        "内置角色都带上了人格字段",
        all(
            (item.get("character") or {}).get("personality")
            for item in (builtin.get("imported") or [])
        ),
    )
    png_character = next((item for item in characters if item["name"] == "深夜角色"), None)
    if png_character:
        avatar = client.get("/api/characters/%s/avatar" % png_character["id"])
        c.check("头像接口返回图片", avatar.status_code == 200 and avatar.content[:4] == b"\x89PNG")

    target_character = characters[0]
    total_characters = len(client.get("/api/characters").json())
    client.patch("/api/characters/%s/enabled" % target_character["id"], json={"enabled": False})
    enabled_list = client.get("/api/characters", params={"enabled_only": True}).json()
    c.check(
        "禁用角色后启用数减 1",
        len(enabled_list) == total_characters - 1,
        "%d / %d" % (len(enabled_list), total_characters),
    )
    client.patch("/api/characters/%s/enabled" % target_character["id"], json={"enabled": True})
    c.check(
        "重新启用角色",
        len(client.get("/api/characters", params={"enabled_only": True}).json()) == total_characters,
    )

    # ------------------------------------------------------------- 单聊回复
    mock.reset(reply_text="在的呀，今天怎么样？", proactive_text="突然有点想你了。")
    emitted = mock.emit_c2c("你好呀，今天有点累")
    c.check("mock 网关把单聊事件推给了机器人", bool(emitted.get("delivered")), str(emitted))

    def c2c_records() -> List[Dict[str, Any]]:
        return [item for item in mock.official_sent() if item.get("kind") == "c2c"]

    replied = wait_for(lambda: len(c2c_records()) >= 1, timeout=45)
    c.check("收到单聊消息后自动回复（走官方 REST 接口）", replied, str(mock.official_sent())[:300])
    if not replied:
        dump_diagnostics(data_dir, bot_output, mock, "单聊回复失败")
    if replied:
        record = c2c_records()[0]
        c.check("回复发给了正确的 openid", record.get("openid") == "mock-user-openid", str(record.get("openid")))
        c.check("回复内容来自 LLM", "今天怎么样" in str(record.get("content")), str(record.get("content")))
        c.check(
            "被动回复带 msg_id 与递增的 msg_seq（官方要求）",
            record.get("msg_id") == emitted.get("id") and int(record.get("msg_seq") or 0) >= 1,
            str({k: record.get(k) for k in ("msg_id", "msg_seq")}),
        )
        c.check("消息类型为文本（msg_type=0）", int(record.get("msg_type", -1)) == 0, str(record.get("msg_type")))

    # openid 自动记忆（官方平台只能按 openid 发主动消息）
    c.check(
        "自动记住了最近的私聊 openid",
        wait_for(
            lambda: (client.get("/api/qq/status").json() or {}).get("last_user_openid") == "mock-user-openid",
            timeout=20,
        ),
        str(client.get("/api/qq/status").json().get("last_user_openid")),
    )

    conversations = client.get("/api/conversations").json()
    active = [item for item in conversations if int(item.get("message_count") or 0) > 0]
    c.check("会话列表记录了对话", bool(active), str(conversations)[:200])
    if active:
        character_id = active[0]["character_id"]
        rows = client.get("/api/conversations/%s/messages" % character_id).json()["messages"]
        c.check("对话包含用户与角色两条消息", len(rows) >= 2, str(len(rows)))
        c.check("消息角色标注正确", [row["role"] for row in rows[:2]] == ["user", "assistant"])

    # ------------------------------------------------------------- 长期记忆
    mock.emit_c2c("我叫小明，我喜欢在深夜写代码，记得哦")
    wait_for(lambda: len(c2c_records()) >= 2, timeout=45)

    memory_found = False
    for item in client.get("/api/conversations").json():
        if int(item.get("message_count") or 0) <= 0:
            continue
        memories = client.get("/api/conversations/%s/memories" % item["character_id"]).json()
        if any("深夜写代码" in str(entry.get("content")) for entry in memories):
            memory_found = True
            break
    c.check("自动抽取长期记忆", memory_found)

    if active:
        manual_memory = client.post(
            "/api/conversations/%s/memories" % active[0]["character_id"],
            json={"content": "用户讨厌被催稿"},
        )
        c.check("手动添加记忆", manual_memory.status_code == 200)
        memories = client.get("/api/conversations/%s/memories" % active[0]["character_id"]).json()
        c.check("记忆列表可读", any("讨厌被催稿" in str(item["content"]) for item in memories))
        if memories:
            deleted = client.delete("/api/memories/%d" % int(memories[0]["id"]))
            c.check("删除记忆", deleted.status_code == 200)

    # ------------------------------------------------------------- 群聊 @ 回复
    client.put(
        "/api/config",
        json={"qq": {"group_reply_enabled": True, "official": {"allowed_groups": []}}},
        timeout=30,
    )
    time.sleep(1.5)
    mock.emit_group("群里在聊什么？")
    group_replied = wait_for(
        lambda: any(item.get("kind") == "group" for item in mock.official_sent()), timeout=45
    )
    c.check("群聊 @ 时回复到群里（走 /v2/groups/... ）", group_replied, str(mock.official_sent())[:300])
    if group_replied:
        group_record = next(item for item in mock.official_sent() if item.get("kind") == "group")
        c.check(
            "群回复发给了正确的 group_openid",
            group_record.get("group_openid") == "mock-group-openid",
            str(group_record.get("group_openid")),
        )
        c.check(
            "群回复同样带 msg_id（被动回复）",
            bool(group_record.get("msg_id")),
            str(group_record.get("msg_id")),
        )

    # ------------------------------------------------------------- 主动消息
    c.phase("4/4 主动消息 / 限流 / 多机器人 / 推送 / 退出")
    before = len(c2c_records())
    result = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
    c.check("主动消息触发成功", bool(result.get("ok")), json.dumps(result, ensure_ascii=False)[:300])
    c.check("主动消息由某个角色发出", bool(result.get("character")), str(result.get("character")))
    c.check(
        "主动消息内容来自 LLM 的主动分支",
        "想你了" in str(result.get("content")),
        str(result.get("content")),
    )
    c.check("主动消息真实发送到官方平台", len(c2c_records()) > before)
    if len(c2c_records()) > before:
        proactive_record = c2c_records()[-1]
        c.check(
            "主动消息发给记住的 openid",
            proactive_record.get("openid") == "mock-user-openid",
            str(proactive_record.get("openid")),
        )
        c.check(
            "主动消息不带 msg_id（官方「主动消息」形态）",
            not proactive_record.get("msg_id"),
            str({k: proactive_record.get(k) for k in ("msg_id", "msg_seq")}),
        )

    stats = client.get("/api/stats/today").json()
    c.check("今日统计计入主动消息", int(stats.get("proactive_total") or 0) >= 1, str(stats))
    proactive_logs = client.get("/api/proactive/logs").json()
    c.check("主动消息写入日志表", any(item.get("trigger_type") == "manual" for item in proactive_logs))

    first_character = result.get("character_id")
    second = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
    c.check(
        "避免连续由同一角色发言",
        bool(second.get("ok")) and second.get("character_id") != first_character,
        "%s -> %s" % (first_character, second.get("character_id")),
    )

    start = time.strftime("%H:%M", time.localtime(time.time() - 3600))
    end = time.strftime("%H:%M", time.localtime(time.time() + 3600))
    client.put("/api/config", json={"proactive": {"dnd_hours": {"enabled": True, "start": start, "end": end}}})
    skipped = client.post("/api/proactive/trigger", json={"force": False}, timeout=60).json()
    c.check("免打扰时段拒绝发送", bool(skipped.get("skipped")), json.dumps(skipped, ensure_ascii=False))
    c.check("给出可读的跳过原因", "免打扰" in str(skipped.get("reason")), str(skipped.get("reason")))

    client.put(
        "/api/config",
        json={"proactive": {"dnd_hours": {"enabled": False}, "global_daily_limit": 1}},
    )
    limited = client.post("/api/proactive/trigger", json={"force": False}, timeout=60).json()
    c.check("达到每日上限后拒绝发送", bool(limited.get("skipped")), json.dumps(limited, ensure_ascii=False))
    c.check("上限原因可读", "上限" in str(limited.get("reason")), str(limited.get("reason")))

    # ------------------------------------------------------------- openid 遗忘
    # 先把每日上限放开，否则下面会先撞上「今日上限」而不是「不知道发给谁」
    client.put("/api/config", json={"proactive": {"global_daily_limit": 50}})
    c.check("清除 openid 接口可用", bool(client.post("/api/qq/forget-openid", timeout=20).json().get("ok")))
    forgotten = client.post("/api/proactive/trigger", json={"force": False}, timeout=60).json()
    c.check(
        "忘记 openid 后无法发送主动消息（官方必须知道发给谁）",
        bool(forgotten.get("skipped")),
        json.dumps(forgotten, ensure_ascii=False)[:200],
    )
    c.check(
        "给出了「先在 QQ 里发一条消息」的可读提示",
        "openid" in str(forgotten.get("reason")),
        str(forgotten.get("reason")),
    )
    mock.emit_c2c("我回来啦")
    c.check(
        "再收到一条单聊后又记住 openid",
        wait_for(
            lambda: (client.get("/api/qq/status").json() or {}).get("last_user_openid") == "mock-user-openid",
            timeout=30,
        ),
    )

    # ------------------------------------------------------------- 配置热更新
    client.put("/api/config", json={"proactive": {"probability": 0.55}})
    config_payload = client.get("/api/config").json()
    c.check(
        "配置热更新生效",
        abs(float(config_payload["config"]["proactive"]["probability"]) - 0.55) < 1e-6,
        str(config_payload["config"]["proactive"]["probability"]),
    )
    on_disk = (data_dir / "config.yaml").read_text(encoding="utf-8")
    c.check("配置写回磁盘", "0.55" in on_disk)
    c.check(
        "热更新后官方凭据仍在（没有被写没）",
        str((config_payload["config"].get("qq", {}).get("official", {}) or {}).get("app_id")) == mock_servers.OFFICIAL_APP_ID,
        str((config_payload["config"].get("qq", {}).get("official", {}) or {}).get("app_id")),
    )

    llm_test = client.post("/api/llm/test", timeout=60).json()
    c.check("LLM 连通性测试通过", bool(llm_test.get("ok")), str(llm_test))

    # ------------------------------------------------------------- 多机器人
    created_bot = client.post("/api/bots", json={"name": "二号机器人"}, timeout=30).json()
    c.check("可以新增一个机器人", bool(created_bot.get("ok")), json.dumps(created_bot, ensure_ascii=False)[:200])
    bots = client.get("/api/bots").json()
    c.check("机器人列表变成 2 个", int(bots.get("count") or 0) == 2, str(bots.get("count")))
    if created_bot.get("bot"):
        bot_id = str(created_bot["bot"].get("id") or "")
        updated = client.put(
            "/api/bots/%s" % bot_id,
            json={
                "name": "二号机器人",
                "official": {
                    "app_id": mock_servers.OFFICIAL_APP_ID,
                    "app_secret": mock_servers.OFFICIAL_APP_SECRET,
                    "api_domain": MOCK_URL,
                    "token_url": "%s/app/getAppAccessToken" % MOCK_URL,
                },
            },
            timeout=30,
        )
        c.check("可以给机器人填官方凭据", updated.status_code == 200, updated.text[:200])
        c.check(
            "第二个机器人的官方网关也连上了（多机器人各自一条长连接）",
            wait_for(
                lambda: any(
                    item.get("id") == bot_id and item.get("connected")
                    for item in (client.get("/api/bots").json().get("bots") or [])
                ),
                timeout=60,
            ),
            str(client.get("/api/bots").json().get("bots")),
        )
        c.check("可以删除机器人", bool(client.delete("/api/bots/%s" % bot_id, timeout=30).json().get("ok")))
        c.check(
            "删除后回到 1 个机器人",
            int(client.get("/api/bots").json().get("count") or 0) == 1,
            str(client.get("/api/bots").json().get("count")),
        )

    # ------------------------------------------------------- WebSocket 推送
    ws_event: Dict[str, Any] = {}

    def _ws_listen() -> None:
        try:
            from websockets.sync.client import connect

            with connect("ws://127.0.0.1:%d/ws/events" % API_PORT, open_timeout=8) as socket:
                deadline = time.time() + 60
                triggered = False
                while time.time() < deadline:
                    payload = json.loads(socket.recv(timeout=5))
                    if payload.get("type") == "hello" and not triggered:
                        triggered = True
                        client.post("/api/proactive/trigger", json={"force": True}, timeout=120)
                    if payload.get("type") == "proactive_sent":
                        ws_event.update(payload)
                        return
        except Exception as exc:
            ws_event["error"] = str(exc)

    ws_thread = threading.Thread(target=_ws_listen, daemon=True)
    ws_thread.start()
    ws_thread.join(timeout=70)
    c.check(
        "WebSocket 推送主动消息事件",
        ws_event.get("type") == "proactive_sent",
        str(ws_event)[:300],
    )

    # ------------------------------------------------------------- 日志与退出
    log_file = data_dir / "logs" / "bot.log"
    c.check("生成 Bot 日志文件", log_file.exists())
    if log_file.exists():
        content = log_file.read_text(encoding="utf-8", errors="replace")
        c.check("日志记录了主动消息", "主动消息" in content)
        c.check("日志记录了消息回复", "回复" in content)
        c.check("日志记录了官方网关就绪", "官方" in content)

    shutdown = client.post("/api/shutdown", timeout=15)
    c.check("优雅退出接口返回", shutdown.status_code == 200)
    exited = wait_for(lambda: process.poll() is not None, timeout=25)
    if not exited:
        process.terminate()
        wait_for(lambda: process.poll() is not None, timeout=10)
    c.check("Bot 进程优雅退出", exited)

    if not exited:
        print("---- bot 输出 ----")
        print("\n".join(bot_output[-50:]))


def main() -> int:
    # 单元自检在独立进程中运行：测试主进程保持「干净」（不创建 asyncio 事件循环），
    # 之后再启动 mock 与 Bot 子进程，避免 Windows 上 proactor 事件循环与
    # subprocess 之间的句柄继承问题导致监听套接字异常。
    if "--unit-only" in sys.argv:
        checker = Checker()
        phase_unit_logic(checker)
        print("##RESULT## " + json.dumps({"passed": checker.passed, "failed": checker.failed}, ensure_ascii=False))
        return checker.summary()

    checker = Checker()
    print("BaiAi-Tavern 自检开始（QQ 官方机器人通道，Python %s）" % sys.version.split()[0])

    checker.phase("1/4 单元自检：解析 / 规则 / 文本处理（独立子进程）")
    result = subprocess.run(
        [sys.executable, "-m", "tests.smoke_test", "--unit-only"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
        close_fds=True,
    )
    print(result.stdout.replace("##RESULT##", "# 子进程结果 #").rstrip())
    payload = None
    for line in (result.stdout or "").splitlines():
        if line.startswith("##RESULT##"):
            try:
                payload = json.loads(line[len("##RESULT##") :].strip())
            except Exception:
                payload = None
    if payload:
        checker.passed += int(payload.get("passed") or 0)
        checker.failed.extend(payload.get("failed") or [])
    else:
        checker.check(
            "单元自检子进程正常结束",
            False,
            (result.stderr or result.stdout or "")[-400:],
        )

    try:
        phase_e2e(checker)
    except Exception as exc:
        import traceback

        checker.check("端到端自检未抛出异常", False, str(exc))
        traceback.print_exc()
    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
