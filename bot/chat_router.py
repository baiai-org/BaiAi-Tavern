"""统一的「收到消息 → 生成回复 → 发送」路由。

链路：

* 记录用户发言时间（空闲触发要用）
* 选角色（``#角色名`` 显式指定 → 该机器人绑定的角色 → 会话上次发言的角色 → 随机）
* 调用 AI 引擎生成回复（带该角色独立的上下文与记忆）
* 通过调用方传入的 ``reply`` 回调真正发送（官方通道自己实现发送细节）
* 记录会话归属、更新界面状态、推送事件

通道只负责：解析官方平台事件 → 判定是否需要响应 → 构造
:class:`IncomingMessage` → 调用 :func:`handle_incoming`。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from common.logging_setup import get_logger
from common.utils import truncate

from .ai_engine import REPLY_FALLBACK
from .database import crud
from .media.hub import MediaInbound, OutgoingReply

log = get_logger("bot.chat_router")


@dataclass
class IncomingMessage:
    """统一的入站消息描述。"""

    text: str
    peer_id: str                      # 私聊对端（user_openid）
    is_group: bool = False
    group_id: str = ""                # 群 openid
    self_id: str = ""
    mentioned: bool = True            # 群消息是否 @ 了**本机器人**（私聊恒为 True）
    any_mentioned: bool = False       # 群消息里有没有 @ 任何人（@ 了别人时本机器人在群里让路）
    message_id: str = ""              # 被动回复要用的消息 id（官方平台必需）
    source: str = "official"          # 事件来源（官方平台）
    session_key: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        if self.session_key:
            return self.session_key
        if self.is_group:
            return "%s:group:%s:%s" % (self.source, self.group_id, self.peer_id)
        return "%s:private:%s" % (self.source, self.peer_id)


# 回复回调：``async def reply(out, character, incoming) -> bool``
# ``out`` 是 :class:`OutgoingReply`（文字/语音 + 可选图片），由通道负责真正发出。
ReplyFn = Callable[[OutgoingReply, Dict[str, Any], IncomingMessage], Awaitable[bool]]


async def select_character(
    runtime: Any, session_key: str, text: str, bot: Any = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    """返回 ``(角色, 去掉指令前缀后的文本)``。

    顺序：

    1. ``#角色名`` 显式指定（临时换人，优先级最高）；
    2. **该机器人绑定的角色**（多机器人时由用户在界面上指定）；
    3. 该会话上次发言的角色（保持连贯）；
    4. 随机。
    """
    characters = await runtime.enabled_characters()
    if not characters:
        return None, text

    stripped = (text or "").strip()
    for character in characters:
        name = str(character.get("name") or "")
        if not name:
            continue
        for prefix in ("#%s" % name, "＃%s" % name):
            if stripped.startswith(prefix):
                remainder = stripped[len(prefix) :].strip(" ：:,，")
                return character, remainder or stripped

    if bot is not None:
        bound = getattr(bot, "character_id", "") or ""
        if bound:
            for character in characters:
                if str(character.get("id")) == str(bound):
                    return character, text
            log.warning(
                "机器人「%s」绑定的角色（%s）不存在或未启用，改为按会话/随机选择",
                getattr(bot, "name", ""),
                bound,
            )

    last_id = await crud.get_setting(runtime.db, "session:%s:last_character" % session_key, "")
    if last_id:
        for character in characters:
            if str(character.get("id")) == str(last_id):
                return character, text

    return random.choice(characters), text


def default_session_key(source: str, is_group: bool, peer_id: str, group_id: str) -> str:
    if is_group:
        return "%s:group:%s:%s" % (source, group_id, peer_id)
    return "%s:private:%s" % (source, peer_id)


async def handle_incoming(
    runtime: Any,
    incoming: IncomingMessage,
    reply: ReplyFn,
    bot: Any = None,
) -> Optional[str]:
    """核心链路：入库 → 选角色 → 多媒体解析 → 生成 → 组装 → 发送 → 记录。

    返回回复文字（失败返回 None）。
    """
    config = runtime.config
    text = (incoming.text or "").strip()
    raw = incoming.raw or {}
    attachments = raw.get("attachments") if isinstance(raw, dict) else None
    has_attachments = isinstance(attachments, list) and len(attachments) > 0
    if not text and not has_attachments:
        return None
    spec = getattr(bot, "spec", None)
    reply_enabled = (
        bool(spec.qq("reply_enabled", True)) if spec is not None else bool(config.get("qq.reply_enabled", True))
    )
    if not reply_enabled:
        log.info("已关闭自动回复（qq.reply_enabled = false），忽略本条消息")
        return None

    await runtime.note_user_message()
    session_key = incoming.key()
    character, user_text = await select_character(runtime, session_key, text, bot=bot)
    if character is None:
        log.warning("收到消息但没有启用的角色，无法回复")
        return None

    log.info(
        "收到消息 [%s/%s] %s%s：%s",
        incoming.source,
        "群聊" if incoming.is_group else "私聊",
        incoming.peer_id,
        ("（机器人：%s）" % getattr(bot, "name", "")) if bot is not None else "",
        truncate(user_text or "（图片/语音消息）", 60),
    )

    hint = ""
    if incoming.is_group:
        if incoming.mentioned:
            hint = "【当前场景】这是 QQ 群聊，对方在群里 @ 了你，请正常回应。"
        else:
            hint = "【当前场景】这是 QQ 群聊，对方没有 @ 你（是普通群消息）。"
            "群里还有其他人，回复时自然一点，别显得突兀。"
    else:
        hint = "【当前场景】这是 QQ 单聊（官方机器人通道）。"

    # ------------------------------------------------------------ 入站多媒体
    hub = getattr(runtime, "media", None)
    image_paths: List[str] = []
    voice_path = ""
    if hub is not None:
        try:
            media: Optional[MediaInbound] = await hub.inbound_media(incoming)
            if media is not None:
                image_paths = list(media.image_paths)
                voice_path = str(media.voice_paths[0]) if media.voice_paths else ""
                note = media.text_note()
                if note:
                    user_text = (user_text + " " + note).strip() if user_text else note
                log.info(
                    "入站多媒体：images=%d voice_path=%r voice_texts=%r errors=%s",
                    len(image_paths),
                    bool(voice_path),
                    media.voice_texts,
                    media.errors,
                )
        except Exception as exc:
            log.warning("入站多媒体处理失败（按纯文字继续）：%s", exc)
        media_hint = hub.media_hint()
        if media_hint:
            hint = (hint + "\n" + media_hint).strip()

    llm_text = user_text or (text or "（对方发来了一条消息）")
    content = await runtime.engine.reply(
        character, llm_text, chat_hint=hint, image_paths=image_paths or None, voice_path=voice_path
    )
    degraded = False
    if not content:
        content = REPLY_FALLBACK
        degraded = True

    # ------------------------------------------------------------ 出站组装
    out = OutgoingReply(text=content, body=content)
    if hub is not None:
        try:
            out = await hub.compose(character, content)
        except Exception as exc:
            log.warning("出站消息组装失败（按纯文字继续）：%s", exc)
            out = OutgoingReply(text=content, body=content)

    sent = False
    try:
        sent = await reply(out, character, incoming)
    except Exception as exc:  # pragma: no cover - 发送失败不应影响后续消息
        log.error("回复发送失败：%s", exc)
        runtime.note_error("回复发送失败：%s" % exc)
        runtime.publish({"type": "error", "scope": "send", "error": str(exc)})

    # V0.2.2：回复以图片/语音发出时，把记录里那条消息标记为带媒体
    if sent and out.has_media:
        try:
            if out.voice_paths:
                await crud.update_last_message_media(
                    runtime.db, str(character.get("id") or ""), "voice", str(out.voice_paths[0])
                )
            elif out.image_path:
                await crud.update_last_message_media(
                    runtime.db, str(character.get("id") or ""), "image", str(out.image_path)
                )
        except Exception as exc:  # pragma: no cover
            log.debug("标记出站媒体记录失败：%s", exc)

    # V0.2.2：回复后顺手压缩过期对话（后台跑，不阻塞事件推送）
    try:
        import asyncio as _asyncio

        _asyncio.create_task(runtime.maybe_summarize(str(character.get("id") or "")))
    except Exception:  # pragma: no cover
        pass

    await crud.set_setting(
        runtime.db, "session:%s:last_character" % session_key, str(character.get("id") or "")
    )
    runtime.note_reply(str(out.text), str(character.get("name") or ""))
    runtime.publish(
        {
            "type": "reply_sent",
            "character": character.get("name"),
            "character_id": character.get("id"),
            "content": out.text,
            "degraded": degraded,
            "sent": sent,
            "chat_type": "group" if incoming.is_group else "private",
            "source": incoming.source,
            "peer_id": incoming.peer_id,
            "inbound_image": image_paths is not None and len(image_paths) > 0,
            "outbound_image": bool(out.image_path),
            "outbound_voice": bool(out.voice_paths),
            "bot_id": getattr(bot, "id", ""),
            "bot_name": getattr(bot, "name", ""),
        }
    )
    return str(out.text)


def strip_mentions(text: str, bot_id: str = "") -> str:
    """去掉官方平台消息里的 @ 占位（``<@!123456>`` 形如 CQ 码的标记）。"""
    import re

    cleaned = re.sub(r"<@!?\d+>", " ", text or "")
    cleaned = re.sub(r"<@[^>]*>", " ", cleaned)
    if bot_id:
        cleaned = cleaned.replace("<@%s>" % bot_id, " ")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


__all__ = [
    "IncomingMessage",
    "ReplyFn",
    "default_session_key",
    "handle_incoming",
    "select_character",
    "strip_mentions",
]
