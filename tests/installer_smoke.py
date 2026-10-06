"""安装程序自检：真的装一次、再卸一次，验证快捷方式、卸载信息与「一个 EXE」的设计。

运行::

    python -m tests.installer_smoke

三个阶段：

1. **源码模式**：用小体积假 payload 跑 ``installer.installer_main --silent`` 与
   ``--silent --uninstall``，验证复制 / 快捷方式 / 注册表 / 保留用户数据；
2. **主程序卸载路径**：模拟「设置 → 应用」点卸载（安装目录里的主程序 ``--uninstall``），
   验证卸载信息立刻消失、程序文件被清理、正在运行的自己由延迟批处理删掉；
3. **升级安装**：旧版本还在运行时也要能装上（先自动关掉被占用进程）；
4. **成品模式**（存在 ``dist\\BaiAi-Tavern V0.2.exe`` 时执行）：
   用真实安装包装一次、再用装出来的主程序 ``--uninstall`` 卸一次。

所有操作都指向临时目录与独立的注册表项，不会影响机器上真实的安装。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer import common as ic  # noqa: E402
from tests import smoke_test  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TEST_APP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BaiAi-Tavern-Selftest"
SETUP_EXE_NAME = "%s %s.exe" % (ic.APP_NAME, ic.APP_VERSION)


def make_payload(directory: Path) -> Path:
    """造一份小体积 payload（结构同真实安装包）。

    exe 用系统自带的 ``cmd.exe`` 冒充：Windows 只肯给「真正的可执行文件」建快捷方式，
    用假字节会连快捷方式都建不出来，测不出真实安装的行为。
    """
    stand_in = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    (directory / "resources" / "styles").mkdir(parents=True, exist_ok=True)
    shutil.copy2(stand_in, directory / ic.EXE_NAME)
    shutil.copy2(stand_in, directory / ic.BOT_EXE_NAME)
    (directory / "config.example.yaml").write_text("app:\n  theme: dark\n", encoding="utf-8")
    (directory / ic.LAUNCHER_NAME).write_text("@echo off\r\n", encoding="utf-8")
    (directory / "resources" / "styles" / "dark.qss").write_text("QWidget { }\n", encoding="utf-8")
    # 旧版本残留：安装时应该被清掉
    (directory / ic.LEGACY_UNINSTALLER_NAME).write_bytes(b"MZ-legacy-uninstaller")
    return directory


def wait_until(predicate, seconds: float = 30.0) -> bool:
    """轮询等待条件成立（延迟自删除是异步的，需要等它跑完）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.4)
    return predicate()


def gui_alive(exe: Path, wait: float = 8.0, env: Optional[Dict[str, str]] = None) -> bool:
    """打开图形界面，等几秒确认它还活着，再连子进程一起结束掉。

    只验证「窗口能打开、不是一闪就退」（打包漏了 tkinter / Tcl 会在这里暴露），
    不点任何按钮，所以不会真的安装或卸载任何东西。
    """
    exe = Path(exe)
    if not exe.exists():
        return False
    try:
        proc = subprocess.Popen(
            [str(exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
            env=env,
        )
    except Exception:
        return False
    try:
        time.sleep(wait)
        return proc.poll() is None
    finally:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=60,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except Exception:
            proc.kill()


def run_silent(module: str, args: List[str], env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", module] + args
    merged = dict(os.environ)
    merged.update(env or {})
    merged["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        command,
        cwd=str(ROOT),
        env=merged,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def run_update_phase(tmp: Path, checker) -> None:
    """更新系统自检：版本比较 / 附件挑选 / SHA256 / 下载与进度 / 限流 / 静默安装拉起。

    用一个本地 HTTP 服务冒充 GitHub API + 下载源，不依赖外网。
    """
    import http.server
    import threading

    from app import updater

    # ------------------------------------------------------------ 版本解析与比较
    checker.check(
        "版本号解析（V0.2 / v0.10.1 / 无数字）",
        updater.parse_version("V0.2") == (0, 2)
        and updater.parse_version("v0.10.1") == (0, 10, 1)
        and updater.parse_version("abc") == (),
        str(updater.parse_version("v0.10.1")),
    )
    checker.check(
        "新版判定（0.3>0.2，0.10>0.9 按数字比较，无法比较时不算新）",
        updater.is_newer("v0.3", "V0.2") is True
        and updater.is_newer("v0.2", "V0.2") is False
        and updater.is_newer("v0.10", "V0.9") is True
        and updater.is_newer("abc", "V0.2") is False,
        "",
    )
    checker.check(
        "版本号显示归一化（v0.3 → V0.3）",
        updater.normalize_version("v0.3") == "V0.3"
        and updater.normalize_version("release 0.10") == "V0.10",
        str(updater.normalize_version("v0.3")),
    )

    # ------------------------------------------------------------ Release 附件挑选
    fake_release = {
        "tag_name": "v0.3",
        "html_url": "https://github.com/baiai-org/BaiAi-Tavern/releases/tag/v0.3",
        "body": "# V0.3\n\n- 更新系统\n- 一些修复",
        "assets": [
            {"name": "BaiAi-Tavern.exe", "browser_download_url": "http://127.0.0.1/x/main.exe"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "http://127.0.0.1/x/SHA256SUMS.txt"},
            {"name": "BaiAi-Tavern-V0.3.exe", "browser_download_url": "http://127.0.0.1/x/BaiAi-Tavern-V0.3.exe"},
            {"name": "README.md", "browser_download_url": "http://127.0.0.1/x/README.md"},
        ],
    }
    asset = updater.pick_installer_asset(fake_release)
    checker.check(
        "从附件里挑出安装包（跳过主程序 EXE / 非 EXE）",
        asset is not None and asset["name"] == "BaiAi-Tavern-V0.3.exe",
        str(asset.get("name") if asset else None),
    )
    checker.check(
        "能挑出 SHA256SUMS.txt（用于下载校验）",
        (updater.pick_sums_asset(fake_release) or {}).get("name") == "SHA256SUMS.txt",
        "",
    )
    space_release = {"tag_name": "v0.3", "assets": [{"name": "BaiAi-Tavern V0.3.exe", "browser_download_url": "http://x/y.exe"}]}
    checker.check(
        "带空格的安装包名也能认出来（历史命名兼容）",
        (updater.pick_installer_asset(space_release) or {}).get("name") == "BaiAi-Tavern V0.3.exe",
        "",
    )
    checker.check("Release 没有安装包附件时返回 None", updater.pick_installer_asset({"tag_name": "v0.3", "assets": []}) is None, "")

    # ------------------------------------------------------------ SHA256 校验
    sums_file = tmp / "verify"
    sums_file.mkdir(parents=True, exist_ok=True)
    sample = sums_file / "BaiAi-Tavern-V0.3.exe"
    sample.write_bytes(b"fake-installer-bytes-for-verify")
    real_hash = updater.sha256_file(sample)
    checker.check(
        "SHA256 校验：一致=True，不一致=False，找不到行=None（跳过）",
        updater.verify_sha256(sample, "%s  BaiAi-Tavern-V0.3.exe\n" % real_hash) is True
        and updater.verify_sha256(sample, "0" * 64 + "  BaiAi-Tavern-V0.3.exe\n") is False
        and updater.verify_sha256(sample, "%s  Other.exe\n" % real_hash) is None,
        real_hash[:16],
    )

    # ------------------------------------------------------------ 本地 GitHub 模拟：检查 + 下载
    exe_bytes = b"fake-updater-installer-payload" * 4096  # ~80KB，模拟安装包
    sums_text = "%s  BaiAi-Tavern-V0.3.exe\n" % updater.sha256_file(_write_bytes(sums_file / "real.exe", exe_bytes))
    served = {
        "BaiAi-Tavern-V0.3.exe": exe_bytes,
        "SHA256SUMS.txt": sums_text.encode("utf-8"),
    }
    local_release = dict(fake_release)
    local_release["assets"] = [
        {"name": "BaiAi-Tavern-V0.3.exe", "browser_download_url": "http://127.0.0.1:PORT/x/BaiAi-Tavern-V0.3.exe"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "http://127.0.0.1:PORT/x/SHA256SUMS.txt"},
    ]

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/"):
                body = json.dumps(local_release).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            name = self.path.rsplit("/", 1)[-1]
            data = served.get(name)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # /api 前缀 = GitHub API 路径；其余路径按文件名返回预置文件
    api_base = "http://127.0.0.1:%d/api" % port
    # 把占位符 PORT 换成真实端口
    local_release = json.loads(json.dumps(local_release).replace("127.0.0.1:PORT", "127.0.0.1:%d" % port))

    try:
        checker.check(
            "从模拟 GitHub API 拉到最新 Release",
            updater.latest_release(api_base=api_base).get("tag_name") == "v0.3",
            "",
        )
        info = updater.check_latest("V0.2", api_base=api_base)
        checker.check(
            "check_latest：发现新版且挑出安装包附件",
            info.get("newer") is True
            and info.get("latest_display") == "V0.3"
            and (info.get("asset") or {}).get("name") == "BaiAi-Tavern-V0.3.exe",
            str({k: info.get(k) for k in ("newer", "latest_display", "current_display")}),
        )
        checker.check("check_latest：已是最新时 newer=False", updater.check_latest("V0.3", api_base=api_base).get("newer") is False, "")

        progress_calls: List[tuple] = []
        download_dir = tmp / "download"
        download_dir.mkdir(exist_ok=True)
        path = updater.download(info["asset"]["browser_download_url"], dest_dir=download_dir,
                                progress=lambda r, t: progress_calls.append((int(r), int(t or 0))))
        checker.check(
            "下载安装包成功且字节一致",
            path.is_file() and path.read_bytes() == exe_bytes,
            str(path),
        )
        checker.check(
            "下载进度回调有上报且累计字节单调不减",
            len(progress_calls) >= 1
            and all(a <= b for a, b in zip([r for r, _ in progress_calls], [r for r, _ in progress_calls][1:])),
            "上报 %d 次，最后 %s" % (len(progress_calls), progress_calls[-1] if progress_calls else "-"),
        )
        checker.check(
            "按 SHA256SUMS.txt 校验下载文件通过",
            updater.verify_sha256(path, sums_text) is True,
            "",
        )
        try:
            updater.latest_release(api_base="http://127.0.0.1:%d/none" % port)
            checker.check("API 404 时抛出可读的 UpdateError", False, "未抛出异常")
        except updater.UpdateError as exc:
            checker.check("API 404 时抛出可读的 UpdateError", "Release" in str(exc) or "404" in str(exc) or "限流" in str(exc), str(exc))
    finally:
        server.shutdown()

    # ------------------------------------------------------------ 启动检查限流
    checker.check(
        "启动检查限流：没查过=查，6 小时内=不查，超过=查，坏时间戳=查",
        updater.should_auto_check(lambda key, default=None: default) is True
        and updater.should_auto_check(lambda key, default=None: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))) is False
        and updater.should_auto_check(lambda key, default=None: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7 * 3600))) is True
        and updater.should_auto_check(lambda key, default=None: "not-a-time") is True,
        "",
    )

    # ------------------------------------------------------------ 静默安装拉起（.bat 冒充安装包）
    launch_dir = tmp / "launch"
    launch_dir.mkdir(exist_ok=True)
    fake_bat = launch_dir / "fake-installer.bat"
    fake_bat.write_text("@echo off\r\necho %* > marker.txt\r\n", encoding="ascii")
    marker = launch_dir / "marker.txt"
    old_cwd = os.getcwd()
    os.chdir(launch_dir)
    try:
        updater.launch_installer(fake_bat, launch_dir / "target", launch_after=True)
        checker.check(
            "launch_installer 以 --silent --dir 拉起安装程序",
            wait_until(marker.exists, seconds=15)
            and "--silent" in marker.read_text(encoding="ascii")
            and str(launch_dir / "target") in marker.read_text(encoding="ascii")
            and "--no-run" not in marker.read_text(encoding="ascii"),
            marker.read_text(encoding="ascii").strip() if marker.exists() else "（marker 未生成）",
        )
        if marker.exists():
            marker.unlink()
        updater.launch_installer(fake_bat, launch_dir / "target2", launch_after=False)
        checker.check(
            "重装 / 修复流程带 --no-run（装完不自动启动新版）",
            wait_until(lambda: marker.exists() and "--no-run" in marker.read_text(encoding="ascii"), seconds=15),
            marker.read_text(encoding="ascii").strip() if marker.exists() else "（marker 未生成）",
        )
    finally:
        os.chdir(old_cwd)


def _write_bytes(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def main() -> int:
    checker = smoke_test.Checker()
    print("安装程序自检开始（Python %s）" % sys.version.split()[0])
    tmp = Path(tempfile.mkdtemp(prefix="baiai-installer-"))
    payload = make_payload(tmp / "payload")
    # 进程内直接调用 ic.install 的阶段（升级安装）也走同一份假 payload，
    # 避免依赖 build\payload 是否存在（未打包的工作区里没有它）。
    os.environ["BAIAI_PAYLOAD"] = str(payload)
    install_dir = tmp / "app"
    desktop = tmp / "desktop"
    startmenu = tmp / "startmenu"
    desktop.mkdir(parents=True, exist_ok=True)
    startmenu.mkdir(parents=True, exist_ok=True)

    try:
        # ------------------------------------------------------ 第一阶段：源码模式
        checker.phase("1/5 安装 / 卸载逻辑（源码模式）")
        result = run_silent(
            "installer.installer_main",
            [
                "--silent",
                "--dir",
                str(install_dir),
                "--desktop-dir",
                str(desktop),
                "--startmenu-dir",
                str(startmenu),
                "--app-key",
                TEST_APP_KEY,
                "--no-run",
            ],
            env={"BAIAI_PAYLOAD": str(payload)},
        )
        checker.check("静默安装返回成功", result.returncode == 0, (result.stdout or "")[-300:] + (result.stderr or "")[-200:])
        checker.check(
            "程序文件已复制到安装目录",
            (install_dir / ic.EXE_NAME).exists()
            and (install_dir / ic.BOT_EXE_NAME).exists()
            and (install_dir / "resources" / "styles" / "dark.qss").exists(),
            str(sorted(p.name for p in install_dir.iterdir())[:8]) if install_dir.exists() else "（目录不存在）",
        )
        shortcut = desktop / ("%s.lnk" % ic.APP_NAME)
        checker.check("桌面快捷方式已创建", shortcut.is_file(), str([p.name for p in desktop.iterdir()]))
        checker.check(
            "快捷方式能被系统识别，并指向安装目录里的主程序",
            shortcut.is_file() and ic.lnk_target(shortcut).lower() == str(install_dir / ic.EXE_NAME).lower(),
            "读回=%s（写入方式：%s）" % (ic.lnk_target(shortcut) or "空", ic.LAST_SHORTCUT_METHOD),
        )
        menu_folder = startmenu / ic.APP_NAME
        checker.check(
            "开始菜单里只有主程序快捷方式（不再有单独的卸载入口）",
            (menu_folder / ("%s.lnk" % ic.APP_NAME)).is_file()
            and not any("卸载" in p.name for p in menu_folder.glob("*.lnk")),
            str([p.name for p in menu_folder.iterdir()]) if menu_folder.exists() else "（目录不存在）",
        )
        entry = ic.read_uninstall_entry(TEST_APP_KEY)
        checker.check(
            "写入了卸载信息（可在「设置 → 应用」里看到并卸载）",
            entry.get("DisplayName") == ic.APP_NAME
            and entry.get("DisplayVersion") == ic.APP_VERSION
            and entry.get("Publisher") == ic.APP_AUTHOR,
            json.dumps({k: entry.get(k) for k in ("DisplayName", "DisplayVersion", "Publisher")}, ensure_ascii=False),
        )
        checker.check(
            "卸载命令指向主程序 --uninstall（不再是独立卸载 EXE）",
            "--uninstall" in str(entry.get("UninstallString") or "")
            and ic.EXE_NAME in str(entry.get("UninstallString") or "")
            and "--uninstall --silent" in str(entry.get("QuietUninstallString") or ""),
            "%s | %s" % (entry.get("UninstallString"), entry.get("QuietUninstallString")),
        )
        checker.check(
            "记录了安装目录与快捷方式（卸载时按记录清理）",
            str(install_dir) in str(entry.get("InstallLocation") or "")
            and str(shortcut) in str(entry.get("Shortcuts") or ""),
            str(entry.get("Shortcuts"))[:200],
        )
        checker.check(
            "安装时清掉了旧版本的独立卸载程序",
            not (install_dir / ic.LEGACY_UNINSTALLER_NAME).exists(),
            str(sorted(p.name for p in install_dir.iterdir())),
        )
        checker.check("安装后能识别出「已安装」", ic.is_installed(TEST_APP_KEY), json.dumps(ic.installed_info(TEST_APP_KEY), ensure_ascii=False))

        # 造一点用户数据，验证卸载默认保留
        (install_dir / "data").mkdir(parents=True, exist_ok=True)
        (install_dir / "data" / "config.yaml").write_text("# 用户配置\n", encoding="utf-8")

        result = run_silent(
            "installer.installer_main",
            ["--silent", "--uninstall", "--dir", str(install_dir), "--app-key", TEST_APP_KEY],
            env={"BAIAI_PAYLOAD": str(payload)},
        )
        checker.check("静默卸载返回成功", result.returncode == 0, (result.stdout or "")[-300:])
        checker.check(
            "程序文件已删除",
            not (install_dir / ic.EXE_NAME).exists() and not (install_dir / ic.BOT_EXE_NAME).exists(),
            str(sorted(p.name for p in install_dir.iterdir())) if install_dir.exists() else "（目录已删）",
        )
        checker.check(
            "默认保留用户数据（data）",
            (install_dir / "data" / "config.yaml").is_file(),
            str(sorted(p.name for p in install_dir.iterdir())) if install_dir.exists() else "（目录已删）",
        )
        checker.check(
            "快捷方式已删除",
            not shortcut.exists() and not (menu_folder / ("%s.lnk" % ic.APP_NAME)).exists(),
            str([p.name for p in desktop.iterdir()]),
        )
        checker.check("卸载信息已从注册表移除", not ic.read_uninstall_entry(TEST_APP_KEY), str(ic.read_uninstall_entry(TEST_APP_KEY))[:200])

        # 再装一次，验证「同时删除数据」这条路径
        run_silent(
            "installer.installer_main",
            [
                "--silent",
                "--dir",
                str(install_dir),
                "--desktop-dir",
                str(desktop),
                "--startmenu-dir",
                str(startmenu),
                "--app-key",
                TEST_APP_KEY,
                "--no-run",
                "--no-desktop",
                "--no-startmenu",
            ],
            env={"BAIAI_PAYLOAD": str(payload)},
        )
        checker.check(
            "再次安装成功，且按参数跳过快捷方式",
            (install_dir / ic.EXE_NAME).exists() and not shortcut.exists(),
            str(sorted(p.name for p in install_dir.iterdir())[:6]),
        )
        run_silent(
            "installer.installer_main",
            ["--silent", "--uninstall", "--dir", str(install_dir), "--app-key", TEST_APP_KEY, "--remove-data"],
            env={"BAIAI_PAYLOAD": str(payload)},
        )
        checker.check(
            "勾选「同时删除数据」时用户数据一起清掉",
            not (install_dir / "data").exists(),
            str(sorted(p.name for p in install_dir.iterdir())) if install_dir.exists() else "（目录已删）",
        )

        # ------------------------------- 主程序卸载走的那条路（删掉正在运行的自己）
        # 用户反馈过：从「设置 → 应用」或卸载向导卸载后，快捷方式和文件都没了，
        # 但应用列表里还留着 —— 原因是延迟自删除那条分支提前 return，跳过了删注册表。
        checker.phase("2/5 主程序 --uninstall（删除正在运行的自己）")
        gui_dir = tmp / "self-uninstall"
        ic.install(
            install_dir=gui_dir,
            create_desktop=True,
            create_startmenu=True,
            desktop_path=desktop,
            startmenu_path=startmenu,
            reg_path=TEST_APP_KEY,
        )
        (gui_dir / "data").mkdir(parents=True, exist_ok=True)
        (gui_dir / "data" / "config.yaml").write_text("# 用户配置\n", encoding="utf-8")

        result = ic.uninstall(
            install_dir=gui_dir, remove_data=False, reg_path=TEST_APP_KEY, self_exe=gui_dir / ic.EXE_NAME
        )
        checker.check(
            "主程序自卸载返回成功（延迟自删除）",
            result.get("ok") and result.get("deferred"),
            json.dumps(result, ensure_ascii=False)[:200],
        )
        checker.check(
            "【关键】卸载后立刻就不在「设置 → 应用」里了（卸载信息已删）",
            not ic.read_uninstall_entry(TEST_APP_KEY),
            json.dumps(ic.read_uninstall_entry(TEST_APP_KEY), ensure_ascii=False)[:200],
        )
        checker.check(
            "主程序自卸载也会删掉桌面/开始菜单快捷方式",
            not shortcut.exists() and not (startmenu / ic.APP_NAME / ("%s.lnk" % ic.APP_NAME)).exists(),
            str([p.name for p in desktop.iterdir()]),
        )
        checker.check(
            "主程序自卸载会删掉其它程序文件（自己留给延迟命令）",
            not (gui_dir / ic.BOT_EXE_NAME).exists() and not (gui_dir / "resources").exists(),
            str(sorted(p.name for p in gui_dir.iterdir())) if gui_dir.exists() else "（目录已删）",
        )
        checker.check("主程序自卸载默认保留用户数据", (gui_dir / "data" / "config.yaml").is_file())
        checker.check(
            "延迟命令确实把主程序自己删掉了",
            wait_until(lambda: not (gui_dir / ic.EXE_NAME).exists()),
            str(sorted(p.name for p in gui_dir.iterdir())) if gui_dir.exists() else "（目录已删）",
        )
        checker.check(
            "保留数据时安装目录不会连带删掉（只剩用户数据）",
            gui_dir.is_dir() and sorted(p.name for p in gui_dir.iterdir()) == ["data"],
            str(sorted(p.name for p in gui_dir.iterdir())) if gui_dir.exists() else "（目录已删）",
        )

        # 同一路径 + 勾选删除数据
        ic.install(
            install_dir=gui_dir,
            create_desktop=False,
            create_startmenu=False,
            desktop_path=desktop,
            startmenu_path=startmenu,
            reg_path=TEST_APP_KEY,
        )
        ic.uninstall(install_dir=gui_dir, remove_data=True, reg_path=TEST_APP_KEY, self_exe=gui_dir / ic.EXE_NAME)
        checker.check("勾选删数据后注册表也清干净", not ic.read_uninstall_entry(TEST_APP_KEY), "")
        checker.check(
            "勾选删数据时安装目录被彻底删除",
            wait_until(lambda: not gui_dir.exists()),
            str(gui_dir),
        )

        # 已经不在注册表里时重复卸载也不报错（幂等）
        again = ic.uninstall(install_dir=gui_dir, remove_data=False, reg_path=TEST_APP_KEY, self_exe=None)
        checker.check(
            "卸载信息已清后重复卸载不会抛异常（幂等）",
            isinstance(again, dict) and not ic.read_uninstall_entry(TEST_APP_KEY),
            json.dumps(again, ensure_ascii=False)[:160],
        )

        # ------------------------------- 升级安装时旧版本还在运行（用户报过的问题）
        # 正在运行的 exe 会锁住自己，复制就报 Permission denied：
        #   "以下文件复制失败：bot.exe: [Errno 13] Permission denied: ..."
        # 安装程序必须先把安装目录里的旧进程关掉。
        checker.phase("3/5 升级安装：旧版本还在运行时也要能装")
        busy_dir = tmp / "busy-install"
        ic.install(
            install_dir=busy_dir,
            create_desktop=False,
            create_startmenu=False,
            desktop_path=desktop,
            startmenu_path=startmenu,
            reg_path=TEST_APP_KEY,
        )
        running = subprocess.Popen(
            [str(busy_dir / ic.BOT_EXE_NAME)],
            cwd=str(busy_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 保持 stdin 是一条打开的管道：假 payload 里 bot.exe 是 cmd.exe 的替身，
            # stdin 为 DEVNULL（EOF）时它读完就退出了，就锁不住自己的 exe。
            stdin=subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        time.sleep(2.0)
        locked = False
        try:
            with open(busy_dir / ic.BOT_EXE_NAME, "r+b"):
                locked = False
        except PermissionError:
            locked = True
        except Exception:
            locked = False
        checker.check(
            "先确认：运行中的程序会锁住自己的 exe（这就是复制失败的原因）",
            locked and running.poll() is None,
            "锁住=%s 进程存活=%s" % (locked, running.poll() is None),
        )
        started = time.time()
        result = ic.install(
            install_dir=busy_dir,
            create_desktop=False,
            create_startmenu=False,
            desktop_path=desktop,
            startmenu_path=startmenu,
            reg_path=TEST_APP_KEY,
        )
        checker.check(
            "【关键】旧版本正在运行时，安装依然成功（自动关掉旧进程）",
            bool(result.get("ok")),
            json.dumps({k: result.get(k) for k in ("ok", "error", "stopped")}, ensure_ascii=False)[:240],
        )
        checker.check(
            "安装程序报告了被关掉的旧进程",
            bool(result.get("stopped")),
            str(result.get("stopped")),
        )
        checker.check(
            "旧进程确实已经结束",
            wait_until(lambda: running.poll() is not None, seconds=10),
            "退出码=%s" % running.poll(),
        )
        checker.check(
            "旧版本关闭判断得很快（不会让用户干等）",
            (time.time() - started) < 60,
            "耗时 %.1fs" % (time.time() - started),
        )
        checker.check(
            "升级后安装目录里的程序文件是新的且完整",
            (busy_dir / ic.EXE_NAME).is_file() and (busy_dir / ic.BOT_EXE_NAME).is_file(),
            str(sorted(p.name for p in busy_dir.iterdir())[:8]),
        )
        try:
            if running.poll() is None:
                running.kill()
        except Exception:
            pass
        run_silent(
            "installer.installer_main",
            ["--silent", "--uninstall", "--dir", str(busy_dir), "--app-key", TEST_APP_KEY],
            env={"BAIAI_PAYLOAD": str(payload)},
        )

        # ------------------------------------------------------ 第四阶段：更新系统（V0.2）
        checker.phase("4/5 更新系统（GitHub Releases 检查 / 下载 / 校验 / 静默安装）")
        run_update_phase(tmp, checker)

        # ------------------------------------------------------ 第五阶段：成品 exe
        installer_exe = ROOT / "dist" / SETUP_EXE_NAME
        if not installer_exe.exists():
            checker.phase("5/5 安装包成品（未构建，跳过）")
            checker.check("安装包尚未构建（先执行 scripts\\build_installer.bat）", True)
            return checker.summary()

        checker.phase("5/5 安装包成品（真实安装包 + 主程序卸载）")
        real_dir = tmp / "real-install"
        real_desktop = tmp / "real-desktop"
        real_startmenu = tmp / "real-startmenu"
        real_desktop.mkdir(parents=True, exist_ok=True)
        real_startmenu.mkdir(parents=True, exist_ok=True)
        # 成品阶段必须用安装包内嵌的真实 payload。源码模式阶段把
        # BAIAI_PAYLOAD（cmd.exe 冒充的小体积假 payload）设进了全局环境变量，
        # 子进程默认会继承——一旦泄漏，装出来的"主程序"就是 cmd.exe 副本，
        # 它的 ``--uninstall --silent`` 什么也不做、直接退出码 0，
        # 卸载逻辑根本没被验证（曾导致"自卸载后注册表项已清理"误报失败）。
        clean_env = {k: v for k, v in os.environ.items() if k != "BAIAI_PAYLOAD"}
        run = subprocess.run(
            [
                str(installer_exe),
                "--silent",
                "--dir",
                str(real_dir),
                "--desktop-dir",
                str(real_desktop),
                "--startmenu-dir",
                str(real_startmenu),
                "--app-key",
                TEST_APP_KEY,
                "--no-run",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            creationflags=0x08000000 if os.name == "nt" else 0,
            env=clean_env,
        )
        checker.check("成品安装包可以静默安装", run.returncode == 0, (run.stdout or "")[-300:] + (run.stderr or "")[-200:])
        checker.check(
            "成品安装后主程序与 Bot 都在（不再有独立卸载程序）",
            (real_dir / ic.EXE_NAME).is_file()
            and (real_dir / ic.BOT_EXE_NAME).is_file()
            and not (real_dir / ic.LEGACY_UNINSTALLER_NAME).exists()
            # 真实主程序是几十 MB 的 PyInstaller 单文件；假 payload 的
            # cmd.exe 冒充体只有几百 KB——体积对不上说明装的不是真程序
            and (real_dir / ic.EXE_NAME).stat().st_size > 1_000_000,
            str(sorted(p.name for p in real_dir.iterdir())[:10]) if real_dir.exists() else "（目录不存在）",
        )
        real_lnk = real_desktop / ("%s.lnk" % ic.APP_NAME)
        checker.check(
            "成品安装后桌面快捷方式可用且指向主程序",
            real_lnk.is_file() and ic.lnk_target(real_lnk).lower() == str(real_dir / ic.EXE_NAME).lower(),
            "读回=%s" % (ic.lnk_target(real_lnk) or "空"),
        )
        entry = ic.read_uninstall_entry(TEST_APP_KEY)
        if not entry:
            # 便于定位：两个注册表视图各是什么情况
            print("  [诊断] key=%s" % TEST_APP_KEY)
            print("  [诊断] winreg 视图=%s" % ic.read_uninstall_entry_winreg(TEST_APP_KEY))
            print("  [诊断] reg.exe 视图=%s" % ic.reg_exe_read(TEST_APP_KEY))
        checker.check(
            "成品安装写入的卸载信息版本/作者正确",
            entry.get("DisplayVersion") == ic.APP_VERSION and entry.get("Publisher") == ic.APP_AUTHOR,
            json.dumps(
                {k: entry.get(k) for k in ("DisplayName", "DisplayVersion", "Publisher", "EstimatedSize")},
                ensure_ascii=False,
            ),
        )
        checker.check(
            "成品安装包的图形界面能打开（tkinter 打进去了，不是一闪就退）",
            gui_alive(installer_exe, env=clean_env),
            "安装包：%s" % installer_exe.name,
        )

        # 用「安装目录里的主程序 --uninstall」卸载（就是「设置 → 应用」会调用的命令）
        (real_dir / "data").mkdir(parents=True, exist_ok=True)
        (real_dir / "data" / "config.yaml").write_text("# keep me\n", encoding="utf-8")
        run = subprocess.run(
            [str(real_dir / ic.EXE_NAME), "--uninstall", "--silent"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=0x08000000 if os.name == "nt" else 0,
            # 主程序卸载时固定用默认注册表项；自检通过环境变量把它重定向到临时项，
            # 避免动到机器上真实的安装信息
            env={**clean_env, "BAIAI_UNINSTALL_KEY": TEST_APP_KEY},
        )
        checker.check("主程序可以静默卸载自己", run.returncode == 0, (run.stdout or "")[-300:])
        checker.check("主程序自卸载后注册表项已清理", not ic.read_uninstall_entry(TEST_APP_KEY), "")
        checker.check(
            "主程序自卸载后自己被删掉、用户数据保留（目录里只剩 data）",
            wait_until(
                lambda: not (real_dir / ic.EXE_NAME).exists()
                and not (real_dir / ic.BOT_EXE_NAME).exists()
                and (real_dir / "data" / "config.yaml").is_file()
            ),
            str(sorted(p.name for p in real_dir.iterdir())) if real_dir.exists() else "（目录已删）",
        )

        return checker.summary()
    except Exception as exc:  # pragma: no cover - 自检自身异常
        import traceback

        traceback.print_exc()
        checker.check("安装程序自检未抛出异常", False, str(exc))
        return 1
    finally:
        ic.remove_uninstall_entry(TEST_APP_KEY)
        if checker.failed and os.environ.get("BAIAI_KEEP_TESTDIR"):
            print("（BAIAI_KEEP_TESTDIR 已设置，保留临时目录：%s）" % tmp)
        else:
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
