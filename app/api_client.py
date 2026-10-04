"""GUI → Bot 进程的 HTTP 客户端。

所有方法都是同步的，调用方应放到 :class:`app.workers.TaskRunner` 的线程池里执行，
避免阻塞 Qt 事件循环。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from common.logging_setup import get_logger

log = get_logger("app.api_client")


class ApiError(RuntimeError):
    """API 调用失败。"""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        token: str = "",
        timeout: float = 8.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self._client: Optional[httpx.Client] = None

    # ---------------------------------------------------------------- 基础设施
    def configure(
        self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: Optional[float] = None
    ) -> None:
        if base_url and base_url.rstrip("/") != self.base_url:
            self.base_url = base_url.rstrip("/")
            self.close()
        if token is not None:
            self.token = token
        if timeout is not None:
            self.timeout = float(timeout)

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.token:
                headers["X-Tavern-Token"] = self.token
            self._client = httpx.Client(
                base_url=self.base_url, headers=headers, timeout=self.timeout
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        kwargs: Dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params:
            kwargs["params"] = params
        if files is not None:
            kwargs["files"] = files
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise ApiError("无法连接到 Bot 进程（%s），请确认 Bot 已启动" % self.base_url) from exc
        except httpx.TimeoutException as exc:
            raise ApiError("请求超时：%s" % exc) from exc
        except Exception as exc:
            raise ApiError("请求失败：%s" % exc) from exc

        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = str(payload.get("detail") or payload.get("message") or "")
            except Exception:
                detail = response.text[:200]
            raise ApiError(detail or ("HTTP %d" % response.status_code), response.status_code)

        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.content

    # ---------------------------------------------------------------- 状态
    def health(self) -> Dict[str, Any]:
        # 超时放宽到 6 秒：Bot 进程刚 bind 端口时可能还在初始化，
        # 过早中断连接会影响服务端监听（Windows proactor 的已知行为）。
        return self._request("GET", "/api/health", timeout=6.0)

    def status(self) -> Dict[str, Any]:
        return self._request("GET", "/api/status")

    def stats_today(self) -> Dict[str, Any]:
        return self._request("GET", "/api/stats/today")

    def logs(self, source: str = "bot", lines: int = 200) -> Dict[str, Any]:
        return self._request("GET", "/api/logs", params={"source": source, "lines": lines})

    # ---------------------------------------------------------------- Bot
    def bot_start(self) -> Dict[str, Any]:
        return self._request("POST", "/api/bot/start")

    def bot_stop(self) -> Dict[str, Any]:
        return self._request("POST", "/api/bot/stop")

    def bot_restart(self) -> Dict[str, Any]:
        return self._request("POST", "/api/bot/restart")

    def shutdown(self) -> Dict[str, Any]:
        return self._request("POST", "/api/shutdown", timeout=3.0)

    # ------------------------------------------------------------ 机器人管理
    def bots(self) -> Dict[str, Any]:
        return self._request("GET", "/api/bots", timeout=20.0)

    def create_bot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/bots", json=payload or {}, timeout=30.0)

    def update_bot(self, bot_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/api/bots/%s" % bot_id, json=values, timeout=30.0)

    def delete_bot(self, bot_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/api/bots/%s" % bot_id, timeout=30.0)

    def test_bot(self, bot_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/bots/%s/test" % bot_id, timeout=45.0)

    def reconnect_bot(self, bot_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/bots/%s/reconnect" % bot_id, timeout=45.0)

    def forget_bot_openid(self, bot_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/bots/%s/forget-openid" % bot_id, timeout=15.0)

    # ------------------------------------------------------------ QQ 连接状态
    def qq_status(self, bot_id: str = "") -> Dict[str, Any]:
        params = {"bot_id": bot_id} if bot_id else None
        return self._request("GET", "/api/qq/status", params=params, timeout=12.0)

    def qq_test(self, bot_id: str = "") -> Dict[str, Any]:
        """测试 QQ 官方机器人连接（凭证 + 机器人信息 + 网关）。"""
        params = {"bot_id": bot_id} if bot_id else None
        return self._request("POST", "/api/qq/test", params=params, timeout=45.0)

    def qq_reconnect(self, bot_id: str = "") -> Dict[str, Any]:
        params = {"bot_id": bot_id} if bot_id else None
        return self._request("POST", "/api/qq/reconnect", params=params, timeout=45.0)

    def qq_forget_openid(self, bot_id: str = "") -> Dict[str, Any]:
        params = {"bot_id": bot_id} if bot_id else None
        return self._request("POST", "/api/qq/forget-openid", params=params, timeout=15.0)

    def llm_test(self) -> Dict[str, Any]:
        return self._request("POST", "/api/llm/test", timeout=60.0)

    # ---------------------------------------------------------------- 角色
    def characters(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/characters", params={"enabled_only": enabled_only})

    def character(self, character_id: str) -> Dict[str, Any]:
        return self._request("GET", "/api/characters/%s" % character_id)

    def import_character(self, path: Path, overwrite: bool = True) -> Dict[str, Any]:
        path = Path(path)
        data = path.read_bytes()
        return self._request(
            "POST",
            "/api/characters/import",
            params={"overwrite": overwrite},
            files={"file": (path.name, data, "application/octet-stream")},
            timeout=60.0,
        )

    def import_character_path(self, path: str, overwrite: bool = True) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/characters/import-path",
            params={"overwrite": overwrite},
            json={"path": path},
            timeout=120.0,
        )

    def scan_characters(self) -> Dict[str, Any]:
        return self._request("POST", "/api/characters/scan", timeout=120.0)

    def create_character(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """不依赖角色卡，直接用界面填写的内容创建角色。"""
        return self._request("POST", "/api/characters", json=fields, timeout=30.0)

    def import_builtin_characters(self) -> Dict[str, Any]:
        return self._request("POST", "/api/characters/import-builtin", timeout=60.0)

    def update_character(self, character_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/api/characters/%s" % character_id, json=fields)

    def set_character_enabled(self, character_id: str, enabled: bool) -> Dict[str, Any]:
        return self._request(
            "PATCH", "/api/characters/%s/enabled" % character_id, json={"enabled": enabled}
        )

    def delete_character(self, character_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/api/characters/%s" % character_id)

    def character_avatar(self, character_id: str) -> Optional[bytes]:
        try:
            data = self._request("GET", "/api/characters/%s/avatar" % character_id, timeout=15.0)
        except ApiError:
            return None
        return data if isinstance(data, bytes) else None

    # ---------------------------------------------------------------- 对话
    def conversations(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/conversations")

    def messages(self, character_id: str, limit: int = 200) -> Dict[str, Any]:
        return self._request(
            "GET", "/api/conversations/%s/messages" % character_id, params={"limit": limit}
        )

    def clear_messages(self, character_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/api/conversations/%s/messages" % character_id)

    def memories(self, character_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/conversations/%s/memories" % character_id)

    def add_memory(self, character_id: str, content: str, weight: float = 1.0) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/conversations/%s/memories" % character_id,
            json={"content": content, "weight": weight},
        )

    def delete_memory(self, memory_id: int) -> Dict[str, Any]:
        return self._request("DELETE", "/api/memories/%d" % int(memory_id))

    # ---------------------------------------------------------------- 配置
    def get_config(self) -> Dict[str, Any]:
        return self._request("GET", "/api/config")

    def put_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/api/config", json=patch, timeout=20.0)

    def reload_config(self) -> Dict[str, Any]:
        return self._request("POST", "/api/config/reload", timeout=20.0)

    # ------------------------------------------------------------ 主动消息
    def proactive_status(self) -> Dict[str, Any]:
        return self._request("GET", "/api/proactive/status")

    def proactive_trigger(
        self, character_id: Optional[str] = None, force: bool = True, bot_id: Optional[str] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"force": force}
        if character_id:
            payload["character_id"] = character_id
        if bot_id:
            payload["bot_id"] = bot_id
        return self._request("POST", "/api/proactive/trigger", json=payload, timeout=180.0)

    def proactive_trigger_for_bot(
        self, bot_id: str, character_id: Optional[str] = None, force: bool = True
    ) -> Dict[str, Any]:
        """让指定的机器人发一条主动消息。"""
        return self.proactive_trigger(character_id=character_id, force=force, bot_id=bot_id)

    def proactive_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/proactive/logs", params={"limit": limit})


__all__ = ["ApiClient", "ApiError"]
