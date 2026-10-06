"""QQ 官方机器人（开放平台）通道自检。

用 mock 复刻官方平台的四件套——凭证接口、REST 发消息接口、WebSocket 网关、
事件推送——然后启动**真实的 Bot 进程**，逐条验证官方通道：

1. 凭证获取：AppID/AppSecret → access_token（错误凭据要能识别 code 并给出可读提示）
2. 网关连接：Hello → Identify → READY（带 intents，上报会话与序号）
3. 单聊消息：C2C_MESSAGE_CREATE → 生成回复 → ``POST /v2/users/{openid}/messages``，
   且必须带 msg_id 与递增的 msg_seq
4. 群聊 @：GROUP_AT_MESSAGE_CREATE → ``POST /v2/groups/{group_openid}/messages``
5. openid 自动记忆（官方平台只能按 openid 发主动消息）
6. 主动消息：不带 msg_id 直接推送（官方「主动消息」形态）
7. 错误码处理：错误的 AppID / AppSecret → 可读的中文提示（含错误码）
8. 沙盒开关：``sandbox: true`` 会把默认域名切到 sandbox 域名
9. 网关掉线后自动重连（掐断 mock 网关 → 机器人自己连回来）
10. 重启 Bot 进程后，自动记住的 openid 仍能发主动消息

运行::

    python -m tests.official_smoke
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

from tests import card_factory, mock_servers, smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.4) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def write_official_config(data_dir: Path, api_port: int, mock_url: str, **official_overrides: Any) -> Path:
    """官方通道配置：只需要 AppID/AppSecret，指向 mock 官方平台与 mock LLM。"""
    import yaml

    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port
    smoke_test.MOCK_URL = mock_url
    smoke_test.write_bot_config(data_dir)
    path = data_dir / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["qq"]["official"].update(official_overrides)
    config["qq"]["group_reply_enabled"] = True
    config.setdefault("app", {})["start_bot_on_launch"] = False
    # V0.2：基线 e2e 保持纯文字回复（语音概率 0），富媒体段落里再单独打开
    config.setdefault("media", {})["voice_reply_probability"] = 0.0
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def update_official_config(data_dir: Path, **official_overrides: Any) -> None:
    """直接改 config.yaml（模拟用户手改配置文件，随后由 Bot 热重载或手动重载生效）。"""
    import yaml

    path = data_dir / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["qq"]["official"].update(official_overrides)
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def start_bot(data_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "QQAI_DATA_DIR": str(data_dir),
            "QQAI_HOME": str(ROOT),
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )
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
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def c2c_records(mock: Any) -> List[Dict[str, Any]]:
    return [item for item in mock.official_sent() if item.get("kind") == "c2c"]


def main() -> int:
    checker = smoke_test.Checker()
    print("QQ 官方机器人通道自检开始（Python %s）" % sys.version.split()[0])

    mock_port = free_port()
    api_port = free_port()
    mock = mock_servers.MockProcess(port=mock_port).start()
    mock.reset(reply_text="官方通道回复：你好呀。", proactive_text="官方通道主动消息。")

    data_dir = Path(tempfile.mkdtemp(prefix="tavern-official-"))
    write_official_config(data_dir, api_port, mock.base_url)
    cards_dir = data_dir / "characters"
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_factory.write_json_card(cards_dir / "官方角色.json", "官方角色")

    bot = None
    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=30.0)
    try:
        checker.phase("官方通道：网关 / 单聊 / 群聊 / 主动消息")
        bot = start_bot(data_dir)
        output = smoke_test.drain(bot)

        healthy = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=120)
        checker.check("Bot 进程已启动", healthy)
        if not healthy:
            print("\n".join(output[-40:]))
            return 1

        scan = client.post("/api/characters/scan", timeout=60).json()
        checker.check(
            "导入角色卡（官方通道同样使用角色池）",
            len(scan.get("imported") or []) == 1,
            json.dumps(scan, ensure_ascii=False)[:160],
        )

        status = client.get("/api/status").json()
        checker.check("默认连接方式为官方机器人", status.get("qq_mode") == "official", str(status.get("qq_mode")))
        checker.check(
            "状态里带有可读的模式名",
            "官方" in str(status.get("qq_mode_label")),
            str(status.get("qq_mode_label")),
        )
        checker.check("状态里已没有 napcat 段", "napcat" not in status, str(sorted(status.keys())))

        connected = wait_for(
            lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")), timeout=90
        )
        checker.check("官方机器人网关已连接（WebSocket READY）", connected)
        qq = client.get("/api/qq/status").json()
        checker.check(
            "读到机器人身份信息",
            bool(qq.get("nickname") or qq.get("user_id")),
            json.dumps({k: qq.get(k) for k in ("nickname", "user_id", "app_id")}, ensure_ascii=False),
        )
        checker.check(
            "上报了网关会话状态",
            bool((qq.get("gateway") or {}).get("session_id")),
            str(qq.get("gateway")),
        )
        checker.check(
            "鉴权 intents 正确传递",
            int(mock.state().get("official_intents") or 0) == smoke_test.OFFICIAL_INTENTS,
            str(mock.state().get("official_intents")),
        )

        # ------------------------------------------------------------ 连接测试接口
        test = client.post("/api/qq/test", timeout=30).json()
        checker.check("「测试连接」返回可用", bool(test.get("available")), json.dumps(test, ensure_ascii=False)[:200])
        checker.check("测试结果包含模式信息", test.get("mode") == "official", str(test.get("mode")))
        checker.check(
            "测试结果带上网关地址所需的域名信息",
            str(test.get("api_domain") or "").startswith("http"),
            str(test.get("api_domain")),
        )

        # ------------------------------------------------------------ 单聊回复
        mock.reset(reply_text="官方通道回复：你好呀。", proactive_text="官方通道主动消息。")
        emitted = mock.emit_c2c("你好，官方机器人")
        checker.check("mock 网关已把单聊事件推给机器人", bool(emitted.get("delivered")), str(emitted))

        replied = wait_for(
            lambda: any(
                item["kind"] == "c2c" and "官方通道回复" in str(item.get("content"))
                for item in mock.official_sent()
            ),
            timeout=45,
        )
        checker.check("单聊消息能自动回复（走官方 REST 接口）", replied, str(mock.official_sent())[:200])
        if replied:
            record = c2c_records(mock)[0]
            checker.check(
                "回复发给了正确的 openid",
                record.get("openid") == mock_servers.DEFAULT_OPENID,
                str(record.get("openid")),
            )
            checker.check(
                "被动回复带 msg_id 与 msg_seq（官方要求）",
                record.get("msg_id") == emitted.get("id") and int(record.get("msg_seq") or 0) >= 1,
                str({k: record.get(k) for k in ("msg_id", "msg_seq")}),
            )
            checker.check(
                "消息类型为文本（msg_type=0）",
                int(record.get("msg_type", -1)) == 0,
                str(record.get("msg_type")),
            )

        # 会话记录
        conversations = client.get("/api/conversations").json()
        active = [item for item in conversations if int(item.get("message_count") or 0) > 0]
        checker.check("官方通道的对话已入库", bool(active), str(conversations)[:160])

        # ------------------------------------------------------------ 群聊 @
        before = len(mock.official_sent())
        mock.emit_group("群里在聊什么？")
        checker.check(
            "群聊 @ 消息能回复到群里",
            wait_for(
                lambda: any(
                    item["kind"] == "group"
                    and item.get("group_openid") == mock_servers.DEFAULT_GROUP_OPENID
                    for item in mock.official_sent()[before:]
                ),
                timeout=45,
            ),
            str(mock.official_sent()[before:])[:200],
        )

        # ------------------------------------------------------------ openid 记忆
        qq = client.get("/api/qq/status").json()
        checker.check(
            "自动记住了最近的私聊 openid（官方平台主动消息只能按 openid 发）",
            qq.get("last_user_openid") == mock_servers.DEFAULT_OPENID,
            str(qq.get("last_user_openid")),
        )

        # ------------------------------------------------------------ 白名单 / markdown
        checker.phase("官方通道：白名单 / markdown / 群白名单")

        # markdown 开关：开启后 msg_type 应为 2（官方 markdown 消息）
        before = len(mock.official_sent())
        update_official_config(data_dir, markdown=True)
        client.post("/api/config/reload", timeout=20)
        mock.emit_c2c("用 markdown 回我一句")
        checker.check(
            "开启 markdown 后按 msg_type=2 发送",
            wait_for(
                lambda: any(int(item.get("msg_type", -1)) == 2 for item in mock.official_sent()[before:]),
                timeout=45,
            ),
            str(mock.official_sent()[before:])[:200],
        )
        update_official_config(data_dir, markdown=False)
        client.post("/api/config/reload", timeout=20)

        # 私聊白名单：allow_all_users=false 时只回白名单里的 openid
        update_official_config(data_dir, allow_all_users=False, allowed_users=["someone-else"])
        client.post("/api/config/reload", timeout=20)
        before = len(mock.official_sent())
        mock.emit_c2c("我在白名单外面")
        time.sleep(8)
        checker.check(
            "不在私聊白名单里的用户不会被回复",
            len(mock.official_sent()) == before,
            str(mock.official_sent()[before:])[:200],
        )
        update_official_config(data_dir, allowed_users=[mock_servers.DEFAULT_OPENID])
        client.post("/api/config/reload", timeout=20)
        mock.emit_c2c("现在我在白名单里")
        checker.check(
            "把用户加进白名单后恢复回复（说明上一条是被白名单拦下的，不是链路卡住）",
            wait_for(lambda: len(mock.official_sent()) > before, timeout=45),
            str(mock.official_sent()[before:])[:200],
        )
        update_official_config(data_dir, allow_all_users=True, allowed_users=[])
        client.post("/api/config/reload", timeout=20)

        # 群白名单：allowed_groups 非空时只回名单里的群
        update_official_config(data_dir, allowed_groups=["other-group-openid"])
        client.post("/api/config/reload", timeout=20)
        before = len(mock.official_sent())
        mock.emit_group("这个群不在白名单里")
        time.sleep(8)
        checker.check(
            "不在群白名单里的群不会被回复",
            len(mock.official_sent()) == before,
            str(mock.official_sent()[before:])[:200],
        )
        update_official_config(data_dir, allowed_groups=[mock_servers.DEFAULT_GROUP_OPENID])
        client.post("/api/config/reload", timeout=20)
        mock.emit_group("这个群在白名单里")
        checker.check(
            "群加进白名单后恢复群回复",
            wait_for(lambda: len(mock.official_sent()) > before, timeout=45),
            str(mock.official_sent()[before:])[:200],
        )
        update_official_config(data_dir, allowed_groups=[])
        client.post("/api/config/reload", timeout=20)
        before = len(mock.official_sent())
        mock.emit_group("白名单清空后应该不限群")
        checker.check(
            "群白名单清空后表示不限（仍能回复）",
            wait_for(lambda: len(mock.official_sent()) > before, timeout=45),
            str(mock.official_sent()[before:])[:200],
        )

        # ------------------------------------------------------------ 主动消息
        before = len(mock.official_sent())
        result = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
        checker.check("官方通道下能发送主动消息", bool(result.get("ok")), json.dumps(result, ensure_ascii=False)[:200])
        sent = mock.official_sent()[before:]
        if sent:
            checker.check(
                "主动消息不带 msg_id（官方「主动消息」形态）",
                not sent[0].get("msg_id"),
                str({k: sent[0].get(k) for k in ("msg_id", "msg_seq", "content")}),
            )
            checker.check(
                "主动消息内容来自 LLM 的主动分支",
                "主动消息" in str(sent[0].get("content")),
                str(sent[0].get("content")),
            )
        else:
            checker.check("主动消息真的发出去了", False, "没有记录到官方接口调用")

        # ------------------------------------------------------------ 掉线重连
        checker.phase("官方通道：掉线重连 / 错误码 / 沙盒 / 重启记忆")
        checker.check("网关当前在线", bool((client.get("/api/qq/status").json() or {}).get("connected")))
        dropped = mock.drop_gateway()
        checker.check("已掐断 mock 网关连接", int(dropped.get("closed") or 0) >= 1, str(dropped))
        checker.check(
            "网关断开后能自动重连（状态回到已连接）",
            wait_for(
                lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")),
                timeout=60,
                interval=1.0,
            ),
        )
        reconnected = client.get("/api/qq/status").json()
        checker.check(
            "重连计数被记录（说明真的重建过连接）",
            int((reconnected.get("gateway") or {}).get("reconnect_count", 0)) >= 1,
            str(reconnected.get("gateway")),
        )
        mock.reset(reply_text="重连之后的回复。", proactive_text="官方通道主动消息。")
        re_emitted = mock.emit_c2c("重连之后还在吗")
        checker.check("重连后仍能收到事件", bool(re_emitted.get("delivered")), str(re_emitted))
        checker.check(
            "重连后仍能正常回复",
            wait_for(
                lambda: any("重连之后" in str(item.get("content")) for item in c2c_records(mock)),
                timeout=45,
            ),
            str(mock.official_sent())[:200],
        )

        # ------------------------------------------------------------ 错误凭据
        # 直接改 config.yaml（模拟用户手改配置文件），然后**什么都不问**，等调度器里
        # 的「配置热重载检查」任务（每 30 秒一次）自己把改动应用上去。
        #
        # 注意：这里只能探测 /api/health（它不读配置）。/api/status、/api/qq/status
        # 这类接口会顺手 reload_if_changed()，把「文件已变化」这个信号先消费掉，
        # 那样热重载任务就再也看不到这次改动了。
        update_official_config(data_dir, app_secret="wrong-secret")
        print("  等待配置热重载任务应用手改的凭据（最多 45 秒，期间只探测 /api/health）…")
        deadline = time.time() + 45
        while time.time() < deadline:
            client.get("/api/health", timeout=5)
            time.sleep(3)
        bad_secret = client.post("/api/qq/test", timeout=30).json()
        checker.check(
            "手改 config.yaml 后能被自动热重载（每 30 秒检查一次），错误的 AppSecret 被识别为失败",
            not bad_secret.get("available"),
            json.dumps(bad_secret, ensure_ascii=False)[:200],
        )
        checker.check(
            "失败原因里带官方错误码 100016 与可读说明",
            "100016" in str(bad_secret.get("error")) and "AppSecret" in str(bad_secret.get("error")),
            str(bad_secret.get("error")),
        )

        # 错误的 AppID：走「重新加载配置」按钮那条路径（load + apply_config，立即生效）
        update_official_config(data_dir, app_id="wrong-app-id")
        checker.check("重新加载配置接口可用", bool(client.post("/api/config/reload", timeout=20).json().get("ok")))
        bad_appid = client.post("/api/qq/test", timeout=30).json()
        checker.check(
            "错误的 AppID 也会被识别为失败并给出 100007 提示",
            not bad_appid.get("available") and "100007" in str(bad_appid.get("error")),
            str(bad_appid.get("error")),
        )

        update_official_config(
            data_dir,
            app_id=mock_servers.OFFICIAL_APP_ID,
            app_secret=mock_servers.OFFICIAL_APP_SECRET,
        )
        client.post("/api/config/reload", timeout=20)
        checker.check(
            "修正凭据后「测试连接」恢复可用",
            wait_for(lambda: bool(client.post("/api/qq/test", timeout=30).json().get("available")), timeout=45),
            json.dumps(client.post("/api/qq/test", timeout=30).json(), ensure_ascii=False)[:200],
        )

        # ------------------------------------------------------------ 沙盒开关
        # 自定义域名不会被沙盒开关改写，但状态里要如实报告
        update_official_config(data_dir, sandbox=True)
        client.post("/api/config/reload", timeout=20)
        sandbox_info = client.post("/api/qq/test", timeout=30).json()
        checker.check("沙盒开关会被如实上报", bool(sandbox_info.get("sandbox")), str(sandbox_info.get("sandbox")))
        checker.check(
            "自定义域名不会被沙盒开关改写（仍指向 mock）",
            sandbox_info.get("api_domain") == mock.base_url,
            str(sandbox_info.get("api_domain")),
        )
        # 用真实的默认域名验证「沙盒域名切换」规则本身（纯规则，不发网络请求）
        from bot.qq_official.client import OfficialQQClient

        base_default = OfficialQQClient(
            "a", "b", api_domain="https://api.sgroup.qq.com", sandbox=False
        ).api_domain
        checked_sandbox = OfficialQQClient(
            "a", "b", api_domain="https://api.sgroup.qq.com", sandbox=True
        ).api_domain
        checker.check(
            "官方默认域名 + sandbox=true → 切到 sandbox 域名",
            base_default == "https://api.sgroup.qq.com"
            and checked_sandbox == "https://sandbox.api.sgroup.qq.com",
            "%s → %s" % (base_default, checked_sandbox),
        )
        update_official_config(data_dir, sandbox=False)
        client.post("/api/config/reload", timeout=20)
        restored = client.post("/api/qq/test", timeout=30).json()
        checker.check(
            "沙盒开关可以关回去（状态恢复为非沙盒）",
            not restored.get("sandbox"),
            json.dumps(restored, ensure_ascii=False)[:200],
        )

        # ------------------------------------------------------------ 重启 Bot 进程
        # 主动消息目标只靠「自动记住的 openid」，重启后必须还能发出去
        checker.check("优雅退出接口可用", client.post("/api/shutdown", timeout=15).status_code == 200)
        stopped = wait_for(lambda: bot.poll() is not None, timeout=25)
        checker.check("Bot 进程已退出（准备重启）", stopped)
        if not stopped:
            bot.terminate()
            wait_for(lambda: bot.poll() is not None, timeout=10)

        # 重启前记下 mock 里已有的消息，重启后不应该有任何新消息（除非主动消息）
        mock.reset(reply_text="重启之后的回复。", proactive_text="重启之后的主动消息。")
        smoke_test.clear_logs(data_dir)
        bot = start_bot(data_dir)
        output = smoke_test.drain(bot)
        healthy = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=120)
        checker.check("Bot 进程重启成功", healthy)
        if not healthy:
            print("\n".join(output[-40:]))
            return 1
        checker.check(
            "重启后网关重新连上",
            wait_for(lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")), timeout=90),
        )
        remembered = client.get("/api/qq/status").json()
        checker.check(
            "重启后仍记得上次的 openid（从数据库读回）",
            remembered.get("last_user_openid") == mock_servers.DEFAULT_OPENID,
            str(remembered.get("last_user_openid")),
        )
        total_before = len(mock.official_sent())
        restarted = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
        checker.check(
            "重启后无需再收到消息即可发主动消息",
            bool(restarted.get("ok")),
            json.dumps(restarted, ensure_ascii=False)[:200],
        )
        after_restart = mock.official_sent()[total_before:]
        checker.check(
            "主动消息发给了重启前记住的 openid",
            bool(after_restart) and after_restart[0].get("openid") == mock_servers.DEFAULT_OPENID,
            str(after_restart[:1]),
        )
        checker.check(
            "重启后的主动消息仍然是「无 msg_id」形态",
            bool(after_restart) and not after_restart[0].get("msg_id"),
            str(after_restart[:1]),
        )

        # ------------------------------------------------------------ V0.2 富媒体
        checker.phase("V0.2 富媒体：语音 / 图片双向（mock 线路）")
        # 四条线路全部指向 mock 服务（与 GUI「模型路由」保存走同一个接口；
        # 语音转文字不占线路——用 QQ 官方平台随消息推送的参考转写）
        media_patch = {
            "providers": {
                "vision": {"engine": "openai", "base_url": "%s/v1" % mock.base_url, "api_key": "mock-key", "model": "mock-vision"},
                "image": {"engine": "openai", "base_url": "%s/v1" % mock.base_url, "api_key": "mock-key", "model": "mock-image"},
                "tts": {"engine": "openai", "base_url": "%s/v1" % mock.base_url, "api_key": "mock-key", "model": "mock-tts"},
            },
            "media": {
                "enabled": True,
                "voice_reply_probability": 1.0,
                "allow_image": True,
                "voice_max_chars": 180,
                "temp_days": 3,
            },
        }
        patch_ok = client.put("/api/config", json=media_patch).status_code == 200
        checker.check("模型路由配置通过 API 写入（GUI 保存同款路径）", patch_ok)
        providers_state = client.get("/api/providers").json()
        checker.check(
            "状态接口能列出全部四条线路",
            all(slot in (providers_state.get("slots") or {}) for slot in ("chat", "vision", "image", "tts")),
            str(sorted((providers_state.get("slots") or {}).keys())),
        )

        # ------------------------------------------------- 语音回复（TTS）
        mock.reset(reply_text="我现在用语音跟你说。", proactive_text="mock 主动消息。")
        emitted = mock.emit_c2c("我们用语音聊天吧")
        voice_replied = wait_for(
            lambda: any(i.get("file_info") for i in mock.official_sent() if i.get("kind") == "c2c"),
            timeout=90,
        )
        checker.check("文字消息收到语音回复（TTS 合成 + 富媒体发送）", voice_replied, str(mock.official_sent())[-300:])
        if voice_replied:
            voice_rec = next(i for i in mock.official_sent() if i.get("file_info"))
            checker.check(
                "语音回复带 msg_id 与 msg_seq（被动回复序号）",
                voice_rec.get("msg_id") == emitted.get("id") and int(voice_rec.get("msg_seq") or 0) >= 1,
                str({k: voice_rec.get(k) for k in ("msg_id", "msg_seq")}),
            )
            checker.check("TTS 线路被真实调用", int(mock.state().get("tts_calls", 0)) >= 1, str(mock.state().get("tts_calls")))
            checker.check("语音以 file_type=3 上传", any(u.get("file_type") == 3 for u in mock.media_uploads()), str(mock.media_uploads())[-200:])
            checker.check(
                "语音回复没有重复发文字（整条改为语音）",
                not any(i.get("kind") == "c2c" and i.get("content") for i in mock.official_sent()),
                str([i.get("content") for i in mock.official_sent()]),
            )

        # ------------------------------------------------- 图像理解（vision）
        client.put("/api/config", json={"media": {"voice_reply_probability": 0.0}})
        mock.reset(reply_text="这条不该出现（vision 优先）", vision_text="我看你发的图了：一个蓝色的小方块。")
        emitted = mock.client.post(
            "/__control/emit_c2c",
            json={"attachments": [{"url": mock.attachment_url("test.png"), "filename": "test.png", "content_type": "image/png"}]},
        ).json()
        vision_replied = wait_for(
            lambda: any("我看你发的图了" in str(i.get("content")) for i in mock.official_sent()),
            timeout=90,
        )
        checker.check("纯图片消息经视觉线路理解后回复文字", vision_replied, str(mock.official_sent())[-300:])
        checker.check("视觉线路被真实调用（请求带 image_url）", int(mock.state().get("vision_calls", 0)) >= 1, str(mock.state().get("vision_calls")))
        checker.check(
            "图片挂在当前（最后一条）user 消息上发给视觉模型",
            mock.state().get("vision_image_in_last_user") is True,
            str({k: mock.state().get(k) for k in ("vision_calls", "vision_image_in_last_user")}),
        )
        checker.check("图片附件被下载", int(mock.state().get("media_files_served", 0)) >= 1, str(mock.state().get("media_files_served")))

        # ------------------------------------------------- [IMG] 生图 + 多段序号
        client.put("/api/config", json={"media": {"voice_reply_probability": 1.0}})
        mock.reset(reply_text="看，我给你画好了\n[IMG] 一只在月球上喝茶的猫")
        emitted = mock.emit_c2c("给我画张图吧")
        gen_replied = wait_for(
            lambda: sum(1 for i in mock.official_sent() if i.get("file_info")) >= 2,
            timeout=120,
        )
        checker.check("[IMG] 标记触发生图并随语音一起发出", gen_replied, str(mock.official_sent())[-400:])
        if gen_replied:
            recs = [i for i in mock.official_sent() if i.get("file_info")]
            checker.check("图片以 file_type=1 上传", any(u.get("file_type") == 1 for u in mock.media_uploads()), str(mock.media_uploads())[-200:])
            checker.check("生图线路被真实调用", int(mock.state().get("image_gen_calls", 0)) >= 1, str(mock.state().get("image_gen_calls")))
            checker.check(
                "多条富媒体按 msg_seq 递增发送",
                sorted(int(r.get("msg_seq") or 0) for r in recs) == [1, 2],
                str([(r.get("msg_seq")) for r in recs]),
            )
            checker.check("富媒体回复都带 msg_id", all(r.get("msg_id") == emitted.get("id") for r in recs), str(recs))

        # ------------------------------------------------- 语音理解（平台参考转写，零配置）
        client.put("/api/config", json={"media": {"voice_reply_probability": 0.0}})
        mock.reset(reply_text="我听见你刚才说的话了。")
        _voice_served_before = int(mock.state().get("media_files_served", 0))
        mock.client.post(
            "/__control/emit_c2c",
            json={
                "attachments": [
                    {
                        "url": mock.attachment_url("test.silk"),
                        "voice_wav_url": mock.attachment_url("wav_variant.bin"),
                        "filename": "voice.silk",
                        "content_type": "audio/silk",
                        "asr_refer_text": "我饿了",
                    }
                ]
            },
        )
        refer_replied = wait_for(
            lambda: any("我听见你刚才说的话了" in str(i.get("content")) for i in mock.official_sent()),
            timeout=90,
        )
        checker.check("语音消息经平台参考转写正常回复", refer_replied, str(mock.official_sent())[-300:])
        checker.check(
            "参考转写文本进了模型上下文（asr_refer_text → LLM 请求）",
            "我饿了" in str(mock.state().get("last_llm_user_text", "")),
            str(mock.state().get("last_llm_user_text", ""))[:200],
        )
        checker.check(
            "语音附件不被下载（url / voice_wav_url 均不请求）",
            int(mock.state().get("media_files_served", 0)) == _voice_served_before
            and int(mock.state().get("wav_variant_served", 0)) == 0,
            str({k: mock.state().get(k) for k in ("media_files_served", "wav_variant_served")}),
        )

        checker.check("再次优雅退出", client.post("/api/shutdown", timeout=15).status_code == 200)
        checker.check("Bot 进程正常退出", wait_for(lambda: bot.poll() is not None, timeout=25))
        return checker.summary()
    except Exception as exc:  # pragma: no cover
        import traceback

        traceback.print_exc()
        checker.check("官方通道自检未抛出异常", False, str(exc))
        return 1
    finally:
        if bot is not None and bot.poll() is None:
            bot.terminate()
            wait_for(lambda: bot.poll() is not None, timeout=10)
        client.close()
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
