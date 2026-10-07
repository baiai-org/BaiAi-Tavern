"""定时触发的“真实到点”验证（慢速自检，约 2~3 分钟）。

其它自检都用「手动触发」验证发送链路；本脚本让调度器**自己**在到点时发送，
从而验证 APScheduler 任务注册、到点回调、准入检查链与官方通道发送链路真正连通。

运行::

    python -m tests.scheduler_live

做法：
* 只开**定时触发**（``scheduled_times`` 设为「下一分钟」），空闲 / 随机触发都关掉，
  这样收到的每一条主动消息都必然来自 CronTrigger；
* 主动消息目标用配置里的 ``qq.official.target_openid``（脚本不向机器人投递任何消息，
  也不调用任何手动触发接口）；
* 到点后断言 mock 官方平台真的收到了 ``POST /v2/users/{openid}/messages``。

说明：官方通道的定时任务带 90 秒随机抖动（避免机器人整点同时发言），
所以等待窗口要给足（下一分钟 + 抖动 + 一点余量）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests import card_factory, mock_servers, smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

#: 定时任务带 90 秒抖动，加上「等到下一分钟」最多 60 秒，再留一点余量
WAIT_SECONDS = 210


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    checker = smoke_test.Checker()
    print("调度器到点自检开始（QQ 官方机器人通道，Python %s）" % sys.version.split()[0])

    mock_port = free_port()
    api_port = free_port()
    smoke_test.MOCK_URL = "http://127.0.0.1:%d" % mock_port
    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port

    mock = mock_servers.MockProcess(port=mock_port).start()
    mock.reset(reply_text="回复。", proactive_text="定时器到点啦。")

    data_dir = Path(tempfile.mkdtemp(prefix="tavern-sched-"))
    config_path = smoke_test.write_bot_config(data_dir)
    smoke_test.clear_logs(data_dir)

    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    next_minute = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")
    data["proactive"].update(
        {
            "scheduled_enabled": True,
            "scheduled_times": [next_minute],
            "idle_enabled": False,      # 只留定时触发，收到的消息必然来自 CronTrigger
            "random_enabled": False,
            "global_daily_limit": 10,
            "min_interval_minutes": 0,
            "probability": 1.0,
        }
    )
    # 官方通道只能按 openid 发消息：这里显式指定目标（脚本不投递任何用户消息）
    data["qq"]["official"]["target_openid"] = mock_servers.DEFAULT_OPENID
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cards_dir = data_dir / "characters"
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_factory.write_json_card(cards_dir / "定时角色.json", "定时角色")

    env = os.environ.copy()
    env.update({"QQAI_DATA_DIR": str(data_dir), "QQAI_HOME": str(ROOT), "PYTHONPATH": str(ROOT)})
    process = subprocess.Popen(
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
    output = smoke_test.drain(process)
    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=30.0)

    try:
        healthy = smoke_test.wait_for(
            lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=120
        )
        checker.check("Bot 进程已启动", healthy)
        if not healthy:
            print("\n".join(output[-40:]))
            return 1

        client.post("/api/characters/scan", timeout=60)
        checker.check(
            "角色已导入并启用",
            len(client.get("/api/characters", params={"enabled_only": True}).json()) == 1,
        )
        checker.check(
            "官方网关已连接（否则发不出消息）",
            smoke_test.wait_for(
                lambda: bool((client.get("/api/qq/status").json() or {}).get("connected")), timeout=90
            ),
        )

        status = client.get("/api/proactive/status").json()
        jobs = status.get("jobs") or []
        names = [str(job.get("name")) for job in jobs]
        checker.check("调度器已注册定时任务", any("定时主动消息" in name for name in names), str(names))
        checker.check("调度器已注册配置热重载任务", any("配置热重载" in name for name in names), str(names))
        upcoming = [job for job in jobs if job.get("next_run")]
        checker.check("定时任务给出了下次执行时间", bool(upcoming), json.dumps(upcoming, ensure_ascii=False))
        for job in jobs:
            print("    [job] %s next_run=%s" % (job.get("name"), job.get("next_run")))
        checker.check(
            "定时任务的下次执行时间是配置的那一刻（任务名按机器人区分，V0.2.2）",
            any(
                str(job.get("name")).startswith("定时主动消息 " + next_minute)
                for job in jobs
            ),
            "%s / %s" % (next_minute, str(names)),
        )

        print("  等待调度器自动发送（最多 %d 秒，全程不调用任何手动触发接口）…" % WAIT_SECONDS)
        import time as _time

        _deadline = _time.time() + WAIT_SECONDS
        _last_dump = 0.0
        sent = False
        while _time.time() < _deadline:
            if len(mock.official_sent()) >= 1:
                sent = True
                break
            if _time.time() - _last_dump > 20:
                _last_dump = _time.time()
                try:
                    _st = client.get("/api/proactive/status", timeout=5).json()
                    for _job in _st.get("jobs") or []:
                        print(
                            "    [poll] %s next_run=%s running=%s"
                            % (_job.get("name"), _job.get("next_run"), _st.get("running"))
                        )
                except Exception:
                    pass
            _time.sleep(1.0)
        checker.check(
            "调度器到点后自动发送了主动消息",
            sent,
            json.dumps(mock.official_sent(), ensure_ascii=False)[:300],
        )
        if sent:
            message = mock.official_sent()[0]
            checker.check("消息走官方单聊接口", message.get("kind") == "c2c", str(message.get("kind")))
            checker.check(
                "消息内容来自 LLM 的主动分支",
                "到点" in str(message.get("content")),
                str(message.get("content")),
            )
            checker.check(
                "消息发送给配置的目标 openid",
                message.get("openid") == mock_servers.DEFAULT_OPENID,
                str(message.get("openid")),
            )
            checker.check(
                "定时主动消息不带 msg_id（不是被动回复）",
                not message.get("msg_id"),
                str({k: message.get(k) for k in ("msg_id", "msg_seq")}),
            )
            logs = client.get("/api/proactive/logs").json()
            trigger_types = {str(item.get("trigger_type")) for item in logs}
            checker.check(
                "主动日志记录了 scheduled 触发",
                "scheduled" in trigger_types,
                str(trigger_types),
            )
            stats = client.get("/api/stats/today").json()
            checker.check("今日统计已计入", int(stats.get("proactive_total") or 0) >= 1, str(stats))
        return checker.summary()
    finally:
        try:
            client.post("/api/shutdown", timeout=10)
        except Exception:
            pass
        if not smoke_test.wait_for(lambda: process.poll() is not None, timeout=20):
            process.terminate()
            smoke_test.wait_for(lambda: process.poll() is not None, timeout=10)
        client.close()
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
