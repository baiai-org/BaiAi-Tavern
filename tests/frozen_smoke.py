"""打包产物自检：直接运行 dist 下的 exe，验证打包后依然可用。

前置条件::

    pyinstaller --noconfirm --clean pyinstaller_bot.spec
    pyinstaller --noconfirm --clean pyinstaller.spec

然后运行::

    python -m tests.frozen_smoke

阶段一：直接运行 ``dist/bot.exe``，验证能启动、连上（mock）官方机器人平台、导入角色卡、
        回复单聊、发送主动消息、优雅退出。
阶段二：运行 ``dist/BaiAi-Tavern.exe``（offscreen 模式），验证 GUI 能拉起同目录下的
        ``bot.exe``，并且整条链路（界面 → Bot → mock 开放平台）可用。

``dist/`` 还没打包，或者产物比当前源码旧时，本自检会**明确说明原因并跳过（退出码 0）**：
用一个旧的 exe 去验证新代码毫无意义，遇到这种情况请先重新打包。
加 ``--force`` 可以强制跑（例如只想确认旧产物还能启动）。
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
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests import card_factory, mock_servers, smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 打包时会一起打进 exe 的源码目录（用来判断产物是否比代码旧）
SOURCE_DIRS = ("app", "bot", "common", "scripts", "installer")
SOURCE_FILES = (
    "config.example.yaml",
    "pyinstaller.spec",
    "pyinstaller_bot.spec",
    "requirements.txt",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def official_values(mock) -> Dict[str, Any]:
    """官方机器人凭据（指向 mock 开放平台）。"""
    return {
        "app_id": mock_servers.OFFICIAL_APP_ID,
        "app_secret": mock_servers.OFFICIAL_APP_SECRET,
        "api_domain": mock.base_url,
        "token_url": "%s/app/getAppAccessToken" % mock.base_url,
        "sandbox": False,
        "target_openid": "user-frozen",
        "group_openid": "",
        "allow_all_users": True,
        "allowed_users": [],
        "allowed_groups": [],
        "markdown": False,
        "max_reply_segments": 3,
        "reply_segment_max_len": 200,
    }


def write_config(
    data_dir: Path, api_port: int, mock, start_bot: bool = False, onboarding_done: bool = True
) -> Path:
    """写出一份「官方机器人」配置：LLM 与 QQ 都指向 mock。"""
    import yaml

    smoke_test.API_PORT = api_port
    smoke_test.BASE_URL = "http://127.0.0.1:%d" % api_port
    smoke_test.MOCK_URL = mock.base_url
    path = smoke_test.write_bot_config(data_dir)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    app = data.setdefault("app", {})
    app.update({"start_bot_on_launch": start_bot, "onboarding_done": onboarding_done})
    app.pop("start_napcat_on_launch", None)

    data.setdefault("api", {}).update({"host": "127.0.0.1", "port": int(api_port)})
    data.setdefault("llm", {}).update(
        {"base_url": "%s/v1" % mock.base_url, "api_key": "mock-key", "model": "mock-model"}
    )
    qq = data.setdefault("qq", {})
    official = dict(qq.get("official") or {})
    official.update(official_values(mock))
    qq["official"] = official
    qq.update({"name": "打包机器人", "enabled": True, "reply_enabled": True, "group_reply_enabled": False})
    for key in ("mode", "napcat_api_url", "target_user_id", "access_token", "self_id"):
        qq.pop(key, None)
    data.setdefault("characters", {})["import_builtin"] = False
    data.pop("napcat", None)
    data.pop("onebot", None)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def pid_listening_on(port: int) -> Optional[int]:
    """用 netstat 找到监听指定端口的进程号（清理残留进程用）。"""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        return None
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":%d" % port) and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def kill_pid(pid: Optional[int], tree: bool = True) -> None:
    if not pid:
        return
    command = ["taskkill", "/F", "/PID", str(pid)]
    if tree:
        command.insert(2, "/T")  # onefile 打包的程序有「引导父进程 + 真正子进程」两层
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=20,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        pass


def stop_process(process: subprocess.Popen, timeout: float = 10.0) -> bool:
    """停止进程（含 onefile 的子进程），返回是否已结束。"""
    if process.poll() is not None:
        return True
    # onefile 打包后 terminate 只结束引导进程，真正的子进程会变成孤儿，因此用进程树结束
    kill_pid(process.pid)
    if wait_for(lambda: process.poll() is not None, timeout=timeout):
        return True
    try:
        process.kill()
    except Exception:
        pass
    return wait_for(lambda: process.poll() is not None, timeout=5)


def find_exe(name: str) -> Path:
    """在发布目录或 dist 根目录中查找 exe。"""
    for candidate in (ROOT / "dist" / "BaiAi-Tavern" / name, ROOT / "dist" / name):
        if candidate.exists():
            return candidate
    return ROOT / "dist" / name


def newest_source_mtime() -> Tuple[float, str]:
    """返回 (最新源码修改时间, 文件相对路径)。"""
    newest: Tuple[float, str] = (0.0, "")
    candidates: List[Path] = []
    for name in SOURCE_DIRS:
        base = ROOT / name
        if base.exists():
            candidates.extend(
                item
                for item in base.rglob("*")
                if item.is_file() and "__pycache__" not in item.parts
            )
    resources = ROOT / "resources"
    if resources.exists():
        candidates.extend(item for item in resources.rglob("*") if item.is_file())
    for name in SOURCE_FILES:
        item = ROOT / name
        if item.exists():
            candidates.append(item)
    for item in candidates:
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        if mtime > newest[0]:
            newest = (mtime, str(item.relative_to(ROOT)))
    return newest


def build_state(force: bool = False) -> Tuple[str, str]:
    """判断打包产物是否可用：``ready`` / ``missing`` / ``stale``。"""
    gui_exe = find_exe("BaiAi-Tavern.exe")
    bot_exe = find_exe("bot.exe")
    missing = [item.name for item in (gui_exe, bot_exe) if not item.exists()]
    if missing:
        return "missing", "找不到打包产物：%s（dist 目录：%s）" % ("、".join(missing), ROOT / "dist")
    if force:
        return "ready", ""
    newest_at, newest_file = newest_source_mtime()
    oldest_exe = min(gui_exe, bot_exe, key=lambda item: item.stat().st_mtime)
    newest_exe_at = max(gui_exe.stat().st_mtime, bot_exe.stat().st_mtime)
    if newest_at > newest_exe_at:
        return "stale", (
            "打包产物比源码旧：最新源码 %s（%s），而 exe 中较新的也只有 %s"
            % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest_at)),
                newest_file,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest_exe_at)),
            )
        )
    return "ready", "旧产物=%s" % oldest_exe.name


def check_official_status(checker, status: Dict[str, Any], label: str) -> None:
    """打包产物的状态快照必须是「只有官方机器人」的形态。"""
    checker.check("%s 的状态快照里没有 napcat / target_user_id（旧通道已移除）" % label,
                  "napcat" not in status and "target_user_id" not in status, str(sorted(status.keys())))
    checker.check("%s 识别到官方机器人凭据" % label, bool((status.get("qq") or {}).get("configured")))
    checker.check(
        "%s 的机器人列表都是官方通道" % label,
        [item.get("mode") for item in (status.get("bots") or [])] == ["official"],
        json.dumps(status.get("bots") or [], ensure_ascii=False)[:200],
    )
    checker.check("%s 识别到 LLM 配置" % label, bool((status.get("llm") or {}).get("configured")))
    checker.check(
        "%s 上报了主动消息调度状态" % label,
        bool((status.get("proactive") or {}).get("running")),
    )


# =============================================================== 阶段一 =====
def phase_bot_exe(checker, mock) -> None:
    print("\n" + "=" * 74)
    print("  阶段一：bot.exe 功能自检")
    print("=" * 74)
    exe = find_exe("bot.exe")
    checker.check("存在 bot.exe", exe.exists())
    if not exe.exists():
        return
    print("  被测文件：%s（%.1f MB）" % (exe, exe.stat().st_size / 1048576))

    api_port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="tavern-frozen-bot-"))
    write_config(data_dir, api_port, mock)
    cards_dir = data_dir / "characters"
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_factory.write_json_card(cards_dir / "打包角色.json", "打包角色")

    env = os.environ.copy()
    env.update({"QQAI_DATA_DIR": str(data_dir), "QQAI_HOME": str(exe.parent)})

    process = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
    )
    output = smoke_test.drain(process)
    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=30.0)
    try:
        healthy = wait_for(lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=120)
        checker.check("bot.exe 能启动并提供接口", healthy)
        if not healthy:
            print("---- 进程输出 ----")
            print("\n".join(output[-40:]))
            log_file = data_dir / "logs" / "bot.log"
            if log_file.exists():
                print("\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]))
            return

        status = client.get("/api/status").json()
        check_official_status(checker, status, "bot.exe")
        checker.check(
            "bot.exe 连上了官方网关（mock 开放平台）",
            wait_for(lambda: bool((client.get("/api/status").json().get("qq") or {}).get("connected")), timeout=90),
            json.dumps(status.get("qq") or {}, ensure_ascii=False)[:200],
        )

        scan = client.post("/api/characters/scan", timeout=60).json()
        checker.check(
            "bot.exe 能扫描导入角色卡",
            len(scan.get("imported") or []) == 1,
            json.dumps(scan, ensure_ascii=False)[:200],
        )

        before = len(mock.official_sent())
        mock.emit_c2c("打包后你还在吗")
        checker.check(
            "bot.exe 能回复官方机器人单聊消息",
            wait_for(
                lambda: any(
                    "打包版回复正常" in str(item.get("content"))
                    for item in mock.official_sent()[before:]
                ),
                timeout=60,
            ),
            json.dumps(mock.official_sent()[before:], ensure_ascii=False)[:200],
        )

        result = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
        checker.check("bot.exe 能发送主动消息", bool(result.get("ok")), json.dumps(result, ensure_ascii=False)[:200])
        checker.check(
            "主动消息内容正确",
            "打包版主动消息" in str(result.get("content")),
            str(result.get("content")),
        )

        log_file = data_dir / "logs" / "bot.log"
        checker.check("bot.exe 写出日志文件", log_file.exists())
        if log_file.exists():
            content = log_file.read_text(encoding="utf-8", errors="replace")
            checker.check("日志中文正常（无乱码）", "主动消息" in content and "回复" in content)

        checker.check("优雅退出接口可用", client.post("/api/shutdown", timeout=15).status_code == 200)
        checker.check(
            "bot.exe 进程正常退出",
            wait_for(lambda: process.poll() is not None, timeout=25),
        )
    finally:
        stop_process(process)
        client.close()


# =============================================================== 阶段二 =====
def phase_gui_exe(checker, mock) -> None:
    print("\n" + "=" * 74)
    print("  阶段二：BaiAi-Tavern.exe 集成自检（由界面拉起 bot.exe）")
    print("=" * 74)
    exe = find_exe("BaiAi-Tavern.exe")
    checker.check("存在 BaiAi-Tavern.exe", exe.exists())
    if not exe.exists():
        return
    checker.check(
        "bot.exe 与主程序同目录（界面可自动拉起）",
        (exe.parent / "bot.exe").exists(),
    )
    print("  被测文件：%s（%.1f MB）" % (exe, exe.stat().st_size / 1048576))

    api_port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="tavern-frozen-gui-"))
    # 让界面自己拉起 bot.exe；onboarding_done=False 用来验证打包版也会做引导判断
    write_config(data_dir, api_port, mock, start_bot=True, onboarding_done=False)
    cards_dir = data_dir / "characters"
    cards_dir.mkdir(parents=True, exist_ok=True)
    card_factory.write_json_card(cards_dir / "界面角色.json", "界面角色")

    env = os.environ.copy()
    env.update(
        {
            "QQAI_DATA_DIR": str(data_dir),
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONIOENCODING": "utf-8",
            # 单实例锁是机器级命名键：用户正在运行安装版时，默认键已被占用，
            # 测试实例抢锁失败会弹模态框卡死（界面拉起 bot 超时）。用唯一键隔离。
            "BAIAI_INSTANCE_SUFFIX": "frozen-%d" % os.getpid(),
        }
    )

    process = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        close_fds=True,
    )
    output = smoke_test.drain(process)
    client = httpx.Client(base_url="http://127.0.0.1:%d" % api_port, timeout=30.0)
    bot_pid: Optional[int] = None
    try:
        alive = wait_for(lambda: process.poll() is None, timeout=5) or process.poll() is None
        checker.check("界面进程启动后保持运行（没有缺依赖/闪退）", alive)

        online = wait_for(
            lambda: client.get("/api/health", timeout=5).status_code == 200, timeout=150
        )
        checker.check("界面自动拉起了打包的 bot.exe（接口可用）", online, "\n".join(output[-20:]))
        if not online:
            print("---- 界面输出 ----")
            print("\n".join(output[-40:]))
            return

        bot_pid = pid_listening_on(api_port)
        status = client.get("/api/status").json()
        check_official_status(checker, status, "打包版界面 + Bot")
        checker.check("Bot 在线状态正常", bool(status.get("online")))

        scan = client.post("/api/characters/scan", timeout=60).json()
        checker.check(
            "打包版能导入角色卡",
            len(scan.get("imported") or []) == 1,
            json.dumps(scan, ensure_ascii=False)[:160],
        )

        before = len(mock.official_sent())
        mock.emit_c2c("打包版桌面端在吗")
        checker.check(
            "打包版全链路能回复官方机器人单聊消息",
            wait_for(
                lambda: any(
                    "打包版回复正常" in str(item.get("content")) for item in mock.official_sent()[before:]
                ),
                timeout=60,
            ),
            json.dumps(mock.official_sent()[before:], ensure_ascii=False)[:200],
        )

        result = client.post("/api/proactive/trigger", json={"force": True}, timeout=120).json()
        checker.check(
            "打包版全链路发送主动消息成功",
            bool(result.get("ok")) and "打包版主动消息" in str(result.get("content")),
            json.dumps(result, ensure_ascii=False)[:200],
        )

        gui_log = data_dir / "logs" / "gui.log"
        checker.check("界面写出 gui.log", gui_log.exists())
        if gui_log.exists():
            content = gui_log.read_text(encoding="utf-8", errors="replace")
            checker.check(
                "gui.log 记录了启动与主题信息",
                "GUI" in content and "主题" in content,
                content[-200:],
            )
        bot_log = data_dir / "logs" / "bot.log"
        checker.check("被拉起的 bot.exe 写出 bot.log", bot_log.exists())

        # 打包版里同样包含「配置引导」相关逻辑：已配置好的用户会被自动补上
        # app.onboarding_done 标记（未配置的用户则会停在引导界面等待操作）。
        import yaml

        config_file = data_dir / "config.yaml"
        marked = wait_for(
            lambda: bool(
                ((yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}).get("app") or {}).get(
                    "onboarding_done"
                )
            ),
            timeout=40,
            interval=0.5,
        )
        checker.check("打包版界面执行了配置引导判断（已配置→补标记）", marked)
    finally:
        stop_process(process)
        # 清理被界面拉起的 Bot 进程（界面被强杀时它会变成孤儿进程）
        kill_pid(bot_pid or pid_listening_on(api_port))
        checker.check(
            "清理后控制接口端口已释放（无残留 Bot 进程）",
            pid_listening_on(api_port) is None,
        )
        client.close()


def main() -> int:
    checker = smoke_test.Checker()
    print("BaiAi-Tavern 打包产物自检开始（Python %s）" % sys.version.split()[0])

    force = "--force" in sys.argv[1:]
    state, detail = build_state(force=force)
    if state != "ready":
        reason = "dist 目录尚未打包" if state == "missing" else "打包产物早于当前源码"
        print("\n" + "=" * 74)
        print("  打包自检已跳过：%s" % reason)
        print("  %s" % detail)
        print("  用一个旧 exe 验证新代码没有意义；请先重新打包后重跑：")
        print("    pyinstaller --noconfirm --clean pyinstaller_bot.spec")
        print("    pyinstaller --noconfirm --clean pyinstaller.spec")
        print("  如果确认要跑当前产物（例如只想验证旧包还能启动），加 --force。")
        print("=" * 74)
        return 0
    if detail:
        print("  产物检查：%s" % detail)

    mock_port = free_port()
    smoke_test.MOCK_URL = "http://127.0.0.1:%d" % mock_port
    mock = mock_servers.MockProcess(port=mock_port).start()
    mock.reset(reply_text="打包版回复正常。", proactive_text="打包版主动消息。")
    try:
        phase_bot_exe(checker, mock)
        phase_gui_exe(checker, mock)
    finally:
        mock.stop()
    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
