"""多机器人 / 多角色自检：两个 QQ 官方机器人各自绑定一个角色，互不串台。

运行::

    python -m tests.multibot_smoke

两个机器人**都是 QQ 官方机器人**（AppID + AppSecret → access_token → WebSocket 网关），
因此这里给每个机器人配一份 mock 开放平台：mock 只把事件推给自己那条网关连接，
和真实平台「一个应用一条网关、只收到自己的事件」完全一致。断言内容：

1. ``qq:`` 段 = 第 1 个机器人，``bots:`` 段 = 第 2 个机器人，各自的凭据与目标独立；
2. 每个机器人只回复发给自己的单聊事件，且只用自己绑定的角色（对话不串台）；
3. ``#角色名`` 可以临时换人（覆盖机器人绑定）；
4. 主动消息按机器人分别发送：各发各的 openid，用各自的角色；
5. ``bot_id="all"`` 让所有已启用的机器人各发一条；
6. 机器人被停用后跳过并给出可读原因，且不再收发消息；
7. 第一个机器人不能删除，第 2 个可以删除；配置里的 ``bots:`` 段同步清空。
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

import httpx  # noqa: E402

from tests import mock_servers, smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 每个机器人自己的主动消息目标 openid（互不相同，用来验证“各发各的”）
TARGET_A = "user-a"
TARGET_B = "user-b"
REPLY_TEXT = "这是角色回复。"
PROACTIVE_TEXT = "这是主动消息。"

# 历史遗留键（旧版 NapCat / OneBot 配置字段）：配置里不应该出现
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


def official_values(mock, target_openid: str, app_id: str = mock_servers.OFFICIAL_APP_ID) -> Dict[str, Any]:
    """某个机器人的官方凭据（指向它自己的 mock 开放平台实例）。"""
    return {
        "app_id": app_id,
        "app_secret": mock_servers.OFFICIAL_APP_SECRET,
        "api_domain": mock.base_url,
        "token_url": "%s/app/getAppAccessToken" % mock.base_url,
        "sandbox": False,
        "target_openid": target_openid,
        "group_openid": "",
        "allow_all_users": True,
        "allowed_users": [],
        "allowed_groups": [],
        "markdown": False,
        "max_reply_segments": 3,
        "reply_segment_max_len": 200,
    }


def prepare_config(config_path: Path, mock_a, mock_b, api_port: int) -> None:
    """写入两个机器人的配置：``qq:`` = 第 1 个，``bots:`` = 第 2 个。"""
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    app = data.setdefault("app", {})
    app["start_bot_on_launch"] = False
    app["onboarding_done"] = True
    app.pop("start_napcat_on_launch", None)

    data.setdefault("api", {}).update({"host": "127.0.0.1", "port": int(api_port)})
    data.setdefault("llm", {}).update(
        {
            "base_url": "%s/v1" % mock_a.base_url,
            "api_key": "mock-key",
            "model": "mock-model",
            "max_tokens": 128,
            "timeout": 20,
            "max_retries": 1,
        }
    )

    # 第 1 个机器人（qq: 段）
    qq = data.setdefault("qq", {})
    qq.update(
        {
            "id": "bot1",
            "name": "主机器人",
            "enabled": True,
            "character_id": "",
            "character_name": "",
            "reply_enabled": True,
            "group_reply_enabled": False,
            "official": official_values(mock_a, TARGET_A),
        }
    )
    for key in LEGACY_QQ_KEYS:
        qq.pop(key, None)

    # 第 2 个机器人（bots: 段）——独立 AppID，模拟真实平台里两个不同的机器人应用
    second = {
        "id": "bot2",
        "name": "副机器人",
        "enabled": True,
        "character_id": "",
        "character_name": "",
        "reply_enabled": True,
        "group_reply_enabled": False,
        "official": official_values(mock_b, TARGET_B, app_id=APP_ID_B),
    }
    data["bots"] = [second]

    proactive = data.setdefault("proactive", {})
    proactive.update(
        {
            "enabled": True,
            "scheduled_enabled": False,
            "idle_enabled": False,
            "random_enabled": False,
            "min_interval_minutes": 0,
            "probability": 1.0,
            "global_daily_limit": 20,
            "per_character_daily_limit": 10,
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


APP_ID_B = "mock-app-id-2"  # 第 2 个机器人的 AppID（两个机器人必须是不同的应用）


def assistant_messages(client: httpx.Client, character_id: str) -> List[Dict[str, Any]]:
    response = client.get("/api/conversations/%s/messages" % character_id, timeout=20.0)
    data = response.json() if response.status_code == 200 else {}
    return [item for item in (data.get("messages") or []) if item.get("role") == "assistant"]


def bot_by_id(client: httpx.Client, bot_id: str) -> Dict[str, Any]:
    for item in (client.get("/api/bots", timeout=20.0).json().get("bots") or []):
        if str(item.get("id") or "") == bot_id:
            return item
    return {}


def main() -> int:
    checker = smoke_test.Checker()
    print("多机器人 / 多角色自检开始（Python %s）" % sys.version.split()[0])

    mock_a_port = free_port()
    mock_b_port = free_port()
    api_port = free_port()
    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port
    smoke_test.MOCK_URL = "http://127.0.0.1:%d" % mock_a_port

    mock_a = mock_servers.MockProcess(port=mock_a_port).start()
    mock_a.reset(reply_text=REPLY_TEXT, proactive_text=PROACTIVE_TEXT)
    mock_b = mock_servers.MockProcess(port=mock_b_port, app_id=APP_ID_B).start()
    mock_b.reset(reply_text=REPLY_TEXT, proactive_text=PROACTIVE_TEXT)

    data_dir = Path(tempfile.mkdtemp(prefix="tavern-multibot-"))
    config_path = smoke_test.write_bot_config(data_dir)
    prepare_config(config_path, mock_a, mock_b, api_port)

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
    output = smoke_test.drain(bot)

    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=60.0)
    try:
        ready = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=150)
        checker.check("Bot 进程已启动", ready)
        if not ready:
            print("\n".join(output[-40:]))
            return 1

        # ------------------------------------------------------ 配置里的两个机器人
        saved = load_config(config_path)
        checker.check(
            "配置里第 1 个机器人写在 qq: 段（兼容旧配置布局）",
            str((saved.get("qq") or {}).get("name")) == "主机器人"
            and str(((saved.get("qq") or {}).get("official") or {}).get("target_openid")) == TARGET_A,
            json.dumps(saved.get("qq") or {}, ensure_ascii=False)[:200],
        )
        checker.check(
            "配置里第 2 个机器人写在 bots: 段",
            len(saved.get("bots") or []) == 1
            and str((saved.get("bots") or [{}])[0].get("name")) == "副机器人"
            and str(((saved.get("bots") or [{}])[0].get("official") or {}).get("target_openid")) == TARGET_B,
            json.dumps(saved.get("bots") or [], ensure_ascii=False)[:200],
        )
        checker.check(
            "配置里没有历史遗留键（NapCat / OneBot 字段）",
            all(key not in (saved.get("qq") or {}) for key in LEGACY_QQ_KEYS)
            and all(
                key not in ((saved.get("bots") or [{}])[0] if saved.get("bots") else {})
                for key in LEGACY_QQ_KEYS
            )
            and "napcat" not in saved
            and "onebot" not in saved,
            json.dumps(sorted(saved.keys()), ensure_ascii=False),
        )

        listing = client.get("/api/bots", timeout=20.0).json()
        bots = listing.get("bots") or []
        checker.check("机器人列表返回 2 个机器人", listing.get("count") == 2, str(listing.get("count")))
        checker.check(
            "两个机器人都是「QQ 官方机器人」（没有第三方协议模式）",
            [item.get("mode") for item in bots] == ["official", "official"],
            json.dumps([item.get("mode") for item in bots], ensure_ascii=False),
        )
        connected = wait_for(
            lambda: all(item.get("connected") for item in (client.get("/api/bots", timeout=20.0).json().get("bots") or [])),
            timeout=90,
        )
        checker.check(
            "两个机器人各自连上了自己的官方网关（mock 开放平台）",
            connected,
            json.dumps(
                [(item.get("name"), item.get("connected"), item.get("error")) for item in bots],
                ensure_ascii=False,
            )[:240],
        )
        checker.check(
            "两个机器人的目标 openid 完全独立",
            [str(item.get("target_openid") or "") for item in bots] == [TARGET_A, TARGET_B],
            json.dumps([item.get("target_openid") for item in bots], ensure_ascii=False),
        )

        status = client.get("/api/status", timeout=20.0).json()
        checker.check(
            "状态快照里带有 bots 列表（界面据此显示各机器人）",
            len(status.get("bots") or []) == 2 and not isinstance(status.get("napcat"), dict),
            json.dumps(sorted(status.keys()), ensure_ascii=False),
        )
        checker.check(
            "状态快照里不再有 napcat / target_user_id（旧通道已移除）",
            "napcat" not in status and "target_user_id" not in status,
            str(sorted(status.keys())),
        )

        # ------------------------------------------------------ 角色与绑定
        char_a = client.post(
            "/api/characters",
            json={"name": "角色A", "description": "{{char}} 是 A", "first_mes": "A 来了"},
            timeout=30.0,
        ).json()
        char_b = client.post(
            "/api/characters",
            json={"name": "角色B", "description": "{{char}} 是 B", "first_mes": "B 来了"},
            timeout=30.0,
        ).json()
        id_a = str((char_a.get("character") or {}).get("id") or "")
        id_b = str((char_b.get("character") or {}).get("id") or "")
        checker.check("创建了两个角色用于绑定", bool(id_a and id_b), "%s / %s" % (id_a, id_b))

        bound = client.put(
            "/api/bots/bot1",
            json={"character_id": id_a, "character_name": "角色A"},
            timeout=30.0,
        )
        checker.check(
            "第 1 个机器人可以绑定角色 A",
            bound.status_code == 200
            and (bound.json().get("bot") or {}).get("character_id") == id_a,
            bound.text[:200],
        )
        updated = client.put(
            "/api/bots/bot2",
            json={"character_id": id_b, "character_name": "角色B"},
            timeout=30.0,
        )
        checker.check(
            "第 2 个机器人可以绑定角色 B",
            updated.status_code == 200
            and (updated.json().get("bot") or {}).get("character_id") == id_b,
            updated.text[:200],
        )
        checker.check(
            "更新第 2 个机器人仍然写进 bots: 段（且不动 qq: 段）",
            str((load_config(config_path).get("qq") or {}).get("character_id")) == id_a
            and str(((load_config(config_path).get("bots") or [{}])[0]).get("character_id")) == id_b,
            json.dumps(load_config(config_path).get("bots") or [], ensure_ascii=False)[:200],
        )
        checker.check(
            "机器人列表显示各自的绑定角色",
            [(item.get("name"), item.get("character_name")) for item in (client.get("/api/bots", timeout=20.0).json().get("bots") or [])]
            == [("主机器人", "角色A"), ("副机器人", "角色B")],
            json.dumps(
                [(item.get("name"), item.get("character_name")) for item in (client.get("/api/bots", timeout=20.0).json().get("bots") or [])],
                ensure_ascii=False,
            ),
        )

        # ------------------------------------------------- 机器人 1 的单聊 → 角色 A
        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        mock_a.emit_c2c("你好呀")
        replied_a = wait_for(lambda: len(mock_a.official_sent()) > before_a, timeout=60)
        checker.check(
            "第 1 个机器人收到自己的单聊事件并回复",
            replied_a,
            json.dumps(mock_a.official_sent()[-1:], ensure_ascii=False)[:200],
        )
        checker.check(
            "回复由绑定角色 A 生成（写入 A 的对话）",
            wait_for(lambda: len(assistant_messages(client, id_a)) >= 1, timeout=30),
            str(assistant_messages(client, id_a))[:200],
        )
        checker.check(
            "第 2 个机器人没有收到第 1 个机器人的事件（不串台）",
            len(mock_b.official_sent()) == before_b and assistant_messages(client, id_b) == [],
            "B 侧消息=%d，B 的对话=%s" % (len(mock_b.official_sent()), assistant_messages(client, id_b)),
        )

        # ------------------------------------------------- 机器人 2 的单聊 → 角色 B
        before_a_sent = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        a_messages = len(assistant_messages(client, id_a))
        mock_b.emit_c2c("在吗")
        checker.check(
            "第 2 个机器人收到自己的单聊事件并回复",
            wait_for(lambda: len(mock_b.official_sent()) > before_b, timeout=60),
            json.dumps(mock_b.official_sent()[-1:], ensure_ascii=False)[:200],
        )
        checker.check(
            "回复由绑定角色 B 生成（写入 B 的对话）",
            wait_for(lambda: len(assistant_messages(client, id_b)) >= 1, timeout=30),
            str(assistant_messages(client, id_b))[:200],
        )
        checker.check(
            "第 1 个机器人没有被顺带触发（消息数与对话条数都不变）",
            len(mock_a.official_sent()) == before_a_sent
            and len(assistant_messages(client, id_a)) == a_messages,
            "A 侧消息=%d/%d，A 的对话=%d/%d"
            % (
                len(mock_a.official_sent()),
                before_a_sent,
                len(assistant_messages(client, id_a)),
                a_messages,
            ),
        )

        # --------------------------------------------- #角色名 可以临时换人
        a_messages = len(assistant_messages(client, id_a))
        mock_b.emit_c2c("#角色A 陪我聊两句")
        checker.check(
            "「#角色名」可以临时覆盖机器人绑定的角色",
            wait_for(lambda: len(assistant_messages(client, id_a)) > a_messages, timeout=45),
            "A=%d/%d B=%d"
            % (len(assistant_messages(client, id_a)), a_messages, len(assistant_messages(client, id_b))),
        )

        # ----------------------------------------------- 多机器人群：@ 谁谁回答
        client.put("/api/bots/bot1", json={"group_reply_enabled": True}, timeout=30.0)
        client.put("/api/bots/bot2", json={"group_reply_enabled": True}, timeout=30.0)
        time.sleep(1.5)

        # 真实平台（「接收所有消息」全量模式）：群里一条消息会推给群里**每个**
        # 机器人，@ 机器人时 content 带 <@AppID> 标记。模拟方式：同一条群消息
        # 同时推到两个 mock 平台（各自代表一个机器人应用）。
        def _emit_to_both(content: str, mention_appid: str = "", message_id: str = "") -> None:
            for mock in (mock_a, mock_b):
                mock.emit_group(
                    content,
                    event_type="GROUP_MESSAGE_CREATE",
                    mention_appid=mention_appid,
                    **({"id": message_id} if message_id else {}),
                )

        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        a_messages = len(assistant_messages(client, id_a))
        b_messages = len(assistant_messages(client, id_b))
        _emit_to_both("群里 @ 了主机器人", mention_appid=mock_servers.OFFICIAL_APP_ID)
        checker.check(
            "@ 主机器人时只有主机器人回复（副机器人看到同一条消息但不抢话）",
            wait_for(
                lambda: any(
                    item.get("kind") == "group" for item in mock_a.official_sent()[before_a:]
                ),
                timeout=60,
            )
            and len(mock_b.official_sent()) == before_b,
            "A 侧新增=%d B 侧新增=%d"
            % (len(mock_a.official_sent()) - before_a, len(mock_b.official_sent()) - before_b),
        )
        checker.check(
            "群回复由被 @ 机器人绑定的角色生成（A 的对话增加，B 的不变）",
            wait_for(lambda: len(assistant_messages(client, id_a)) > a_messages, timeout=30)
            and len(assistant_messages(client, id_b)) == b_messages,
            "A=%d/%d B=%d/%d"
            % (len(assistant_messages(client, id_a)), a_messages, b_messages, len(assistant_messages(client, id_b))),
        )

        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        b_messages = len(assistant_messages(client, id_b))
        _emit_to_both("群里 @ 了副机器人", mention_appid=APP_ID_B)
        checker.check(
            "@ 副机器人时只有副机器人回复（对称验证）",
            wait_for(
                lambda: any(
                    item.get("kind") == "group" for item in mock_b.official_sent()[before_b:]
                ),
                timeout=60,
            )
            and len(mock_a.official_sent()) == before_a,
            "A 侧新增=%d B 侧新增=%d"
            % (len(mock_a.official_sent()) - before_a, len(mock_b.official_sent()) - before_b),
        )
        checker.check(
            "副机器人的群回复用了它绑定的角色 B",
            wait_for(lambda: len(assistant_messages(client, id_b)) > b_messages, timeout=30),
            "B=%d/%d" % (len(assistant_messages(client, id_b)), b_messages),
        )

        # V0.2.2：平台 2026-09 起的新格式 —— content 不带 @ 占位，mentions 里
        # is_you 标记「@ 的是你」（同一条消息推到两个机器人的连接，is_you 不同）
        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        mock_a.emit_group(
            "新格式：@ 了主机器人",
            event_type="GROUP_MESSAGE_CREATE",
            mention_appid=mock_servers.OFFICIAL_APP_ID,
            mention_marker=False,
            mention_is_you=True,
            id="mock-new-format-1",
        )
        mock_b.emit_group(
            "新格式：@ 了主机器人",
            event_type="GROUP_MESSAGE_CREATE",
            mention_appid=mock_servers.OFFICIAL_APP_ID,
            mention_marker=False,
            mention_is_you=False,
            mention_fields={"id": "unmatched-internal-id", "user_openid": "unmatched-internal-id", "union_openid": "unmatched-union-id"},
            id="mock-new-format-1",
        )
        checker.check(
            "新格式（is_you 判定）@ 主机器人时也只有主机器人回复",
            wait_for(
                lambda: any(item.get("kind") == "group" for item in mock_a.official_sent()[before_a:]),
                timeout=60,
            )
            and len(mock_b.official_sent()) == before_b,
            "A 侧新增=%d B 侧新增=%d"
            % (len(mock_a.official_sent()) - before_a, len(mock_b.official_sent()) - before_b),
        )

        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        _emit_to_both("群里普通消息，没有 @ 任何人")
        checker.check(
            "没有人 @ 的普通群消息：开了「响应普通群消息」的两个机器人都回复",
            wait_for(
                lambda: any(item.get("kind") == "group" for item in mock_a.official_sent()[before_a:])
                and any(item.get("kind") == "group" for item in mock_b.official_sent()[before_b:]),
                timeout=60,
            ),
            "A 侧新增=%d B 侧新增=%d"
            % (len(mock_a.official_sent()) - before_a, len(mock_b.official_sent()) - before_b),
        )

        client.put("/api/bots/bot1", json={"group_reply_enabled": False}, timeout=30.0)
        client.put("/api/bots/bot2", json={"group_reply_enabled": False}, timeout=30.0)
        time.sleep(1.5)

        # ----------------------------------------------- 主动消息按机器人分别发送
        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        only_b = client.post("/api/proactive/trigger", json={"bot_id": "bot2", "force": True}, timeout=120.0)
        payload = only_b.json() if only_b.status_code == 200 else {}
        checker.check(
            "指定机器人（bot2）的主动消息只从它自己发出",
            only_b.status_code == 200
            and len(mock_b.official_sent()) > before_b
            and len(mock_a.official_sent()) == before_a,
            "A=%d/%d B=%d/%d %s"
            % (
                len(mock_a.official_sent()),
                before_a,
                len(mock_b.official_sent()),
                before_b,
                json.dumps(payload, ensure_ascii=False)[:160],
            ),
        )
        checker.check(
            "bot2 的主动消息用了它绑定的角色 B",
            str(payload.get("character")) == "角色B" and str(payload.get("bot_id")) == "bot2",
            json.dumps(payload, ensure_ascii=False)[:200],
        )
        checker.check(
            "bot2 的主动消息发给了它自己的 openid",
            str((mock_b.official_sent()[-1] if mock_b.official_sent() else {}).get("openid") or "") == TARGET_B,
            json.dumps(mock_b.official_sent()[-1:], ensure_ascii=False)[:200],
        )
        checker.check(
            "bot2 的主动消息内容来自 LLM 的主动分支",
            PROACTIVE_TEXT in str((mock_b.official_sent()[-1] if mock_b.official_sent() else {}).get("content") or ""),
            str((mock_b.official_sent()[-1] if mock_b.official_sent() else {}).get("content")),
        )

        only_a = client.post("/api/proactive/trigger", json={"bot_id": "bot1", "force": True}, timeout=120.0)
        payload_a = only_a.json() if only_a.status_code == 200 else {}
        checker.check(
            "bot1 的主动消息走自己的官方通道、发给自己的 openid、用角色 A",
            len(mock_a.official_sent()) > before_a
            and str((mock_a.official_sent()[-1] if mock_a.official_sent() else {}).get("openid") or "") == TARGET_A
            and str(payload_a.get("character")) == "角色A",
            json.dumps(payload_a, ensure_ascii=False)[:200],
        )

        # --------------------------------------------------- 全部启用的机器人
        before_a = len(mock_a.official_sent())
        before_b = len(mock_b.official_sent())
        both = client.post("/api/proactive/trigger", json={"bot_id": "all", "force": True}, timeout=180.0)
        payload = both.json() if both.status_code == 200 else {}
        outcomes = payload.get("bots") or []
        checker.check(
            "bot_id=all 让两个机器人各发一条",
            len(mock_a.official_sent()) > before_a
            and len(mock_b.official_sent()) > before_b
            and len([item for item in outcomes if item.get("ok")]) == 2,
            json.dumps(outcomes, ensure_ascii=False)[:240],
        )

        # ------------------------------------------- 停用的机器人跳过并给出原因
        client.put("/api/bots/bot2", json={"enabled": False}, timeout=30.0)
        checker.check(
            "停用后 bot2 的网关断开",
            wait_for(lambda: not bot_by_id(client, "bot2").get("connected"), timeout=60),
            json.dumps(bot_by_id(client, "bot2"), ensure_ascii=False)[:200],
        )
        before_b = len(mock_b.official_sent())
        mock_b.emit_c2c("还活着吗")
        time.sleep(3.0)
        checker.check(
            "停用的机器人不再回复消息",
            len(mock_b.official_sent()) == before_b,
            json.dumps(mock_b.official_sent()[-1:], ensure_ascii=False)[:160],
        )
        skipped = client.post("/api/proactive/trigger", json={"bot_id": "bot2", "force": True}, timeout=60.0)
        payload = skipped.json() if skipped.status_code == 200 else {}
        checker.check(
            "指定停用的机器人时给出可读的跳过原因",
            bool(payload.get("skipped")) and "停用" in str(payload.get("reason") or ""),
            json.dumps(payload, ensure_ascii=False)[:240],
        )
        checker.check(
            "停用不会影响另一个机器人（bot1 仍能发主动消息）",
            client.post("/api/proactive/trigger", json={"bot_id": "bot1", "force": True}, timeout=120.0).status_code == 200,
        )
        client.put("/api/bots/bot2", json={"enabled": True}, timeout=30.0)
        checker.check(
            "重新启用后 bot2 的网关会连回来",
            wait_for(lambda: bool(bot_by_id(client, "bot2").get("connected")), timeout=90),
            json.dumps(bot_by_id(client, "bot2"), ensure_ascii=False)[:200],
        )

        # -------------------------------------------------------- 删除规则
        refused = client.delete("/api/bots/bot1", timeout=20.0)
        checker.check(
            "第 1 个机器人不允许删除（可停用不可删）",
            refused.status_code == 400 and "不能删除" in refused.text,
            refused.text[:160],
        )
        removed = client.delete("/api/bots/bot2", timeout=30.0)
        checker.check("第 2 个机器人可以删除", removed.status_code == 200, removed.text[:160])
        after = client.get("/api/bots", timeout=20.0).json()
        checker.check("删除后只剩一个机器人", after.get("count") == 1, str(after.get("count")))
        saved = load_config(config_path)
        checker.check("config.yaml 里的 bots 段被同步清空", not (saved.get("bots") or []), str(saved.get("bots")))
        checker.check(
            "删除机器人后第 1 个机器人仍然可用（不受影响）",
            bool((client.get("/api/bots", timeout=20.0).json().get("bots") or [{}])[0].get("connected")),
            json.dumps((client.get("/api/bots", timeout=20.0).json().get("bots") or [{}])[0], ensure_ascii=False)[:200],
        )

        return checker.summary()
    except Exception as exc:  # pragma: no cover - 自检自身异常
        import traceback

        traceback.print_exc()
        checker.check("多机器人自检未抛出异常", False, str(exc))
        return 1
    finally:
        client.close()
        for item in (mock_a, mock_b):
            try:
                item.stop()
            except Exception:
                pass
        try:
            if bot.poll() is None:
                bot.terminate()
                bot.wait(timeout=8)
        except Exception:
            try:
                bot.kill()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
