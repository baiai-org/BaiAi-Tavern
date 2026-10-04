"""LLM 客户端：OpenAI SDK 兼容接口（默认 DeepSeek）。

* 失败自动重试（指数退避），全部失败抛 :class:`LLMError`；
* 配置变更后自动重建客户端；
* 提供 :meth:`LLMClient.test_connection` 供 GUI 的“测试连接”按钮使用。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Sequence

from common.logging_setup import get_logger

log = get_logger("bot.ai_engine.llm")

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - 依赖缺失时给出明确提示
    AsyncOpenAI = None  # type: ignore


class LLMError(RuntimeError):
    """LLM 调用失败（网络、鉴权、超时、返回为空等）。"""


class LLMClient:
    def __init__(
        self,
        base_url: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        model: str = "deepseek-chat",
        max_tokens: int = 500,
        temperature: float = 0.85,
        top_p: float = 1.0,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._client: Optional[Any] = None
        self._signature: Optional[tuple] = None
        self.last_error: str = ""
        self.last_usage: Dict[str, Any] = {}

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_config(cls, config: Any) -> "LLMClient":
        return cls(
            base_url=str(config.get("llm.base_url", "https://api.deepseek.com/v1") or ""),
            api_key=str(config.get("llm.api_key", "") or ""),
            model=str(config.get("llm.model", "deepseek-chat") or "deepseek-chat"),
            max_tokens=int(config.get("llm.max_tokens", 500) or 500),
            temperature=float(config.get("llm.temperature", 0.85) or 0.85),
            top_p=float(config.get("llm.top_p", 1.0) or 1.0),
            timeout=float(config.get("llm.timeout", 60) or 60),
            max_retries=int(config.get("llm.max_retries", 2) or 0),
        )

    def apply_config(self, config: Any) -> None:
        """配置热重载时同步参数。"""
        self.base_url = str(config.get("llm.base_url", self.base_url) or self.base_url)
        self.api_key = str(config.get("llm.api_key", self.api_key) or "")
        self.model = str(config.get("llm.model", self.model) or self.model)
        self.max_tokens = int(config.get("llm.max_tokens", self.max_tokens) or self.max_tokens)
        self.temperature = float(config.get("llm.temperature", self.temperature) or self.temperature)
        self.top_p = float(config.get("llm.top_p", self.top_p) or self.top_p)
        self.timeout = float(config.get("llm.timeout", self.timeout) or self.timeout)
        self.max_retries = max(0, int(config.get("llm.max_retries", self.max_retries) or 0))

    def configured(self) -> bool:
        return bool(self.api_key.strip()) and bool(self.base_url.strip())

    # ---------------------------------------------------------------- 客户端
    def _ensure_client(self) -> Any:
        signature = (
            self.base_url,
            self.api_key,
            self.timeout,
            self.model,
        )
        if self._client is not None and self._signature == signature:
            return self._client
        if AsyncOpenAI is None:  # pragma: no cover
            raise LLMError("缺少 openai 依赖，请执行 pip install -r requirements.txt")
        if not self.configured():
            raise LLMError("尚未配置 LLM 的 base_url / api_key")
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,  # 重试由本类统一控制
        )
        self._signature = signature
        return self._client

    async def close(self) -> None:
        client, self._client, self._signature = self._client, None, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 调用
    async def chat(
        self,
        messages: Sequence[Dict[str, str]],
        **overrides: Any
    ) -> str:
        """调用 chat.completions，返回去除首尾空白的文本。"""
        if not messages:
            raise LLMError("messages 不能为空")
        if not self.configured():
            raise LLMError("尚未配置 LLM 的 base_url / api_key，请在“系统设置”中填写")

        payload: Dict[str, Any] = {
            "model": overrides.get("model") or self.model,
            "messages": [dict(item) for item in messages],
            "max_tokens": int(overrides.get("max_tokens") or self.max_tokens),
            "temperature": float(
                self.temperature if overrides.get("temperature") is None else overrides["temperature"]
            ),
            "top_p": float(self.top_p if overrides.get("top_p") is None else overrides["top_p"]),
        }
        if overrides.get("stop"):
            payload["stop"] = overrides["stop"]

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                client = self._ensure_client()
                response = await client.chat.completions.create(**payload)
                content = self._extract_content(response)
                if content:
                    self.last_error = ""
                    self.last_usage = self._extract_usage(response)
                    return content
                last_error = "模型返回了空内容"
            except LLMError:
                raise
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
                # 鉴权类错误重试无意义
                if any(token in last_error for token in ("401", "403", "invalid_api_key", "Unauthorized")):
                    self.last_error = last_error
                    raise LLMError("鉴权失败：%s" % last_error) from exc
            if attempt < self.max_retries:
                delay = 1.0 * (2 ** attempt)
                log.warning("LLM 调用失败（第 %d 次），%.1fs 后重试：%s", attempt + 1, delay, last_error)
                await asyncio.sleep(delay)

        self.last_error = last_error
        raise LLMError("LLM 调用失败（已重试 %d 次）：%s" % (self.max_retries, last_error))

    @staticmethod
    def _extract_content(response: Any) -> str:
        try:
            choices = getattr(response, "choices", None) or []
            if not choices:
                return ""
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if isinstance(content, list):  # 部分兼容实现返回分段内容
                parts = []
                for item in content:
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
                    elif isinstance(item, dict) and item.get("text"):
                        parts.append(str(item["text"]))
                content = "".join(parts)
            return str(content or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        try:
            return {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
        except Exception:
            return {}

    # ------------------------------------------------------------ 连通性测试
    async def test_connection(self) -> Dict[str, Any]:
        """GUI“测试连接”按钮使用。返回 ``{ok, model, reply, error}``。"""
        try:
            reply = await self.chat(
                [
                    {"role": "system", "content": "你是一个测试助手。"},
                    {"role": "user", "content": "只回复两个字：正常"},
                ],
                max_tokens=16,
                temperature=0.0,
            )
            return {"ok": True, "model": self.model, "reply": reply, "error": ""}
        except LLMError as exc:
            return {"ok": False, "model": self.model, "reply": "", "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "model": self.model, "reply": "", "error": str(exc)}
