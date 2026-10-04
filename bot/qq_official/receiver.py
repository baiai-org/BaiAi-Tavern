"""官方机器人事件处理：把平台事件翻译成统一的入站消息。

每个机器人（官方 AppID）都有自己的 :class:`OfficialReceiver` 实例，
因此「收到消息 → 用哪个角色回复 → 用哪条通道发出」全部绑定到具体机器人上。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from common.logging_setup import get_logger
from common.utils import truncate

from ..chat_router import IncomingMessage, handle_incoming, strip_mentions

log = get_logger("bot.qq_official.receiver")

# 私聊消息事件 / 群聊 @ 消息事件
C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
# 关注 / 加群等，用于把 openid 记下来
FRIEND_ADD = "FRIEND_ADD"
GROUP_ADD_ROBOT = "GROUP_ADD_ROBOT"
C2C_MSG_RECEIVE = "C2C_MSG_RECEIVE"


def parse_event(event: Dict[str, Any], self_id: str = "") -> Optional[IncomingMessage]:
    """把官方事件解析成 :class:`IncomingMessage`（非消息事件返回 None）。"""
    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    if event_type not in (C2C_MESSAGE_CREATE, GROUP_AT_MESSAGE_CREATE, C2C_MSG_RECEIVE):
        return None

    author = data.get("author") or {}
    content = strip_mentions(str(data.get("content") or ""), self_id)
    message_id = str(data.get("id") or "")
    is_group = event_type == GROUP_AT_MESSAGE_CREATE

    if is_group:
        group_id = str(data.get("group_openid") or "")
        peer_id = str(author.get("member_openid") or author.get("union_openid") or "")
    else:
        group_id = ""
        peer_id = str(
            author.get("user_openid")
            or author.get("union_openid")
            or author.get("id")
            or ""
        )

    if not peer_id:
        log.debug("官方事件缺少 openid，忽略：%s", truncate(str(data), 160))
        return None

    return IncomingMessage(
        text=content,
        peer_id=peer_id,
        is_group=is_group,
        group_id=group_id,
        self_id=self_id,
        message_id=message_id,
        source="official",
        raw=data,
    )


class OfficialReceiver:
    """某个官方机器人的消息处理入口（由该机器人的网关回调驱动）。"""

    def __init__(self, runtime: Any, bot: Any):
        self.runtime = runtime
        self.bot = bot
        # 被动回复序号：同一个 msg_id 的多次回复要递增（官方要求）
        self._seq: Dict[str, int] = {}

    # ------------------------------------------------------------------ 事件入口
    async def handle(self, event: Dict[str, Any]) -> None:
        runtime = self.runtime
        bot = self.bot
        event_type = str(event.get("type") or "")
        data = event.get("data") or {}

        if event_type in ("READY", "RESUMED"):
            return
        if event_type == FRIEND_ADD:
            openid = str(data.get("openid") or (data.get("author") or {}).get("user_openid") or "")
            if openid:
                log.info("有用户添加了机器人「%s」：%s", bot.name, openid)
                runtime.publish(
                    {
                        "type": "qq_event",
                        "event": event_type,
                        "peer_id": openid,
                        "bot_id": bot.id,
                        "bot_name": bot.name,
                    }
                )
                asyncio.create_task(self._maybe_send_greeting(openid))
            return
        if event_type == GROUP_ADD_ROBOT:
            group_openid = str(data.get("group_openid") or "")
            log.info("机器人「%s」被加入群：%s", bot.name, group_openid)
            if group_openid:
                await bot.remember_openid(group_openid=group_openid)
            runtime.publish(
                {
                    "type": "qq_event",
                    "event": event_type,
                    "group_id": group_openid,
                    "bot_id": bot.id,
                    "bot_name": bot.name,
                }
            )
            return

        incoming = parse_event(event, self_id=str(bot.effective_self_id() or ""))

        # 事件里带的 self_id 也用于学习（官方事件本身不带 QQ 号，这里仅在缺失时补）
        if incoming is None:
            log.debug("忽略官方事件：%s", event_type)
            return

        if not await self._should_reply(incoming):
            return

        await self._remember_peer(incoming)
        await handle_incoming(runtime, incoming, self._reply, bot=bot)

    # ------------------------------------------------------------------ 判定
    async def _should_reply(self, incoming: IncomingMessage) -> bool:
        bot = self.bot
        official = bot.spec.official_config

        if incoming.is_group:
            if not bool(bot.spec.qq("group_reply_enabled", False)):
                log.debug("机器人「%s」未开启群聊回复，忽略群消息", bot.name)
                return False
            allowed = official.get("allowed_groups") or []
            if allowed and incoming.group_id not in {str(item) for item in allowed}:
                log.debug("群 %s 不在机器人「%s」的白名单里", incoming.group_id, bot.name)
                return False
            return True

        if not bool(official.get("allow_all_users", True)):
            allowed = {str(item) for item in (official.get("allowed_users") or [])}
            if incoming.peer_id not in allowed:
                log.debug("用户 %s 不在机器人「%s」的白名单里", incoming.peer_id, bot.name)
                return False
        return True

    async def _remember_peer(self, incoming: IncomingMessage) -> None:
        """记住最近一次私聊/群聊对象，用于主动消息（官方平台只能靠 openid）。"""
        bot = self.bot
        try:
            if incoming.is_group:
                await bot.remember_openid(group_openid=incoming.group_id)
            else:
                await bot.remember_openid(openid=incoming.peer_id)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ 发送
    async def _reply(self, text: str, character: Dict[str, Any], incoming: IncomingMessage) -> bool:
        bot = self.bot
        official = bot.spec.official_config
        max_len = int(official.get("reply_segment_max_len", 200) or 200)
        max_segments = int(official.get("max_reply_segments", 3) or 3)
        markdown = bool(official.get("markdown", False))

        from .client import segments_for_official

        segments = segments_for_official(text, max_len=max_len, max_segments=max_segments)
        if not segments:
            return False

        sender = bot.messaging.official_client()
        char_name = str(character.get("name") or "")
        sent_any = False
        for index, segment in enumerate(segments):
            seq = self._next_seq(incoming.message_id)
            try:
                if incoming.is_group:
                    await sender.send_group(
                        incoming.group_id, segment, msg_id=incoming.message_id, msg_seq=seq, markdown=markdown
                    )
                else:
                    await sender.send_c2c(
                        incoming.peer_id, segment, msg_id=incoming.message_id, msg_seq=seq, markdown=markdown
                    )
                sent_any = True
            except Exception as exc:
                log.error("机器人「%s」官方通道发送失败（%s）：%s", bot.name, char_name, exc)
                self.runtime.note_error("官方通道发送失败：%s" % exc)
                self.runtime.publish({"type": "error", "scope": "send", "error": str(exc), "bot_id": bot.id})
                break
            if index < len(segments) - 1:
                await asyncio.sleep(0.8)  # 多段之间稍作停顿，避免撞频控
        return sent_any

    def _next_seq(self, message_id: str) -> int:
        if not message_id:
            return 1
        current = self._seq.get(message_id, 0) + 1
        if current > 5:  # 官方限制：同一 msg_id 最多 5 次回复
            current = 5
        self._seq[message_id] = current
        if len(self._seq) > 200:  # 简单清理，避免无限增长
            for key in list(self._seq.keys())[:100]:
                self._seq.pop(key, None)
        return current

    async def _maybe_send_greeting(self, openid: str) -> None:
        """用户添加机器人后打一声招呼（官方允许的主动消息之一）。"""
        bot = self.bot
        try:
            if not bot.messaging.official_ready():
                return
            character = await bot.resolved_character()
            if character is None:
                import random

                characters = await self.runtime.enabled_characters()
                if not characters:
                    return
                character = random.choice(characters)
            content = str(character.get("first_mes") or "").strip()
            if not content:
                return
            from .client import segments_for_official

            for segment in segments_for_official(content):
                await bot.messaging.official_client().send_c2c(openid, segment)
            self.runtime.publish(
                {
                    "type": "reply_sent",
                    "character": character.get("name"),
                    "content": content,
                    "chat_type": "private",
                    "source": "official",
                    "trigger": "friend_add",
                    "bot_id": bot.id,
                    "bot_name": bot.name,
                }
            )
        except Exception as exc:  # pragma: no cover
            log.debug("发送好友添加问候失败：%s", exc)


__all__ = [
    "C2C_MESSAGE_CREATE",
    "FRIEND_ADD",
    "GROUP_ADD_ROBOT",
    "OfficialReceiver",
    "parse_event",
]
