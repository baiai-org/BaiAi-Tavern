"""提示词构建。

角色卡中的 ``{{char}}`` / ``{{user}}`` 占位符会被替换为真实名字，
每个角色的提示词完全独立，保证多人格互不干扰。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence

from ..memory.short_term import ShortTermMemory

_WHITESPACE_RE = re.compile(r"[ \t]+\n")

BASE_RULES = """【行为规则】
1. 始终保持角色身份，绝不能承认自己是 AI、语言模型或程序。
2. 输出的是你在 QQ 上发给对方的消息本身，不要输出旁白、动作描写、括号说明、时间地点标注。
3. 不要复述对方的话，不要加“角色名：”之类的前缀，不要使用引号包裹整句话。
4. 语气自然口语化，像真实的人在聊天，可以有一点点情绪和个性。
5. 单条消息尽量简短（不超过 {max_chars} 字），一次只说一件事。"""


def render_placeholders(text: str, character_name: str = "", user_name: str = "你") -> str:
    """替换 SillyTavern 风格的占位符。"""
    if not text:
        return ""
    result = str(text)
    for token in ("{{char}}", "{{Char}}", "<char>", "{{char_name}}"):
        result = result.replace(token, character_name or "我")
    for token in ("{{user}}", "{{User}}", "<user>", "{{user_name}}"):
        result = result.replace(token, user_name or "你")
    return result.strip()


def _clean_block(text: Any) -> str:
    if text is None:
        return ""
    value = str(text).replace("\r\n", "\n").strip()
    return _WHITESPACE_RE.sub("\n", value)


_HTML_TAG_RE = re.compile(r"<[^<>]+>")


def _notes_for_prompt(text: Any) -> str:
    """creator_notes 按 V2 规范默认不进 prompt；这里只在它是短小纯文本时保留
    （内置角色卡用它写一句场景备注）。Chub 卡片会把整张展示页的 HTML 塞进
    creator_notes（可达上万字符），那属于展示内容，发给模型只会稀释人设。"""
    value = _clean_block(text)
    if not value:
        return ""
    if "<" in value and ">" in value:
        plain = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", value)).strip()
        if len(plain) > 300:
            return ""
        value = plain
    return value[:500]


def build_system_prompt(
    character: Mapping[str, Any],
    user_name: str = "你",
    memories_text: str = "",
    extra_instructions: str = "",
    max_chars: int = 120,
    summaries_text: str = "",
) -> str:
    name = str(character.get("name") or "角色")
    sections: List[str] = [
        "你正在扮演以下角色，请完全以该角色的身份、口吻和思维方式说话。",
        "",
        "【角色设定】",
        "姓名：%s" % name,
    ]

    def add(label: str, value: Any) -> None:
        text = _clean_block(render_placeholders(_clean_block(value), name, user_name))
        if text:
            sections.append("%s：%s" % (label, text))

    add("描述", character.get("description"))
    add("性格", character.get("personality"))
    add("场景", character.get("scenario"))
    add("你与对方的关系", _notes_for_prompt(character.get("creator_notes")))

    example = _clean_block(render_placeholders(_clean_block(character.get("mes_example")), name, user_name))
    if example:
        sections.extend(["", "【示例对话】", example[:1500]])

    system_prompt = _clean_block(
        render_placeholders(_clean_block(character.get("system_prompt")), name, user_name)
    )
    if system_prompt:
        sections.extend(["", "【角色专属指令】", system_prompt[:1500]])

    if memories_text:
        sections.extend(["", "【你记得关于对方的事】", memories_text[:1200]])

    if summaries_text:
        # 更早的对话已压缩成摘要（V0.2.2 上下文分级管理）：
        # 原文不再随请求发送，这里只给压缩后的内容
        sections.extend(["", "【更早的对话（已压缩摘要）】", summaries_text])

    if extra_instructions:
        sections.extend(["", _clean_block(extra_instructions)])

    sections.extend(["", BASE_RULES.format(max_chars=max_chars)])
    return "\n".join(sections).strip()


def build_reply_messages(
    character: Mapping[str, Any],
    config: Any,
    history_rows: Sequence[Mapping[str, Any]],
    memory_rows: Sequence[Mapping[str, Any]] = (),
    chat_hint: str = "",
    summaries: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, str]]:
    """被动回复：历史（含最新一条用户消息）直接作为对话上下文。

    ``summaries`` 是更早对话的压缩摘要（V0.2.2）：原文不进请求，摘要进系统提示词。
    """
    user_name = str(config.get("qq.user_nickname", "你") or "你")
    max_chars = int(config.get("proactive.max_message_chars", 120) or 120)
    memories_text = _format_memories(memory_rows)
    extra = chat_hint or ""
    system_prompt = build_system_prompt(
        character, user_name=user_name, memories_text=memories_text, extra_instructions=extra,
        max_chars=max(max_chars, 60),
        summaries_text=ShortTermMemory.summaries_text(summaries),
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(ShortTermMemory.to_chat_messages(list(history_rows)))
    if len(messages) == 1:
        messages.append({"role": "user", "content": "在吗？"})
    return messages


def build_proactive_messages(
    character: Mapping[str, Any],
    config: Any,
    history_rows: Sequence[Mapping[str, Any]] = (),
    memory_rows: Sequence[Mapping[str, Any]] = (),
    interval_hint: str = "",
    summaries: Sequence[Mapping[str, Any]] = (),
) -> List[Dict[str, str]]:
    """主动消息：在历史之后追加一条“隐藏指令”，让角色主动开口。"""
    user_name = str(config.get("qq.user_nickname", "你") or "你")
    max_chars = int(config.get("proactive.max_message_chars", 120) or 120)

    extra = (
        "【本次任务】\n"
        "现在不是对方在找你，而是你主动想找对方说话。"
        "请结合你们的聊天历史和你的记忆，自然地发起一句新的闲聊或关心。"
        "不要重复历史里已经说过的话，不要道歉说“好久没联系”。"
    )
    system_prompt = build_system_prompt(
        character,
        user_name=user_name,
        memories_text=_format_memories(memory_rows),
        extra_instructions=extra,
        max_chars=max_chars,
        summaries_text=ShortTermMemory.summaries_text(summaries),
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(ShortTermMemory.to_chat_messages(list(history_rows)))

    instruction = (
        "（系统提示：%s现在请你主动给%s发一条消息。直接输出你要说的话，"
        "不要任何前缀、引号、括号或解释，不超过 %d 字。）"
        % (interval_hint or "", user_name, max_chars)
    )
    messages.append({"role": "user", "content": instruction})
    return messages


def _format_memories(memory_rows: Sequence[Mapping[str, Any]]) -> str:
    if not memory_rows:
        return ""
    lines = []
    for row in memory_rows:
        content = _clean_block(row.get("content") if isinstance(row, Mapping) else row)
        if content:
            lines.append("- %s" % content)
    return "\n".join(lines)
