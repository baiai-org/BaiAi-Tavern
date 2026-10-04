"""长期记忆：事实条目的抽取、存储与检索。

检索采用轻量方案（关键词重叠加权 + 时间衰减），
不引入向量库，保证打包体积与运行内存可控。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from common.utils import seconds_since

from ..database import Database
from ..database import crud

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n\r]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 值得长期记住的语句特征
_MEMORY_HINTS = (
    "我叫",
    "我的名字",
    "我是",
    "我住",
    "我在",
    "我喜欢",
    "我爱",
    "我讨厌",
    "我不喜欢",
    "我的生日",
    "我养",
    "我有",
    "我习惯",
    "我每天",
    "记得",
    "别忘",
    "以后",
    "下次",
    "我的工作",
    "我上班",
    "我上学",
    "我最近",
)

_STOPWORDS = {
    "的",
    "了",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "们",
    "吗",
    "呢",
    "啊",
    "吧",
    "在",
    "和",
    "就",
    "也",
    "都",
    "很",
    "the",
    "and",
    "you",
    "for",
}


def tokenize(text: str) -> List[str]:
    """中英文混合分词：英文按词，中文按双字滑窗。"""
    text = (text or "").lower()
    tokens: List[str] = [word for word in _WORD_RE.findall(text) if word not in _STOPWORDS]
    cjk_chars = [char for char in text if _CJK_RE.match(char)]
    for index in range(len(cjk_chars) - 1):
        gram = cjk_chars[index] + cjk_chars[index + 1]
        if gram not in _STOPWORDS:
            tokens.append(gram)
    for char in cjk_chars:
        if char not in _STOPWORDS:
            tokens.append(char)
    return tokens


class LongTermMemory:
    def __init__(self, db: Database, retrieve_limit: int = 5, enabled: bool = True):
        self.db = db
        self.retrieve_limit = max(1, int(retrieve_limit))
        self.enabled = bool(enabled)

    # ---------------------------------------------------------------- 写入
    async def add(self, character_id: str, content: str, weight: float = 1.0) -> int:
        content = (content or "").strip()
        if not content:
            return 0
        if await crud.memory_exists(self.db, character_id, content):
            return 0
        return await crud.add_memory(self.db, character_id, content, weight=weight)

    async def add_many(self, character_id: str, contents: List[str]) -> List[str]:
        stored: List[str] = []
        for content in contents:
            if await self.add(character_id, content):
                stored.append(content)
        return stored

    async def list(self, character_id: str) -> List[Dict[str, Any]]:
        return await crud.list_memories(self.db, character_id)

    async def delete(self, memory_id: int) -> bool:
        return await crud.delete_memory(self.db, memory_id)

    # ---------------------------------------------------------------- 检索
    async def retrieve(
        self, character_id: str, query: str = "", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        rows = await crud.list_memories(self.db, character_id)
        if not rows:
            return []
        limit = max(1, int(limit or self.retrieve_limit))
        query_tokens = set(tokenize(query)) if query else set()

        scored: List[tuple] = []
        for row in rows:
            content = str(row.get("content") or "")
            weight = float(row.get("weight") or 1.0)
            score = weight
            if query_tokens:
                content_tokens = set(tokenize(content))
                overlap = len(query_tokens & content_tokens)
                score += overlap * 1.5
                if overlap == 0:
                    score -= 0.5
            age = seconds_since(row.get("created_at"))
            if age is not None:
                score += max(0.0, 0.5 - age / (30 * 86400))
            scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        picked = [row for score, row in scored[:limit] if score > 0]
        if not picked and len(rows) <= limit:
            picked = rows
        return picked

    @staticmethod
    def format_for_prompt(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return ""
        return "\n".join("- %s" % str(row.get("content") or "").strip() for row in rows)

    # ---------------------------------------------------------------- 抽取
    def extract_candidates(self, text: str, limit: int = 3) -> List[str]:
        """从用户发言中挑选值得长期记住的事实（启发式）。"""
        if not text:
            return []
        candidates: List[str] = []
        for raw in _SENTENCE_SPLIT_RE.split(text):
            sentence = raw.strip()
            if not (4 <= len(sentence) <= 80):
                continue
            if not any(hint in sentence for hint in _MEMORY_HINTS):
                continue
            if sentence.endswith("?") or sentence.endswith("？"):
                continue
            if sentence not in candidates:
                candidates.append(sentence)
        return candidates[:limit]

    async def extract_and_store(
        self, character_id: str, text: str, limit: int = 3
    ) -> List[str]:
        if not self.enabled:
            return []
        return await self.add_many(character_id, self.extract_candidates(text, limit=limit))
