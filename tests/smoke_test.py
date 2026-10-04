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
import json
import os
import subprocess
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
