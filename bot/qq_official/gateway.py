"""QQ 官方机器人 WebSocket 网关客户端。

协议（opcode）与腾讯官方 SDK botpy 一致：

===== =============== ============================================
op    名称             说明
===== =============== ============================================
0     Dispatch        服务端推送事件（``t`` 事件名、``s`` 序号、``d`` 数据）
1     Heartbeat       客户端心跳（``d`` 为最近一次序号）
2     Identify        鉴权（token / intents / shard）
6     Resume          断线重连（token / session_id / seq）
7     Reconnect       服务端要求重连
9     Invalid Session 鉴权或 resume 参数无效
10    Hello           连接建立后第一条，含 ``heartbeat_interval``
11    Heartbeat ACK   心跳回应
===== =============== ============================================

发送消息要求网关保持在线，因此这里会在断开时自动重连（带退避），
并把连接状态通过回调交给运行时展示在界面上。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, Optional

from common.logging_setup import get_logger
from common.utils import iso_now, truncate

log = get_logger("bot.qq_official.gateway")

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# 1<<25：群聊与单聊事件（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE 等）
INTENT_GROUP_AND_C2C = 1 << 25
# 频道相关（可选）
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_DIRECT_MESSAGE = 1 << 12

EventHandler = Callable[[Dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[bool, str], None]


class OfficialGateway:
    """长连接网关，断线自动重连。"""

    def __init__(
        self,
        client: Any,
        on_event: EventHandler,
        on_state: Optional[StateHandler] = None,
        intents: int = INTENT_GROUP_AND_C2C,
    ):
        self.client = client
        self.on_event = on_event
        self.on_state = on_state
        self.intents = int(intents) or INTENT_GROUP_AND_C2C

        self.connected = False
        self.session_id = ""
        self.last_seq = 0
        self.last_error = ""
        self.connected_at = ""
        self.event_count = 0
        self.reconnect_count = 0

        self._task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._stop = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="qq-official-gateway")

    async def stop(self) -> None:
        self._stop = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except Exception:
                pass
        self._set_connected(False, "已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "running": self.running,
            "session_id": self.session_id,
            "last_seq": self.last_seq,
            "connected_at": self.connected_at,
            "event_count": self.event_count,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
        }

    def _set_connected(self, value: bool, error: str = "") -> None:
        changed = value != self.connected or (error and error != self.last_error)
        self.connected = value
        if error:
            self.last_error = error
        if value:
            self.connected_at = iso_now()
        if changed and self.on_state is not None:
            try:
                self.on_state(value, self.last_error)
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ 主循环
    async def _run(self) -> None:
        delay = 2.0
        while not self._stop:
            try:
                await self._connect_and_listen()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("官方网关连接异常：%s", exc)
            self._set_connected(False, self.last_error)
            if self._stop:
                break
            self.reconnect_count += 1
            log.info("%.0f 秒后重连官方网关…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, 60.0)

    async def _connect_and_listen(self) -> None:
        import websockets

        url = await self.client.get_gateway_url()
        log.info("连接 QQ 官方网关：%s", url)
        async with websockets.connect(url, ping_interval=None, max_size=8 * 1024 * 1024) as ws:
            self._ws = ws
            heartbeat_task: Optional[asyncio.Task] = None
            try:
                async for raw in ws:
                    if self._stop:
                        break
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    op = int(payload.get("op") or 0)

                    if op == OP_HELLO:
                        interval = float((payload.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
                        await self._send_identify_or_resume(ws)
                        if heartbeat_task is None:
                            heartbeat_task = asyncio.create_task(self._heartbeat(ws, interval))
                        continue

                    if op == OP_HEARTBEAT_ACK or op == OP_RECONNECT:
                        if op == OP_RECONNECT:
                            log.info("服务端要求重连")
                            break
                        continue

                    if op == OP_INVALID_SESSION:
                        log.warning("会话无效，重置并重新鉴权")
                        self.session_id = ""
                        self.last_seq = 0
                        break

                    if op == OP_DISPATCH:
                        seq = int(payload.get("s") or 0)
                        if seq > 0:
                            self.last_seq = seq
                        if not self.connected:
                            self._set_connected(True, "")
                        await self._dispatch(payload)
            finally:
                self._ws = None
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                self.connected = False

    async def _send_identify_or_resume(self, ws: Any) -> None:
        await self.client.access_token()  # 确保 token 已就绪/未过期
        token = self.client.token_string()
        if self.session_id:
            payload = {
                "op": OP_RESUME,
                "d": {"token": token, "session_id": self.session_id, "seq": self.last_seq},
            }
            log.info("尝试恢复官方网关会话（session_id=%s, seq=%s）", self.session_id[:8], self.last_seq)
        else:
            payload = {
                "op": OP_IDENTIFY,
                "d": {
                    "shard": [0, 1],
                    "token": token,
                    "intents": self.intents,
                    "properties": {},
                },
            }
            log.info("向官方网关鉴权（intents=%s）", self.intents)
        await ws.send(json.dumps(payload))

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    async def _dispatch(self, payload: Dict[str, Any]) -> None:
        event_type = str(payload.get("t") or "")
        data = payload.get("d") or {}
        if event_type == "READY":
            self.session_id = str(data.get("session_id") or "")
            user = data.get("user") or {}
            self.client.bot_info = user if isinstance(user, dict) else {}
            log.info(
                "官方机器人已就绪：%s（%s）",
                user.get("username") if isinstance(user, dict) else "?",
                user.get("id") if isinstance(user, dict) else "?",
            )
            self._set_connected(True, "")
        elif event_type == "RESUMED":
            log.info("官方网关会话已恢复")
            self._set_connected(True, "")

        self.event_count += 1
        if event_type and event_type not in ("READY", "RESUMED"):
            log.debug("收到官方事件 %s：%s", event_type, truncate(json.dumps(data, ensure_ascii=False), 200))
        try:
            await self.on_event({"type": event_type, "data": data, "seq": self.last_seq})
        except Exception as exc:  # pragma: no cover - 单个事件失败不影响连接
            log.exception("处理官方事件 %s 失败：%s", event_type, exc)


__all__ = [
    "INTENT_DIRECT_MESSAGE",
    "INTENT_GROUP_AND_C2C",
    "INTENT_GUILD_MESSAGES",
    "INTENT_PUBLIC_GUILD_MESSAGES",
    "OfficialGateway",
]
