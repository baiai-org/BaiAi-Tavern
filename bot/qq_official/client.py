"""QQ 官方机器人（QQ 开放平台）访问层。

依据官方文档与腾讯官方 SDK（tencent-connect/botpy）实现：

* 凭证：``POST https://api.bot.qq.com/app/getAppAccessToken``
  ``{"appId":..., "clientSecret":...}`` → ``{"access_token":..., "expires_in":7200}``；
  **失败时 HTTP 仍是 200，必须看响应体里的 ``code``**。
* 调用：请求头 ``Authorization: QQBot <access_token>``，域名默认 ``api.sgroup.qq.com``
  （沙盒为 ``sandbox.api.sgroup.qq.com``）。
* 发消息：``POST /v2/users/{openid}/messages``（单聊）、
  ``POST /v2/groups/{group_openid}/messages``（群聊），
  被动回复需带 ``msg_id`` 与 ``msg_seq``（同一 msg_id + msg_seq 不能重复），有效期 5 分钟；
  不带 msg_id 即为主动消息（需要平台权限与额度）。
* 发送消息要求机器人 gateway 在线。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from common.async_utils import LoopSafeLock
from common.logging_setup import get_logger
from common.text import sanitize_text, split_message
from common.utils import truncate

log = get_logger("bot.qq_official.client")

MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2

# 需要给出可读提示的业务错误码
ERROR_HINTS = {
    100007: "AppID 无效，或机器人状态异常（请检查开放平台的机器人是否已上线）",
    100016: "AppID 或 AppSecret 不正确",
    10004: "AppID 对应的机器人不存在",
    100001: "请求过于频繁，请降低频率后重试",
    11244: "机器人未加入该群，或群聊消息权限不可用",
    11253: "主动消息频次已达上限（官方对主动消息有额度限制）",
    22009: "主动消息到达上限或未开通主动消息权限",
    30402: "需要先开通「主动消息」权限",
    30403: "该用户不在机器人的可触达范围内",
    40054: "不是有效的 msg_id",
    40034: "消息内容为空或过长",
    50054: "被动回复已过期（官方规定 5 分钟内回复）",
}


class OfficialQQError(RuntimeError):
    """官方接口调用失败。"""

    def __init__(self, message: str, code: int = 0, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def describe_error(payload: Any, status: int = 0) -> str:
    if isinstance(payload, dict):
        code = int(payload.get("code") or payload.get("err_code") or 0)
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        hint = ERROR_HINTS.get(code)
        parts = []
        if code:
            parts.append("code=%s" % code)
        if message:
            parts.append(message)
        text = " ".join(parts) or ("HTTP %d" % status)
        if hint:
            text += "（%s）" % hint
        return text
    return "HTTP %d" % status if status else "未知错误"


class OfficialQQClient:
    """凭证管理 + REST 调用。"""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        api_domain: str = "https://api.sgroup.qq.com",
        token_url: str = "https://api.bot.qq.com/app/getAppAccessToken",
        sandbox: bool = False,
        gateway_path: str = "/gateway",
        timeout: float = 20.0,
    ):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.sandbox = bool(sandbox)
        self.api_domain = self._resolve_domain(api_domain, self.sandbox)
        self.token_url = (token_url or "").strip() or "https://api.bot.qq.com/app/getAppAccessToken"
        self.gateway_path = (gateway_path or "/gateway").strip() or "/gateway"
        self.timeout = float(timeout)
        self.last_error = ""
        self.bot_info: Dict[str, Any] = {}

        self._token = ""
        self._token_expire_at = 0.0
        self._client: Optional[httpx.AsyncClient] = None
        # 用 LoopSafeLock：客户端可能在事件循环启动之前就被构造
        # （Python 3.9 的 asyncio.Lock 会在构造时绑定当时的事件循环）
        self._lock = LoopSafeLock()

    @staticmethod
    def _resolve_domain(domain: str, sandbox: bool) -> str:
        text = (domain or "").strip().rstrip("/") or "https://api.sgroup.qq.com"
        if sandbox:
            text = text.replace("https://api.sgroup.qq.com", "https://sandbox.api.sgroup.qq.com")
            text = text.replace("http://api.sgroup.qq.com", "http://sandbox.api.sgroup.qq.com")
        return text

    def configure(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        sandbox: Optional[bool] = None,
        api_domain: Optional[str] = None,
        token_url: Optional[str] = None,
        gateway_path: Optional[str] = None,
    ) -> None:
        changed = False
        if app_id is not None and app_id.strip() != self.app_id:
            self.app_id = app_id.strip()
            changed = True
        if app_secret is not None and app_secret.strip() != self.app_secret:
            self.app_secret = app_secret.strip()
            changed = True
        if sandbox is not None and bool(sandbox) != self.sandbox:
            self.sandbox = bool(sandbox)
            changed = True
        if api_domain:
            resolved = self._resolve_domain(api_domain, self.sandbox)
            if resolved != self.api_domain:
                self.api_domain = resolved
                changed = True
        if token_url and token_url.strip() != self.token_url:
            self.token_url = token_url.strip()
            changed = True
        if gateway_path and gateway_path.strip() and gateway_path.strip() != self.gateway_path:
            self.gateway_path = gateway_path.strip()
            changed = True
        if changed:
            self._token = ""
            self._token_expire_at = 0.0
            self._reset_client()

    # ------------------------------------------------------------------ 基础
    def configured(self) -> bool:
        return bool(self.app_id) and bool(self.app_secret)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _reset_client(self) -> None:
        client, self._client = self._client, None
        if client is None or client.is_closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(client.aclose())
        else:  # pragma: no cover - 没有运行中的循环时无法异步关闭，丢弃即可
            log.debug("没有运行中的事件循环，放弃关闭旧的 HTTP 连接")

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ 凭证
    async def access_token(self, force: bool = False) -> str:
        """拿 access_token（缓存到过期前 60 秒自动刷新）。"""
        if not self.configured():
            raise OfficialQQError("尚未配置 AppID / AppSecret（官方机器人需要这两个凭据）")
        async with self._lock:
            now = time.time()
            if not force and self._token and now < self._token_expire_at - 60:
                return self._token
            try:
                response = await self.client.post(
                    self.token_url,
                    json={"appId": self.app_id, "clientSecret": self.app_secret},
                )
            except Exception as exc:
                raise OfficialQQError("连接凭证接口失败：%s" % exc) from exc
            try:
                payload = response.json()
            except Exception as exc:
                raise OfficialQQError("凭证接口返回异常（HTTP %d）" % response.status_code) from exc
            # 该接口即使失败 HTTP 也是 200，必须看 code
            if isinstance(payload, dict) and payload.get("code"):
                message = describe_error(payload, response.status_code)
                self.last_error = message
                raise OfficialQQError("获取 access_token 失败：%s" % message)
            token = str((payload or {}).get("access_token") or "")
            if not token:
                raise OfficialQQError("凭证接口没有返回 access_token：%s" % truncate(str(payload), 120))
            expires = float((payload or {}).get("expires_in") or 7200)
            self._token = token
            self._token_expire_at = now + expires
            log.info("已获取 QQ 官方机器人 access_token（%ds 后过期）", int(expires))
            return token

    def token_string(self) -> str:
        return "QQBot %s" % self._token

    # ------------------------------------------------------------------ 调用
    async def request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        retries: int = 2,
    ) -> Dict[str, Any]:
        last_error = ""
        for attempt in range(max(0, retries) + 1):
            token = await self.access_token(force=attempt > 0 and bool(last_error) and "401" in last_error)
            headers = {"Authorization": "QQBot %s" % token, "X-Union-Appid": self.app_id}
            try:
                response = await self.client.request(
                    method, self.api_domain + path, headers=headers, json=json
                )
            except Exception as exc:
                last_error = "请求失败：%s" % exc
            else:
                payload: Any = {}
                if response.content:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = response.text[:200]
                if 200 <= response.status_code < 300:
                    self.last_error = ""
                    if isinstance(payload, dict) and payload.get("code"):
                        # 少数接口 200 也带错误码
                        raise OfficialQQError(describe_error(payload, response.status_code))
                    return payload if isinstance(payload, dict) else {"data": payload}
                last_error = describe_error(payload, response.status_code)
                # 主动消息频次/权限类错误重试没有意义
                code = int(payload.get("code") or 0) if isinstance(payload, dict) else 0
                if code in ERROR_HINTS and code not in (100001,):
                    break
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
        self.last_error = last_error
        raise OfficialQQError(last_error or "调用失败")

    async def get_gateway_url(self) -> str:
        data = await self.request("GET", self.gateway_path)
        url = str((data or {}).get("url") or "")
        if not url:
            raise OfficialQQError("网关接口没有返回 url：%s" % truncate(str(data), 120))
        return url

    async def get_bot_info(self) -> Dict[str, Any]:
        data = await self.request("GET", "/users/@me")
        self.bot_info = data or {}
        return self.bot_info

    # ------------------------------------------------------------------ 发送
    async def send_c2c(
        self,
        openid: str,
        content: str,
        msg_id: str = "",
        msg_seq: int = 1,
        markdown: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "content": content,
            "msg_type": MSG_TYPE_MARKDOWN if markdown else MSG_TYPE_TEXT,
        }
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.request("POST", "/v2/users/%s/messages" % openid, json=body)

    async def send_group(
        self,
        group_openid: str,
        content: str,
        msg_id: str = "",
        msg_seq: int = 1,
        markdown: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "content": content,
            "msg_type": MSG_TYPE_MARKDOWN if markdown else MSG_TYPE_TEXT,
        }
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = msg_seq
        return await self.request("POST", "/v2/groups/%s/messages" % group_openid, json=body)

    async def probe(self) -> Dict[str, Any]:
        """连通性探测：凭证 → 机器人信息 → 网关地址。"""
        if not self.configured():
            return {"available": False, "error": "尚未填写 AppID / AppSecret", "user_id": None, "nickname": None}
        try:
            info = await self.get_bot_info()
            await self.get_gateway_url()
        except OfficialQQError as exc:
            return {"available": False, "error": str(exc), "user_id": None, "nickname": None}
        except Exception as exc:  # pragma: no cover
            return {"available": False, "error": str(exc), "user_id": None, "nickname": None}
        return {
            "available": True,
            "error": "",
            "user_id": info.get("id") or info.get("user_id"),
            "nickname": info.get("username") or info.get("nickname"),
            "sandbox": self.sandbox,
        }


def segments_for_official(text: str, max_len: int = 200, max_segments: int = 3) -> List[str]:
    """官方通道的单条上限更宽，但过长的消息依然拆开发。"""
    return split_message(sanitize_text(text), max_len=max_len, max_segments=max_segments)


__all__ = [
    "MSG_TYPE_MARKDOWN",
    "MSG_TYPE_TEXT",
    "OfficialQQClient",
    "OfficialQQError",
    "describe_error",
    "segments_for_official",
]
