"""文本清理与拆分（发送前统一处理 LLM 输出）。

官方机器人通道单条消息有长度上限，而模型经常一次吐出一大段；
这里把「清理」与「按标点拆分」两件事独立出来，供官方发送层与自检共用。
"""

from __future__ import annotations

import re
from typing import List

from .utils import jitter

# 模型偶尔会把控制字符或“工具调用残留”混进正文，先清掉
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CQ_PATTERN = re.compile(r"\[CQ:", re.IGNORECASE)
# 角色卡（Chub 等）的开场白常带 Markdown 图片链接，QQ 纯文本通道里是噪音
_MD_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((?:https?://[^\s)]+|data:[^\s)]+)\)")
_PREFIX_PATTERNS = [
    re.compile(r"^\s*(?:assistant|system|user)\s*[:：]\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:回复|回答|消息|输出)\s*[:：]\s*"),
]
_FENCE_PATTERN = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

__all__ = ["sanitize_text", "split_message", "typing_delay"]


def sanitize_text(text: str, character_name: str = "") -> str:
    """清理 LLM 输出：去掉角色名前缀、代码围栏、多余引号与控制字符。"""
    text = str(text or "")
    text = _CONTROL_PATTERN.sub("", text)
    text = _FENCE_PATTERN.sub("", text.strip())
    if character_name:
        pattern = re.compile(r"^\s*[【\[(]?\s*%s\s*[】\])]?\s*[:：]\s*" % re.escape(character_name))
        text = pattern.sub("", text)
    for pattern in _PREFIX_PATTERNS:
        text = pattern.sub("", text)
    text = text.strip()
    # 模型有时会把整句话包在引号里
    for left, right in (('"', '"'), ("“", "”"), ("'", "'"), ("「", "」")):
        if len(text) > 1 and text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
    # 方括号里的 CQ 码是旧版 OneBot 的语法，转义掉避免被当成指令
    text = _CQ_PATTERN.sub("［CQ:", text)
    # 开场白/回复里的 Markdown 图片（角色卡自带）在 QQ 纯文本里只是链接噪音
    text = _MD_IMAGE_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, max_len: int = 200, max_segments: int = 3) -> List[str]:
    """把长文本按标点拆成多条消息。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_segments <= 1 or len(text) <= max_len:
        return [text]

    breakers = "。！？…～!?;\n"
    soft_breakers = "，,、 ）)】]"
    parts: List[str] = []
    remaining = text
    while remaining and len(parts) < max_segments - 1:
        if len(remaining) <= max_len:
            break
        window = remaining[:max_len]
        cut = max((window.rfind(char) for char in breakers), default=-1)
        if cut < max_len // 2:
            cut = max(cut, max((window.rfind(char) for char in soft_breakers), default=-1))
        if cut <= 0:
            cut = max_len - 1
        parts.append(remaining[: cut + 1].strip())
        remaining = remaining[cut + 1 :].strip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def typing_delay(text: str, cps: float = 12.0, max_delay: float = 8.0, enabled: bool = True) -> float:
    """按“打字速度”估算延迟秒数（带随机抖动，但不会超过上限）。"""
    if not enabled:
        return 0.0
    cps = max(1.0, float(cps or 12.0))
    ceiling = float(max_delay or 8.0)
    if ceiling <= 0:
        return 0.0
    delay = min(ceiling, len(text or "") / cps)
    return max(0.0, min(ceiling, jitter(delay, ratio=0.25)))
