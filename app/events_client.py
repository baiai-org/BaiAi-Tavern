"""Bot 事件推送的 WebSocket 客户端（运行在独立线程）。

Bot 侧通过 ``ws://127.0.0.1:<port>/ws/events`` 推送主动消息、配置变更等事件，
GUI 用它在托盘上即时弹通知。若连接失败会自动重连，不影响轮询兜底。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from common.logging_setup import get_logger

from .qt_safe import qt_safe

log = get_logger("app.events")

try:
    from websockets.sync.client import connect as ws_connect  # type: ignore
except Exception:  # pragma: no cover
    ws_connect = None  # type: ignore


class EventStream(QThread):
    event_received = Signal(object)
    connection_changed = Signal(bool)

    def __init__(self, url: str, token: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.token = token
        self._stop = False
        self._connected = False

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()
        self.wait(3000)

    # ---------------------------------------------------------------- 内部
    def _set_connected(self, value: bool) -> None:
        if value != self._connected:
            self._connected = value
            self.connection_changed.emit(value)

    def _listen(self) -> None:
        if ws_connect is None:
            raise RuntimeError("缺少 websockets 依赖")
        extra_headers = {"X-Tavern-Token": self.token} if self.token else None
        try:
            with ws_connect(self.url, open_timeout=5, additional_headers=extra_headers) as socket:
                self._set_connected(True)
                while not self._stop:
                    try:
                        raw = socket.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    if raw is None:
                        break
                    try:
                        import json

                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        self.event_received.emit(qt_safe(payload))
        finally:
            self._set_connected(False)

    def run(self) -> None:  # pragma: no cover - 线程中执行
        delay = 1.0
        while not self._stop:
            try:
                self._listen()
                delay = 1.0
            except Exception as exc:
                log.debug("事件推送连接失败: %s", exc)
                self._set_connected(False)
            if self._stop:
                break
            self.msleep(int(delay * 1000))
            delay = min(delay * 1.8, 15.0)


__all__ = ["EventStream"]
