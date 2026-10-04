"""子进程管理：Bot 进程（开发模式用 python -m bot.main，打包后用 bot.exe）。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from common.logging_setup import get_logger
from common.paths import app_root, is_frozen, logs_dir

log = get_logger("app.process")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def kill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("taskkill 失败: %s", exc)


class ManagedProcess(QObject):
    """子进程包装：启动、停止、状态查询。"""

    started = Signal(str)
    stopped = Signal(str, int)
    failed = Signal(str, str)

    def __init__(self, name: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.name = name
        self._process: Optional[subprocess.Popen] = None
        self._handle = None  # 重定向用的文件句柄
        self.last_error = ""

    # ---------------------------------------------------------------- 状态
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def pid(self) -> Optional[int]:
        if self.is_running():
            return self._process.pid  # type: ignore[union-attr]
        return None

    # ---------------------------------------------------------------- 生命周期
    def _close_handle(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None

    def _spawn(
        self,
        command: List[str],
        cwd: Path,
        flags: int,
        log_file: Optional[Path] = None,
        interactive: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """启动子进程。

        ``interactive=True`` 用于需要用户输入/看输出的控制台程序：不重定向标准输入，
        让它能使用新建的控制台窗口。``env`` 可以覆盖子进程环境变量。
        """
        self._close_handle()
        stdout = None
        stderr = None
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(log_file, "ab", buffering=0)
                stdout = self._handle
                stderr = self._handle
            except Exception as exc:
                log.warning("无法重定向 %s 输出到文件: %s", self.name, exc)
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=stdout,
                stderr=stderr,
                stdin=None if interactive else subprocess.DEVNULL,
                close_fds=True,  # 不继承任何句柄，避免子进程状态互相污染
                creationflags=flags,
                env=env,
            )
        except Exception as exc:
            # 一定要记下原因：界面上的“启动失败：未知原因”就是因为这里没写 last_error
            self.last_error = "启动失败：%s（执行 %s，工作目录 %s）" % (
                exc,
                command[0] if command else "?",
                cwd,
            )
            self.failed.emit(self.name, self.last_error)
            log.error("启动 %s 失败: %s（命令：%s，工作目录：%s）", self.name, exc, command, cwd)
            self._close_handle()
            return False
        self.last_error = ""
        self.started.emit(self.name)
        log.info("已启动 %s（pid=%s）: %s", self.name, self._process.pid, " ".join(command))
        return True

    def stop(self, graceful_timeout: float = 8.0, force: bool = True) -> bool:
        """阻塞式停止（请在后台线程中调用）。"""
        if not self.is_running():
            self._close_handle()
            return True

        process = self._process
        assert process is not None
        process.terminate()
        deadline = time.time() + max(0.5, graceful_timeout)
        while time.time() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.2)

        if process.poll() is None and force:
            log.warning("%s 未在 %.1fs 内退出，强制结束进程树", self.name, graceful_timeout)
            kill_process_tree(process.pid)
            try:
                process.wait(timeout=8)
            except Exception:
                pass

        code = process.poll() or 0
        self._process = None
        self._close_handle()
        self.stopped.emit(self.name, int(code))
        log.info("%s 已停止（退出码 %s）", self.name, code)
        return True

    def poll(self) -> Optional[int]:
        """返回退出码（若已退出），用于发现异常退出。"""
        if self._process is None:
            return None
        code = self._process.poll()
        if code is not None:
            self._process = None
            self._close_handle()
            return int(code)
        return None


class BotProcess(ManagedProcess):
    """Bot 进程（开发模式用 python -m bot.main，打包后用 bot.exe）。"""

    def __init__(self, config, parent: Optional[QObject] = None):
        super().__init__("Bot 进程", parent)
        self.config = config

    def executable(self) -> Optional[str]:
        exe = app_root() / "bot.exe"
        if exe.exists():
            return str(exe)
        return None

    def command(self) -> Optional[List[str]]:
        exe = self.executable()
        if exe:
            return [exe]
        if is_frozen():
            return None
        return [sys.executable, "-m", "bot.main", "--no-console"]

    def console_log(self) -> Path:
        return logs_dir() / "bot_stdout.log"

    def start(self) -> bool:
        if self.is_running():
            return True
        command = self.command()
        if not command:
            self.failed.emit(self.name, "未找到 bot.exe，也无法在当前环境启动 Bot")
            return False
        return self._spawn(
            command,
            cwd=app_root(),
            flags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            log_file=self.console_log(),
        )


__all__ = ["BotProcess", "ManagedProcess", "kill_process_tree"]
