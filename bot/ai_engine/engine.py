"""对话引擎：把「角色 + 历史 + 记忆 + LLM」串起来的编排层。"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Mapping, Optional

from common.logging_setup import get_logger
from common.utils import truncate

from ..database import Database
from ..memory.long_term import LongTermMemory
from ..memory.short_term import ShortTermMemory
from .llm_client import LLMClient, LLMError
from .prompt_builder import build_proactive_messages, build_reply_messages

log = get_logger("bot.ai_engine")

REPLY_FALLBACK = "唔……刚刚有点走神，你再说一次好吗？"


class AIEngine:
    def __init__(self, db: Database, config: Any, llm: Optional[LLMClient] = None):
        self.db = db
        self.config = config
        self.llm = llm or LLMClient.from_config(config)
        self.short = ShortTermMemory(db, int(config.get("memory.short_term_max", 20) or 20))
        self.long = LongTermMemory(
            db,
            int(config.get("memory.long_term_retrieve", 5) or 5),
            bool(config.get("memory.long_term_enabled", True)),
        )

    def apply_config(self, config: Any) -> None:
        self.config = config
        self.short.max_messages = max(2, int(config.get("memory.short_term_max", 20) or 20))
        self.long.retrieve_limit = max(1, int(config.get("memory.long_term_retrieve", 5) or 5))
        self.long.enabled = bool(config.get("memory.long_term_enabled", True))
        self.llm.apply_config(config)

    # ------------------------------------------------------------ 上下文准备
    def _include_memory(self) -> bool:
        return bool(self.config.get("memory.long_term_enabled", True)) and bool(
            self.config.get("proactive.include_memory", True)
        )

    async def memories_for(
        self, character_id: str, query: str = ""
    ) -> List[Dict[str, Any]]:
        if not self._include_memory():
            return []
        return await self.long.retrieve(character_id, query=query)

    async def history_for(self, character_id: str, limit: int) -> List[Dict[str, Any]]:
        return await self.short.history(character_id, limit=limit)

    # ---------------------------------------------------------------- 被动回复
    async def reply(
        self,
        character: Mapping[str, Any],
        user_text: str,
        chat_hint: str = "",
        store: bool = True,
    ) -> Optional[str]:
        """生成回复。失败返回 None（此时用户消息依然已入库，保留上下文）。"""
        character_id = str(character.get("id") or "")
        user_text = (user_text or "").strip()
        if not character_id or not user_text:
            return None

        try:
            if store:
                await self.short.append(character_id, "user", user_text)
            history = await self.short.history(character_id)
            memories = await self.memories_for(character_id, query=user_text)
            messages = build_reply_messages(
                character, self.config, history, memories, chat_hint=chat_hint
            )
            content = await self.llm.chat(messages)
            content = content.strip()
            if not content:
                raise LLMError("模型返回空内容")
        except LLMError as exc:
            log.error("角色 [%s] 回复生成失败: %s", character.get("name"), exc)
            return None
        except Exception as exc:  # pragma: no cover - 防御性
            log.exception("角色 [%s] 回复出现异常: %s", character.get("name"), exc)
            return None

        if store:
            await self.short.append(character_id, "assistant", content)
            await self.extract_memory(character_id, user_text)
        log.info("角色 [%s] 回复: %s", character.get("name"), truncate(content, 40))
        return content

    # ---------------------------------------------------------------- 主动消息
    async def proactive(
        self,
        character: Mapping[str, Any],
        trigger_type: str = "manual",
        hint: str = "",
    ) -> Dict[str, Any]:
        """生成主动消息。返回 ``{content, degraded, error}``。"""
        character_id = str(character.get("id") or "")
        max_chars = int(self.config.get("proactive.max_message_chars", 120) or 120)
        context_messages = int(self.config.get("proactive.context_messages", 8) or 8)

        try:
            history = await self.short.history(character_id, limit=context_messages)
            memories = await self.memories_for(character_id)
            messages = build_proactive_messages(
                character, self.config, history, memories, interval_hint=hint
            )
            content = (await self.llm.chat(messages)).strip()
            if not content:
                raise LLMError("模型返回空内容")
            content = truncate(content, max_chars, suffix="")
            await self.short.append(character_id, "assistant", content, is_proactive=True)
            log.info(
                "角色 [%s] 主动消息(%s): %s",
                character.get("name"),
                trigger_type,
                truncate(content, 40),
            )
            return {"content": content, "degraded": False, "error": ""}
        except LLMError as exc:
            fallback = self.fallback_message()
            log.error(
                "角色 [%s] 主动消息生成失败，改用兜底话术: %s", character.get("name"), exc
            )
            await self.short.append(character_id, "assistant", fallback, is_proactive=True)
            return {"content": fallback, "degraded": True, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            log.exception("主动消息生成异常: %s", exc)
            return {"content": "", "degraded": True, "error": str(exc)}

    def fallback_message(self) -> str:
        messages = self.config.get("llm.fallback_messages", []) or []
        if isinstance(messages, str):
            messages = [messages]
        messages = [str(item) for item in messages if str(item).strip()]
        if not messages:
            return "在忙吗？突然有点想你了。"
        return random.choice(messages)

    # ---------------------------------------------------------------- 记忆维护
    async def extract_memory(self, character_id: str, user_text: str) -> List[str]:
        if not bool(self.config.get("memory.auto_extract", True)):
            return []
        try:
            stored = await self.long.extract_and_store(character_id, user_text)
            if stored:
                log.info("为角色 [%s] 新增 %d 条长期记忆", character_id, len(stored))
            return stored
        except Exception as exc:  # pragma: no cover
            log.warning("长期记忆抽取失败: %s", exc)
            return []

    # ---------------------------------------------------------------- 测试
    async def test_llm(self) -> Dict[str, Any]:
        return await self.llm.test_connection()

    async def close(self) -> None:
        await self.llm.close()
