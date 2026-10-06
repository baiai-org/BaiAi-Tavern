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
        self._vision_llm: Optional[LLMClient] = None
        self._vision_signature: Optional[tuple] = None

    def apply_config(self, config: Any) -> None:
        self.config = config
        self.short.max_messages = max(2, int(config.get("memory.short_term_max", 20) or 20))
        self.long.retrieve_limit = max(1, int(config.get("memory.long_term_retrieve", 5) or 5))
        self.long.enabled = bool(config.get("memory.long_term_enabled", True))
        self.llm.apply_config(config)

    # ------------------------------------------------------------ 视觉槽位
    def vision_configured(self) -> bool:
        from common.providers import SLOT_VISION, load_slot

        return load_slot(self.config, SLOT_VISION).configured

    def _vision_client(self) -> LLMClient:
        """看图用独立线路（vision 槽位）；参数变化时重建。"""
        from common.providers import SLOT_VISION, load_slot

        spec = load_slot(self.config, SLOT_VISION)
        signature = (spec.base_url, spec.api_key, spec.model)
        if self._vision_llm is None or self._vision_signature != signature:
            # 推理类模型（如 deepseek-flash）看图会先"想"很久：token 预算太小会
            # 全部耗在思考上、正文返回空（实测 finish_reason=length 且 content 为空），
            # 所以视觉线路的 max_tokens 至少给到 1024。
            self._vision_llm = LLMClient(
                base_url=spec.base_url,
                api_key=spec.api_key,
                model=spec.model,
                max_tokens=max(int(self.config.get("llm.max_tokens", 500) or 500), 1024),
                temperature=float(self.config.get("llm.temperature", 0.85) or 0.85),
                top_p=float(self.config.get("llm.top_p", 1.0) or 1.0),
                timeout=float(self.config.get("llm.timeout", 60) or 60),
                max_retries=int(self.config.get("llm.max_retries", 2) or 0),
            )
            self._vision_signature = signature
        return self._vision_llm

    @staticmethod
    def _with_image(messages: List[Dict[str, Any]], user_text: str, image_paths: List[str]) -> List[Dict[str, Any]]:
        """把**当前（最后一条）user 消息**升级成多模态（文本 + 图片 data URL）。

        必须挂在最后一条 user 消息上：deepseek-flash 实测，图片挂在历史早期的
        user 消息上时模型经常"看不到"图（回复"图没加载出来"），挂在当前消息
        则能正常描述图片内容。
        """
        from ..media.images import ImageError, image_data_url

        target = -1
        for index, item in enumerate(messages):
            if item.get("role") == "user":
                target = index
        if target < 0:
            return messages

        item = messages[target]
        text = str(item.get("content") or user_text or "").strip()
        # 入库时给带图消息加的「【图片】」前缀：图已经在多模态内容里了，去掉避免重复
        if text.startswith("【图片】"):
            text = text[len("【图片】") :].strip()
        content: Any = []
        if text:
            content.append({"type": "text", "text": text})
        for path in image_paths:
            try:
                content.append(
                    {"type": "image_url", "image_url": {"url": image_data_url(path)}}
                )
            except (ImageError, OSError):
                continue
        if not content:
            return messages
        if len(content) == 1:
            content = content[0]["text"]
        result = list(messages)
        new_item = dict(item)
        new_item["content"] = content
        result[target] = new_item
        return result

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
        image_paths: Optional[List[str]] = None,
    ) -> Optional[str]:
        """生成回复。失败返回 None（此时用户消息依然已入库，保留上下文）。

        ``image_paths`` 非空时走视觉线路（vision 槽位）看图回复；
        视觉未配置则把“收到图片但看不到”写进提示，让角色自然回应。
        """
        character_id = str(character.get("id") or "")
        user_text = (user_text or "").strip()
        image_paths = [str(p) for p in (image_paths or []) if str(p)]
        if not character_id or not (user_text or image_paths):
            return None

        # 有图无字时，给视觉模型一个明确的看图任务
        if image_paths and not user_text:
            user_text = "（用户发来了 %d 张图片，请看图内容）" % len(image_paths)
        elif image_paths and user_text in ("", "（对方发来了一条消息）"):
            user_text = "（用户发来了 %d 张图片，请看图内容）" % len(image_paths)
        # 短期记忆里标记“这条带了图片”，避免后续上下文里信息丢失
        stored_text = user_text if not image_paths else "【图片】%s" % user_text

        try:
            if store:
                await self.short.append(
                    character_id,
                    "user",
                    stored_text,
                    kind="image" if image_paths else "text",
                    media_path=image_paths[0] if image_paths else "",
                )
            history = await self.short.history(character_id)
            memories = await self.memories_for(character_id, query=user_text)
            hint = chat_hint
            if image_paths and self.vision_configured():
                messages = build_reply_messages(
                    character, self.config, history, memories, chat_hint=hint
                )
                messages = self._with_image(messages, user_text, image_paths)
                log.info("视觉线路 [%s] 开始看图（%d 张）", self._vision_client().model, len(image_paths))
                content = await self._vision_client().chat(messages)
                log.info("视觉线路 [%s] 看图回复成功（%d 张，%d 字）", self._vision_client().model, len(image_paths), len(content))
            else:
                if image_paths:
                    hint = (
                        "用户发来了 %d 张图片，但还没有配置图像理解，你看不见图片内容。"
                        "可以自然地表现出好奇，并请对方描述一下。" % len(image_paths)
                    )
                messages = build_reply_messages(
                    character, self.config, history, memories, chat_hint=hint
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
            content = self._truncate_keep_marker(content, max_chars)
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

    def _truncate_keep_marker(self, content: str, max_chars: int) -> str:
        """截断主动消息，但保留末尾的 ``[IMG] 描述`` 标记行（V0.2 发图约定）。"""
        marker = str(self.config.get("media.image_marker", "[IMG]") or "[IMG]")
        if marker in content:
            index = content.find(marker)
            head = content[:index].rstrip()
            tail = content[index:].strip()
            return (truncate(head, max_chars, suffix="") + "\n" + tail).strip()
        return truncate(content, max_chars, suffix="")

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
        if self._vision_llm is not None:
            await self._vision_llm.close()
            self._vision_llm = None
            self._vision_signature = None
