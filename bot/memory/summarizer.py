"""对话压缩摘要（V0.2.2 上下文分级管理）。

策略（配置在 ``memory.`` 段，默认值见 ``common.config.DEFAULTS``）：

* 最近 ``context_window_days``（7）天内的消息 → 原文进入模型上下文；
* 更早的消息 → 由本模块**分批压缩成摘要**（``memory_summaries`` 表），
  摘要进系统提示词，原文不再发给模型；
* 超过 ``summary_window_days``（15）天的消息同样会被压缩（压完即归档），
  之后只留在数据库里供「对话查看」按月/按天检索，不再进上下文。

触发时机：

* 每次回复 / 主动消息生成后顺手检查一次（``maybe_summarize``）；
* 运行时另有周期任务扫描所有角色（``scan_all``），覆盖长时间没聊天的角色。

压缩失败的批次保持未标记，下次继续重试。
"""

from __future__ import annotations

from typing import Any, Dict, List

from common.logging_setup import get_logger

from ..database import crud

log = get_logger("bot.memory.summarizer")

SUMMARY_PROMPT = (
    "把以下聊天记录压缩成一段简明摘要：保留重要事实、约定、称呼、情绪走向和未完成的话题，"
    "删掉寒暄和重复。用第三人称客观叙述，不超过 %d 字，直接输出摘要本身，不要任何前缀。"
)


class MemorySummarizer:
    """按角色把过期消息压缩成摘要（断点续压，按 id 顺序）。"""

    def __init__(self, runtime: Any):
        self.rt = runtime

    # ------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.rt.config.get("memory.%s" % key, default)

    def enabled(self) -> bool:
        return bool(self._cfg("summarize_enabled", True))

    def context_window_days(self) -> int:
        try:
            return max(1, int(self._cfg("context_window_days", 7) or 7))
        except (TypeError, ValueError):
            return 7

    def summary_batch(self) -> int:
        try:
            return max(10, int(self._cfg("summary_batch", 60) or 60))
        except (TypeError, ValueError):
            return 60

    def max_summary_chars(self) -> int:
        try:
            return max(50, int(self._cfg("summary_max_chars", 300) or 300))
        except (TypeError, ValueError):
            return 300

    # ------------------------------------------------------------ 核心
    async def maybe_summarize(self, character_id: str, max_batches: int = 1) -> int:
        """检查并压缩一个角色的过期消息，返回本轮生成的摘要条数。"""
        character_id = str(character_id or "")
        if not character_id or not self.enabled():
            return 0
        db = self.rt.db
        boundary_at = self._window_boundary()
        produced = 0
        for _ in range(max(1, int(max_batches))):
            covered_to = await crud.last_summary_covered(db, character_id)
            pending = await crud.unsummarized_old_messages(
                db, character_id, boundary_at, covered_to, limit=self.summary_batch()
            )
            if not pending:
                break
            batch_text = self._render_batch(pending)
            try:
                engine = self.rt.engine
                llm = engine.llm
                content = (await llm.chat(self._summarize_messages(batch_text))).strip()
                if not content:
                    raise RuntimeError("模型返回空摘要")
                content = content[: self.max_summary_chars() * 2]
            except Exception as exc:
                log.info("角色 [%s] 对话压缩跳过（%s），下次重试", character_id, exc)
                break
            covered_to = int(pending[-1].get("id") or 0)
            await crud.insert_summary(db, character_id, covered_to, content)
            await crud.mark_messages_summarized(
                db, [int(row.get("id") or 0) for row in pending if row.get("id")]
            )
            produced += 1
            log.info(
                "角色 [%s] 已压缩 %d 条历史消息为摘要（覆盖到 id=%d，%d 字）",
                character_id, len(pending), covered_to, len(content),
            )
        return produced

    async def scan_all(self, max_batches: int = 1) -> int:
        """周期任务：扫描所有有消息的角色。"""
        if not self.enabled():
            return 0
        db = self.rt.db
        try:
            rows = await db.fetchall(
                "SELECT DISTINCT character_id FROM messages WHERE character_id != ''"
            )
        except Exception:
            return 0
        total = 0
        for row in rows:
            try:
                total += await self.maybe_summarize(str(row.get("character_id") or ""), max_batches=max_batches)
            except Exception as exc:  # pragma: no cover - 单个角色失败不影响其它
                log.warning("压缩角色 [%s] 失败：%s", row.get("character_id"), exc)
        return total

    # ------------------------------------------------------------ 辅助
    def _window_boundary(self) -> str:
        """上下文窗口边界（ISO 时间）：早于它的消息才需要压缩。"""
        import datetime as dt

        moment = dt.datetime.now() - dt.timedelta(days=self.context_window_days())
        return moment.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _render_batch(rows: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for row in rows:
            stamp = str(row.get("created_at") or "")[:16].replace("T", " ")
            role = "用户" if str(row.get("role")) == "user" else "角色"
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            lines.append("%s %s：%s" % (stamp, role, content[:200]))
        return "\n".join(lines)

    def _summarize_messages(self, batch_text: str) -> List[Dict[str, str]]:
        return [
            {
                "role": "system",
                "content": "你是聊天记录整理器。",
            },
            {
                "role": "user",
                "content": SUMMARY_PROMPT % self.max_summary_chars() + "\n\n" + batch_text,
            },
        ]


__all__ = ["MemorySummarizer"]
