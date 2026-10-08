"""官方机器人事件处理：把平台事件翻译成统一的入站消息。

每个机器人（官方 AppID）都有自己的 :class:`OfficialReceiver` 实例，
因此「收到消息 → 用哪个角色回复 → 用哪条通道发出」全部绑定到具体机器人上。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional

from common.logging_setup import get_logger
from common.utils import truncate

from ..chat_router import IncomingMessage, handle_incoming, strip_mentions

log = get_logger("bot.qq_official.receiver")

# 私聊消息事件 / 群聊 @ 消息事件 / 群聊普通消息事件
C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
GROUP_MESSAGE_CREATE = "GROUP_MESSAGE_CREATE"
# 关注 / 加群等，用于把 openid 记下来
FRIEND_ADD = "FRIEND_ADD"
GROUP_ADD_ROBOT = "GROUP_ADD_ROBOT"
C2C_MSG_RECEIVE = "C2C_MSG_RECEIVE"

_GROUP_EVENTS = (GROUP_AT_MESSAGE_CREATE, GROUP_MESSAGE_CREATE)

# 同一 msg_id 的去重窗口（官方平台「相同 msg_id 可能重复推送，需结合 msg_id 去重」，
# 见 bot.q.qq.com 群@机器人事件文档；@ 事件与全量群消息事件可能同时收到同一条）
_SEEN_TTL = 300.0


def _bot_identities(bot: Any) -> List[str]:
    """本机器人已知的全部身份标识（AppID / user_id / 学习到的 id）。

    全量群消息里 ``<@...>`` 标记可能用其中任何一种指向机器人，全部拿来
    匹配才不会漏掉 @。
    """
    identities = set()
    try:
        app_id = str(bot.spec.official("app_id", "") or "").strip()
        if app_id:
            identities.add(app_id)
    except Exception:
        pass
    try:
        self_id = str(bot.effective_self_id() or "").strip()
        if self_id and self_id != "0":
            identities.add(self_id)
    except Exception:
        pass
    try:
        learned = str(getattr(bot, "learned_self_id", "") or "").strip()
        if learned:
            identities.add(learned)
    except Exception:
        pass
    try:
        gateway = getattr(bot, "gateway", None)
        client = getattr(gateway, "client", None) if gateway is not None else None
        info = getattr(client, "bot_info", {}) if client is not None else {}
        for key in ("id", "user_id"):
            value = str((info or {}).get(key) or "").strip()
            if value:
                identities.add(value)
    except Exception:
        pass
    return sorted(identities)


def _mentions_self(raw_content: str, mentions: Any, self_ids: Any) -> bool:
    """全量群消息里判断「@ 的是不是本机器人」。

    平台（2026-09 起）的群 @ 消息 content **已去除 @ 前缀**，@ 判定只能靠
    ``mentions``（消息中 @ 的用户列表，User 对象）。匹配顺序：

    1. ``mentions[].is_you`` 为 True —— 平台直接标注「@ 的是你」（实测字段，
       文档未列）；
    2. mention 条目的任一身份字段（``id`` / ``user_openid`` / ``union_openid`` /
       ``member_openid``）等于本机器人的任一已知身份（AppID / user_id / 学习到的
       id / READY 里的 openid）；
    3. content 里的 ``<@!?(...)>`` 占位等于本机器人身份（旧平台格式，实测抓包
       ``<@BOT_ID> 文本``，保留兼容）。

    同一个群里有多个机器人时，只有被 @ 的那个该回复，所以必须精确匹配身份，
    不能只看到 ``<@`` 或 ``bot: true`` 就认作 @ 了自己。
    """
    ids = []
    if isinstance(self_ids, (list, tuple, set)):
        ids = [str(item).strip() for item in self_ids if str(item).strip()]
    else:
        value = str(self_ids or "").strip()
        if value:
            ids = [value]
    id_set = set(ids)

    if isinstance(mentions, list):
        for item in mentions:
            if not isinstance(item, dict):
                continue
            if item.get("is_you") is True:
                return True
            for key in ("id", "user_openid", "union_openid", "member_openid"):
                if str(item.get(key) or "").strip() in id_set:
                    return True

    content = raw_content or ""
    if not id_set:
        # 拿不到自己的任何身份（配置缺失）时退回旧行为：看到 @ 占位就当被 @
        return "<@" in content
    for identity in id_set:
        if re.search(r"<@!?%s>" % re.escape(identity), content):
            return True
    return False


def parse_event(
    event: Dict[str, Any], self_id: str = "", self_ids: Optional[list] = None
) -> Optional[IncomingMessage]:
    """把官方事件解析成 :class:`IncomingMessage`（非消息事件返回 None）。

    群聊两类事件都解析：``GROUP_AT_MESSAGE_CREATE``（@ 了机器人，content 已去掉
    @ 前缀、按文档语义就是 @ 了本机器人）与 ``GROUP_MESSAGE_CREATE``
    （「接收所有消息」全量模式：群里每条消息都推给每个机器人才会收到，
    content 同样已去掉 @ 前缀，需要按 ``mentions``（``is_you`` / 身份字段）
    或旧格式的 ``<@标识>`` 占位判断 @ 的是不是自己）。

    ``self_ids`` 是本机器人的全部已知身份（AppID / user_id / READY openid），
    全量模式下逐个匹配；只传 ``self_id`` 时用它一个。
    """
    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    is_group_event = event_type in _GROUP_EVENTS
    if event_type not in (C2C_MESSAGE_CREATE, C2C_MSG_RECEIVE) and not is_group_event:
        return None

    author = data.get("author") or {}
    raw_content = str(data.get("content") or "")
    content = strip_mentions(raw_content, self_id)
    message_id = str(data.get("id") or "")
    is_group = is_group_event
    mentions = data.get("mentions")
    # 有没有 @ 任何人（包括 @ 别的机器人 / 群成员）：@ 占位 或 mentions 非空
    any_mentioned = bool("<@" in raw_content) or (
        isinstance(mentions, list) and len(mentions) > 0
    )
    if is_group_event:
        if event_type == GROUP_AT_MESSAGE_CREATE:
            mentioned = True  # 文档语义：@ 了本机器人才会推这个事件
        else:
            identities = list(self_ids) if self_ids else ([self_id] if self_id else [])
            mentioned = _mentions_self(raw_content, mentions, identities)
            if not mentioned:
                # 诊断：全量消息里 @ 了机器人但没匹配上自己的身份 —— 打出 mention
                # 明细，方便真机排查平台下发的身份字段到底是哪种格式
                bot_mentions = [
                    {
                        key: str(item.get(key) or "")
                        for key in ("id", "user_openid", "member_openid", "is_you")
                    }
                    for item in (mentions if isinstance(mentions, list) else [])
                    if isinstance(item, dict) and item.get("bot")
                ]
                if bot_mentions or "<@" in raw_content:
                    log.warning(
                        "全量群消息 @ 了机器人但未匹配到本机器人身份（known=%s，mentions=%s，"
                        "content=%r），让路不回复",
                        identities,
                        bot_mentions,
                        truncate(raw_content, 80),
                    )
    else:
        mentioned = True

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
        mentioned=mentioned,
        any_mentioned=any_mentioned,
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
        # msg_id 去重：官方平台会对同一 msg_id 重复推送（文档明确要求去重），
        # 不去重时同一条消息会触发两次回复；值是 (state, timestamp)
        self._seen_messages: Dict[str, tuple] = {}

    # ------------------------------------------------------------------ 去重
    def _check_duplicate(self, message_id: str, mentioned: bool) -> bool:
        """同一 msg_id 的重复推送去重（返回 True = 本条应忽略）。

        官方平台对同一条消息可能推多次：@ 事件与全量事件可能**同 msg_id 先后到达
        （顺序不保证）**，同一事件也可能重推。全量事件可能先于 @ 事件到达且识别不出
        是 @ 本机器人（被让路），若把 msg_id 一刀切标成「已处理」，后到的 @ 事件会被
        吃掉 → 这条消息永远不会被回复（V0.2.2 真机踩坑：群 @ 彻底不回复）。
        因此按状态区分：

        * 已回复过（含：先到的事件就是 @ 自己）→ 后续同 msg_id 一律忽略；
        * 此前被让路（没 @ 本机器人）→ 这次若 @ 了本机器人（@ 事件），仍要处理。
        """
        if not message_id:
            return False
        now = time.time()
        state_at = self._seen_messages.get(message_id)
        if state_at is not None and (now - state_at[1]) >= _SEEN_TTL:
            self._seen_messages.pop(message_id, None)
            state_at = None
        if state_at is None:
            self._seen_messages[message_id] = ("replied" if mentioned else "skipped", now)
            if len(self._seen_messages) > 400:  # 顺手清理过期项，避免无限增长
                stale = [key for key, (_s, at) in self._seen_messages.items() if now - at >= _SEEN_TTL]
                for key in stale:
                    self._seen_messages.pop(key, None)
            return False
        state, _at = state_at
        if state == "replied":
            return True
        return not mentioned  # 此前让路：这次没 @ 自己才继续忽略

    def _mark_replied(self, message_id: str) -> None:
        if message_id:
            self._seen_messages[message_id] = ("replied", time.time())

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

        incoming = parse_event(
            event,
            self_id=str(bot.effective_self_id() or ""),
            self_ids=_bot_identities(bot),
        )

        # 事件里带的 self_id 也用于学习（官方事件本身不带 QQ 号，这里仅在缺失时补）
        if incoming is None:
            log.debug("忽略官方事件：%s", event_type)
            return

        if self._check_duplicate(incoming.message_id, incoming.mentioned):
            log.debug("msg_id=%s 重复推送（官方平台会重推），忽略", incoming.message_id)
            return

        if not await self._should_reply(incoming):
            return

        self._mark_replied(incoming.message_id)
        await self._remember_peer(incoming)
        await handle_incoming(runtime, incoming, self._reply, bot=bot)

    # ------------------------------------------------------------------ 判定
    async def _should_reply(self, incoming: IncomingMessage) -> bool:
        bot = self.bot
        official = bot.spec.official_config

        if incoming.is_group:
            if not bool(bot.spec.qq("group_reply_enabled", True)):
                log.debug("机器人「%s」未开启群聊回复，忽略群消息", bot.name)
                return False
            allowed = official.get("allowed_groups") or []
            if allowed and incoming.group_id not in {str(item) for item in allowed}:
                log.debug("群 %s 不在机器人「%s」的白名单里", incoming.group_id, bot.name)
                return False
            if not incoming.mentioned:
                if incoming.any_mentioned:
                    # 群里 @ 了别人（别的机器人 / 群成员）：这条消息是对着别人说的，
                    # 本机器人让路不插话（多机器人群里「@ 谁谁回答」的关键）
                    log.debug(
                        "机器人「%s」的群消息 @ 了别人（不是本机器人），忽略", bot.name
                    )
                    return False
                if not bool(bot.spec.qq("group_reply_without_at", True)):
                    log.debug(
                        "机器人「%s」未开启「响应没有 @ 的群消息」，忽略普通群消息", bot.name
                    )
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
    async def _reply(self, out: Any, character: Dict[str, Any], incoming: IncomingMessage) -> bool:
        """发送完整出站消息：文字（分段）+ 图片 + 语音（富媒体 msg_type=7）。"""
        from ..media.hub import OutgoingReply

        if not isinstance(out, OutgoingReply):  # 兼容旧签名（纯文字字符串）
            out = OutgoingReply(text=str(out or ""), body=str(out or ""))
        hub = self.runtime.media
        if hub is None:
            return await self._send_plain_text(out.body, incoming)
        try:
            result = await hub.send_outgoing(
                self.bot,
                out,
                is_group=incoming.is_group,
                peer_id=incoming.peer_id,
                group_id=incoming.group_id,
                message_id=incoming.message_id,
                next_seq=lambda: self._next_seq(incoming.message_id),
            )
        except Exception as exc:
            log.error("机器人「%s」官方通道发送失败：%s", self.bot.name, exc)
            self.runtime.note_error("官方通道发送失败：%s" % exc)
            self.runtime.publish({"type": "error", "scope": "send", "error": str(exc), "bot_id": self.bot.id})
            return False
        if not result.get("ok"):
            self.runtime.note_error("官方通道发送失败：%s" % result.get("error"))
            self.runtime.publish(
                {"type": "error", "scope": "send", "error": result.get("error"), "bot_id": self.bot.id}
            )
        return bool(result.get("sent_text")) or bool(result.get("sent_media"))

    async def _send_plain_text(self, text: str, incoming: IncomingMessage) -> bool:
        """MediaHub 不可用时的纯文字发送（保底路径）。"""
        from .client import segments_for_official

        official = self.bot.spec.official_config
        max_len = int(official.get("reply_segment_max_len", 200) or 200)
        max_segments = int(official.get("max_reply_segments", 3) or 3)
        markdown = bool(official.get("markdown", False))
        segments = segments_for_official(text or "", max_len=max_len, max_segments=max_segments)
        if not segments:
            return False
        sender = self.bot.messaging.official_client()
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
                log.error("机器人「%s」官方通道发送失败：%s", self.bot.name, exc)
                self.runtime.note_error("官方通道发送失败：%s" % exc)
                self.runtime.publish({"type": "error", "scope": "send", "error": str(exc), "bot_id": self.bot.id})
                break
            if index < len(segments) - 1:
                await asyncio.sleep(0.8)
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
