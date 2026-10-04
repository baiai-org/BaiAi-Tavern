"""应用上下文：把配置、API 客户端、线程池、子进程和事件流串起来。

页面（pages/*）只依赖这个对象，方便统一处理轮询、通知与状态同步。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal

from common.logging_setup import get_logger
from common.utils import iso_now

from .api_client import ApiClient, ApiError
from .bot_process import BotProcess
from .events_client import EventStream
from .qt_safe import qt_safe
from .workers import Poller, TaskRunner

log = get_logger("app.context")


class AppContext(QObject):
    status_updated = Signal(object)
    event_received = Signal(object)
    notify = Signal(str, str, str)  # title, message, level(info|warn|error|message)
    bot_state_changed = Signal(bool)
    process_event = Signal(str, str)  # name, state(running|stopped|failed)

    def __init__(self, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self.runner = TaskRunner(self)
        self.api = ApiClient(self.api_base_url(), self.api_token())
        self.bot = BotProcess(config, self)

        self.last_status: Dict[str, Any] = {}
        self.online: bool = False
        self._snapshot_error_count = 0
        self._last_proactive_id: Optional[int] = None
        self._bot_exit_notified = False

        self.status_poller = Poller(
            self.runner, self._fetch_status, interval_ms=3000, label="status_poll", parent=self
        )
        self.status_poller.tick.connect(self._on_status)
        self.status_poller.error.connect(self._on_status_error)

        self.event_stream: Optional[EventStream] = None
        self.events_connected = False

        self.bot.started.connect(lambda name: self.process_event.emit(name, "running"))
        self.bot.failed.connect(lambda name, err: self.process_event.emit(name, "failed:%s" % err))

    # ============================================================== 配置同步
    def api_base_url(self) -> str:
        host = str(self.config.get("api.host", "127.0.0.1") or "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        port = int(self.config.get("api.port", 8765) or 8765)
        return "http://%s:%d" % (host, port)

    def api_token(self) -> str:
        return str(self.config.get("api.token", "") or "")

    def ws_url(self) -> str:
        return self.api_base_url().replace("http://", "ws://").replace("https://", "wss://") + "/ws/events"

    def sync_endpoints(self) -> None:
        """配置变化后刷新 API 地址与 token。"""
        self.api.configure(self.api_base_url(), self.api_token())
        if self.event_stream is not None and self.event_stream.isRunning():
            self.restart_event_stream()

    def reload_config(self) -> None:
        self.config.load(force=True)
        self.sync_endpoints()

    # ============================================================== 生命周期
    def start(self) -> None:
        self.status_poller.start(immediate=True)
        self.start_event_stream()

    def start_event_stream(self) -> None:
        if self.event_stream is not None and self.event_stream.isRunning():
            return
        self.event_stream = EventStream(self.ws_url(), self.api_token(), self)
        self.event_stream.event_received.connect(self._on_ws_event)
        self.event_stream.connection_changed.connect(self._on_ws_connection)
        self.event_stream.start()

    def restart_event_stream(self) -> None:
        if self.event_stream is not None:
            try:
                self.event_stream.stop()
            except Exception:
                pass
            self.event_stream = None
        self.start_event_stream()

    def shutdown(self) -> None:
        try:
            self.status_poller.stop()
        except Exception:
            pass
        if self.event_stream is not None:
            try:
                self.event_stream.stop()
            except Exception:
                pass
            self.event_stream = None
        self.runner.shutdown()
        self.api.close()

    # ============================================================== 状态轮询
    def _fetch_status(self) -> Dict[str, Any]:
        return self.api.status()

    def _on_status(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            return
        self._snapshot_error_count = 0
        snapshot.setdefault("offline", False)
        previous_online = self.online
        self.online = bool(snapshot.get("online"))
        self.last_status = snapshot

        proactive = snapshot.get("last_proactive") or {}
        current_id = proactive.get("id")
        if current_id and current_id != self._last_proactive_id:
            if self._last_proactive_id is not None:
                self.chat_activity(
                    {
                        "kind": "proactive",
                        "character": proactive.get("character_name") or "",
                        "content": proactive.get("content") or "",
                        "trigger": proactive.get("trigger_type") or "",
                    }
                )
            self._last_proactive_id = current_id

        if previous_online != self.online:
            self.bot_state_changed.emit(self.online)
        # 出海前统一清洗：平台 ID 是 20 位数字，直接放进 Signal 会溢出 Qt 的 int64
        self.status_updated.emit(qt_safe(snapshot))

    def _on_status_error(self, message: str) -> None:
        self._snapshot_error_count += 1
        if self._snapshot_error_count in (1, 3, 10):
            log.debug("状态轮询失败(%d): %s", self._snapshot_error_count, message)
        if self._snapshot_error_count >= 2 and self.online:
            self.online = False
            self.bot_state_changed.emit(False)
        snapshot = self.offline_snapshot(message)
        self.last_status = snapshot
        self.status_updated.emit(qt_safe(snapshot))
        self._check_bot_exit()

    def offline_snapshot(self, reason: str = "Bot 未运行") -> Dict[str, Any]:
        return {
            "online": False,
            "ready": False,
            "offline": True,
            "error": reason,
            "started_at": "",
            "self_id": 0,
            "llm": {
                "model": self.config.get("llm.model", ""),
                "configured": self.config.llm_configured(),
            },
            "characters": {"total": 0, "enabled": 0},
            "today": {"proactive_total": 0, "proactive_by_character": []},
            "proactive": {"running": False, "jobs": [], "last_skip_reason": ""},
            "last_proactive": None,
        }

    def _check_bot_exit(self) -> None:
        code = self.bot.poll()
        if code is None:
            self._bot_exit_notified = False
            return
        if self._bot_exit_notified:
            return
        self._bot_exit_notified = True
        self.process_event.emit(self.bot.name, "stopped")
        if code != 0:
            self.notify.emit(
                "Bot 进程已退出",
                "退出码 %s，请查看日志文件排查原因" % code,
                "error",
            )

    # ============================================================== 事件处理
    def _on_ws_connection(self, connected: bool) -> None:
        self.events_connected = connected
        log.debug("事件推送连接状态: %s", connected)

    def _on_ws_event(self, event: Dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "hello":
            status = event.get("status")
            if isinstance(status, dict):
                self._on_status(status)
            return
        if kind == "proactive_sent":
            self.chat_activity(
                {
                    "kind": "proactive",
                    "character": event.get("character") or "",
                    "content": event.get("content") or "",
                    "trigger": event.get("trigger") or "",
                    "degraded": event.get("degraded"),
                }
            )
        elif kind == "reply_sent":
            self.chat_activity(
                {
                    "kind": "reply",
                    "character": event.get("character") or "",
                    "content": event.get("content") or "",
                    "chat_type": event.get("chat_type"),
                }
            )
        elif kind == "proactive_failed":
            self.notify.emit(
                "主动消息发送失败",
                str(event.get("error") or event.get("reason") or "未知错误"),
                "error",
            )
        elif kind in ("config_reloaded", "characters_changed", "conversations_changed"):
            self.request_status_refresh()
        self.event_received.emit(event)

    def chat_activity(self, payload: Dict[str, Any]) -> None:
        payload.setdefault("at", iso_now())
        self.event_received.emit({"type": "chat_activity", **payload})

    def request_status_refresh(self) -> None:
        if not self.runner.is_busy("status_poll"):
            self.status_poller._fire()

    # ============================================================== 进程操作
    def bot_process_running(self) -> bool:
        return self.bot.is_running()

    def start_bot_process(self) -> bool:
        return self.bot.start()

    def stop_bot_process(self, graceful: bool = True) -> None:
        def _work() -> str:
            if graceful and self.online:
                try:
                    self.api.shutdown()
                except ApiError as exc:
                    log.debug("优雅关闭请求失败（忽略）: %s", exc)
                for _ in range(20):
                    if not self.bot.is_running():
                        return "stopped"
                    import time

                    time.sleep(0.3)
            self.bot.stop(graceful_timeout=6.0)
            return "stopped"

        self.runner.run(
            _work,
            on_ok=lambda _result: self.notify.emit("BaiAi-Tavern", "Bot 已停止", "info"),
            on_error=lambda error: self.notify.emit("BaiAi-Tavern", "停止 Bot 失败：%s" % error, "error"),
            label="stop_bot",
        )

    def restart_bot_process(self) -> None:
        def _work() -> str:
            if self.online:
                try:
                    self.api.shutdown()
                except ApiError:
                    pass
                import time

                for _ in range(20):
                    if not self.bot.is_running():
                        break
                    time.sleep(0.3)
            self.bot.stop(graceful_timeout=5.0)
            import time

            time.sleep(0.5)
            if not self.bot.start():
                raise RuntimeError("无法启动 Bot 进程")
            return "restarted"

        self.runner.run(
            _work,
            on_ok=lambda _result: self.notify.emit("BaiAi-Tavern", "Bot 已重启", "info"),
            on_error=lambda error: self.notify.emit("BaiAi-Tavern", "重启 Bot 失败：%s" % error, "error"),
            label="restart_bot",
        )

    # ============================================================== 工具方法
    def run_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_ok: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        label: str = "",
        key: Optional[str] = None,
        **kwargs: Any
    ) -> bool:
        return self.runner.run(
            fn, *args, on_ok=on_ok, on_error=on_error, label=label, key=key, **kwargs
        )

    def error_handler(self, title: str) -> Callable[[str], None]:
        def _handler(message: str) -> None:
            self.notify.emit(title, message, "error")

        return _handler


__all__ = ["AppContext"]
