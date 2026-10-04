"""短期记忆：最近 N 轮对话。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..database import Database
from ..database import crud


class ShortTermMemory:
    """对话上下文管理。

    每个角色拥有独立的历史（按 ``character_id`` 隔离），
    保证多角色人格不会互相污染。
    """

    def __init__(self, db: Database, max_messages: int = 20):
        self.db = db
        self.max_messages = max(2, int(max_messages))

    async def append(
        self,
        character_id: str,
        role: str,
        content: str,
        is_proactive: bool = False,
    ) -> int:
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = (content or "").strip()
        if not content:
            return 0
        return await crud.add_message(
            self.db, character_id, role, content, is_proactive=is_proactive
        )

    async def history(
        self, character_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        return await crud.recent_messages(
            self.db, character_id, limit or self.max_messages
        )

    async def clear(self, character_id: str) -> int:
        return await crud.clear_messages(self.db, character_id)

    @staticmethod
    def to_chat_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """转换为 OpenAI Chat Completions 的 messages 结构。"""
        result: List[Dict[str, str]] = []
        for row in rows:
            role = str(row.get("role") or "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            result.append({"role": role, "content": content})
        return result

    @staticmethod
    def format_for_prompt(rows: List[Dict[str, Any]], user_name: str = "你") -> str:
        """把历史对话渲染成纯文本（用于嵌入系统提示词）。"""
        lines: List[str] = []
        for row in rows:
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            speaker = "我" if str(row.get("role")) == "user" else "（你）"
            lines.append("%s：%s" % (speaker, content))
        return "\n".join(lines)
