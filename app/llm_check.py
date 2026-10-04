"""GUI 侧直接访问 LLM 端点（不依赖 Bot 进程）。

配置引导与系统设置里需要：
* 当场验证 API Key 是否可用（``test_llm``）；
* 从上游拉取可用模型列表（``fetch_models``），避免让用户手填模型名。

此时 Bot 进程可能还在启动中，所以这里用 httpx 直接请求 OpenAI 兼容接口，
并把常见失败原因翻译成可读提示。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import httpx

from common.logging_setup import get_logger
from common.utils import truncate

log = get_logger("app.llm_check")

PROBE_PROMPT = "只回复两个字：正常"

# 明显不是聊天模型的名称片段（拉取列表时过滤掉，避免用户选错）
NON_CHAT_KEYWORDS = (
    "embed",
    "embedding",
    "rerank",
    "bge",
    "gte-",
    "whisper",
    "tts",
    "audio",
    "speech",
    "moderation",
    "dall-e",
    "image",
    "stable-diffusion",
    "clip",
    "codex",
    "davinci",
    "transcribe",
)


def _candidate_endpoints(base_url: str, suffix: str) -> List[str]:
    """兼容不同写法：带 /v1、不带 /v1、以及已经写到具体路径的情况。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    if base.endswith(suffix):
        return [base]
    candidates = [base + suffix]
    if not base.endswith("/v1"):
        candidates.append(base + "/v1" + suffix)
    result: List[str] = []
    for item in candidates:
        if item not in result:
            result.append(item)
    return result


def _headers(api_key: str) -> Dict[str, str]:
    key = (api_key or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    return headers


def _error_for_status(status_code: int, text: str = "", key_empty: bool = False) -> Tuple[bool, str]:
    """返回 (是否应该继续尝试下一个候选地址, 提示信息)。"""
    if status_code in (401, 403):
        if key_empty:
            return False, "鉴权失败（HTTP %d）：尚未填写 API Key（本地部署的服务可留空）" % status_code
        return False, "鉴权失败（HTTP %d），API Key 可能不正确" % status_code
    if status_code == 402:
        return False, "账户余额不足（HTTP 402）"
    if status_code == 404:
        return True, "接口不存在（HTTP 404），Base URL 可能缺少 /v1"
    if status_code == 429:
        return False, "请求过于频繁（HTTP 429），稍后再试"
    if status_code >= 400:
        return True, "服务返回 HTTP %d：%s" % (status_code, truncate(text.replace("\n", " "), 140))
    return False, ""


def _extract_reply(payload: Any) -> str:
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        return str(content or "")
    except Exception:
        return ""


def _extract_model_ids(payload: Any) -> List[str]:
    """兼容 ``{"data": [...]}`` / ``{"models": [...]}`` / 纯数组三种返回。"""
    items: Any = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "result"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        return []

    ids: List[str] = []
    for item in items:
        value = ""
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            for key in ("id", "name", "model"):
                if isinstance(item.get(key), str) and item[key].strip():
                    value = item[key].strip()
                    break
        if value and value not in ids:
            ids.append(value)
    return ids


def filter_chat_models(models: List[str]) -> List[str]:
    """过滤掉向量/语音/画图等非聊天模型。"""
    result = []
    for name in models:
        lowered = name.lower()
        if any(keyword in lowered for keyword in NON_CHAT_KEYWORDS):
            continue
        result.append(name)
    return sorted(result, key=lambda item: item.lower())


def test_llm(
    base_url: str,
    api_key: str,
    model: str = "deepseek-chat",
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """发一次最小对话请求，返回 ``{ok, message, reply, endpoint}``。"""
    urls = _candidate_endpoints(base_url, "/chat/completions")
    if not urls:
        return {"ok": False, "message": "请先填写 Base URL", "reply": "", "endpoint": ""}
    if not (model or "").strip():
        return {"ok": False, "message": "请先选择或填写模型名称", "reply": "", "endpoint": ""}

    body = {
        "model": model.strip(),
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": 16,
        "temperature": 0,
    }
    headers = _headers(api_key)

    last_error = ""
    for url in urls:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=body, headers=headers)
        except httpx.ConnectError as exc:
            return {
                "ok": False,
                "message": "无法连接该地址，请检查网络或 Base URL（%s）" % exc,
                "reply": "",
                "endpoint": url,
            }
        except httpx.TimeoutException:
            last_error = "请求超时（%ds），请检查网络或稍后重试" % int(timeout)
            continue
        except Exception as exc:
            last_error = "请求失败：%s" % exc
            continue

        if response.status_code >= 400:
            keep_going, message = _error_for_status(
                response.status_code, response.text, key_empty=not (api_key or "").strip()
            )
            if not keep_going:
                return {"ok": False, "message": message, "reply": "", "endpoint": url}
            last_error = message
            continue

        try:
            payload = response.json()
        except Exception:
            last_error = "返回内容不是 JSON，可能不是 OpenAI 兼容接口"
            continue

        if isinstance(payload, dict) and payload.get("error"):
            return {
                "ok": False,
                "message": "服务返回错误：%s" % truncate(str(payload.get("error")), 120),
                "reply": "",
                "endpoint": url,
            }

        reply = _extract_reply(payload)
        return {
            "ok": True,
            "message": "连接成功（%s 返回：%s）" % (model.strip(), truncate(reply, 20) or "空回复"),
            "reply": reply,
            "endpoint": url,
        }

    return {"ok": False, "message": last_error or "测试失败", "reply": "", "endpoint": urls[0]}


def fetch_models(
    base_url: str,
    api_key: str = "",
    timeout: float = 20.0,
    include_non_chat: bool = False,
) -> Dict[str, Any]:
    """从上游拉取模型列表，返回 ``{ok, models, message, endpoint, total}``。"""
    urls = _candidate_endpoints(base_url, "/models")
    if not urls:
        return {"ok": False, "models": [], "message": "请先填写 Base URL", "endpoint": "", "total": 0}

    headers = _headers(api_key)
    last_error = ""
    for url in urls:
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url, headers=headers)
        except httpx.ConnectError as exc:
            return {
                "ok": False,
                "models": [],
                "message": "无法连接该地址（%s）" % exc,
                "endpoint": url,
                "total": 0,
            }
        except httpx.TimeoutException:
            last_error = "请求超时（%ds）" % int(timeout)
            continue
        except Exception as exc:
            last_error = "请求失败：%s" % exc
            continue

        if response.status_code >= 400:
            keep_going, message = _error_for_status(
                response.status_code, response.text, key_empty=not (api_key or "").strip()
            )
            if not keep_going:
                return {"ok": False, "models": [], "message": message, "endpoint": url, "total": 0}
            last_error = message
            continue

        try:
            payload = response.json()
        except Exception:
            last_error = "返回内容不是 JSON，该服务可能不支持 /models"
            continue

        raw = _extract_model_ids(payload)
        if not raw:
            last_error = "服务没有返回任何模型（可能不支持 /models，请手动填写模型名）"
            continue

        models = raw if include_non_chat else filter_chat_models(raw)
        if not models:
            models = raw
        log.info("从 %s 拉取到 %d 个模型（过滤后 %d 个）", url, len(raw), len(models))
        return {
            "ok": True,
            "models": models,
            "message": "获取到 %d 个可用模型" % len(models),
            "endpoint": url,
            "total": len(raw),
        }

    return {"ok": False, "models": [], "message": last_error or "获取模型列表失败", "endpoint": urls[0], "total": 0}


__all__ = ["fetch_models", "filter_chat_models", "test_llm"]
