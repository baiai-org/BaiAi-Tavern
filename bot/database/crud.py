"""领域数据访问函数。

所有函数均为 async，第一个参数为 :class:`bot.database.Database` 实例。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from common.utils import iso_now, today_str

from . import Database

CHARACTER_FIELDS = (
    "id",
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
    "creator_notes",
    "tags",
    "avatar_path",
    "source_path",
    "card_spec",
    "tts_voice",
    "tts_rate",
    "tts_pitch",
    "tts_volume",
    "tts_speed",
    "enabled",
    "sort_order",
)


# ============================================================== 角色 =========
def _normalize_tags(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    if value is None:
        return "[]"
    return str(value)


def tags_to_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


async def insert_character(db: Database, data: Dict[str, Any]) -> Dict[str, Any]:
    payload = {key: data.get(key) for key in CHARACTER_FIELDS if key in data}
    payload["tags"] = _normalize_tags(payload.get("tags"))
    payload["enabled"] = 1 if payload.get("enabled", 1) else 0
    payload.setdefault("sort_order", 0)
    payload["created_at"] = data.get("created_at") or iso_now()
    payload["updated_at"] = payload["created_at"]
    await db.insert("characters", payload)
    return await get_character(db, str(payload["id"])) or {}


async def upsert_character(db: Database, data: Dict[str, Any]) -> Dict[str, Any]:
    existing = await get_character(db, str(data.get("id", "")))
    if existing:
        await update_character(db, str(data["id"]), data)
        return await get_character(db, str(data["id"])) or {}
    return await insert_character(db, data)


async def list_characters(db: Database, enabled_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM characters"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY sort_order ASC, created_at ASC"
    return await db.fetchall(sql)


async def get_character(db: Database, character_id: str) -> Optional[Dict[str, Any]]:
    if not character_id:
        return None
    return await db.fetchone("SELECT * FROM characters WHERE id = ?", (character_id,))


async def get_character_by_name(db: Database, name: str) -> Optional[Dict[str, Any]]:
    return await db.fetchone("SELECT * FROM characters WHERE name = ? LIMIT 1", (name,))


async def update_character(
    db: Database, character_id: str, fields: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    allowed = {
        key: value for key, value in fields.items() if key in CHARACTER_FIELDS and key != "id"
    }
    if not allowed:
        return await get_character(db, character_id)
    if "tags" in allowed:
        allowed["tags"] = _normalize_tags(allowed["tags"])
    if "enabled" in allowed:
        allowed["enabled"] = 1 if allowed["enabled"] else 0
    allowed["updated_at"] = iso_now()
    await db.update("characters", allowed, "id = ?", (character_id,))
    return await get_character(db, character_id)


async def set_character_enabled(db: Database, character_id: str, enabled: bool) -> bool:
    result = await update_character(db, character_id, {"enabled": enabled})
    return result is not None


async def delete_character(db: Database, character_id: str) -> bool:
    cursor = await db.execute("DELETE FROM characters WHERE id = ?", (character_id,))
    await db.execute("DELETE FROM messages WHERE character_id = ?", (character_id,))
    await db.execute("DELETE FROM memories WHERE character_id = ?", (character_id,))
    return bool(cursor.rowcount)


async def count_characters(db: Database, enabled_only: bool = False) -> int:
    sql = "SELECT COUNT(*) AS n FROM characters"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return int(await db.scalar(sql, default=0) or 0)


# ============================================================== 消息 =========
async def add_message(
    db: Database,
    character_id: str,
    role: str,
    content: str,
    is_proactive: bool = False,
    created_at: Optional[str] = None,
    kind: str = "text",
    media_path: str = "",
) -> int:
    return await db.insert(
        "messages",
        {
            "character_id": character_id,
            "role": role,
            "content": content,
            "kind": kind or "text",
            "media_path": media_path or "",
            "is_proactive": 1 if is_proactive else 0,
            "created_at": created_at or iso_now(),
        },
    )


async def recent_messages(db: Database, character_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM messages WHERE character_id = ? ORDER BY id DESC LIMIT ?",
        (character_id, max(1, int(limit))),
    )
    rows.reverse()
    return rows


async def messages_in_window(
    db: Database,
    character_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    """按时间窗口取消息（V0.2.2 上下文分级：最近 N 天的原文）。

    ``since`` / ``until`` 是 ISO 时间字符串（含当天），按字典序比较即可。
    返回升序（旧 → 新）。
    """
    where = ["character_id = ?"]
    params: List[Any] = [character_id]
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at <= ?")
        params.append(until)
    # 取窗口内**最新**的 limit 条（DESC 取再反序）：旧实现按 ASC 取，
    # 窗口内消息超过 limit 时刚入库的最新消息会被截掉，导致"回复看不到当前消息"
    rows = await db.fetchall(
        "SELECT * FROM messages WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where),
        tuple(params + [max(1, int(limit))]),
    )
    rows.reverse()
    return list(rows)


async def search_messages(
    db: Database,
    character_id: str,
    limit: int = 300,
    search: str = "",
    month: str = "",
    day: str = "",
) -> List[Dict[str, Any]]:
    """按 月 / 天 / 关键词 检索对话记录（V0.2.2：按月按天查看）。

    ``month`` 形如 ``2026-10``，``day`` 形如 ``2026-10-06``；均按
    ``substr(created_at, 1, 7 / 10)`` 精确匹配。返回升序，但只保留**最新**
    的 limit 条（与 recent_messages 一致，避免旧消息把新记录挤出去）。
    """
    where = ["character_id = ?"]
    params: List[Any] = [character_id]
    if month:
        where.append("substr(created_at, 1, 7) = ?")
        params.append(str(month)[:7])
    if day:
        where.append("substr(created_at, 1, 10) = ?")
        params.append(str(day)[:10])
    keyword = str(search or "").strip()
    if keyword:
        where.append("content LIKE ?")
        params.append("%" + keyword.replace("%", "\\%").replace("_", "\\_") + "%")
    rows = await db.fetchall(
        "SELECT * FROM messages WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where),
        tuple(params + [max(1, int(limit))]),
    )
    rows.reverse()
    return list(rows)


async def message_months(db: Database, character_id: str) -> List[str]:
    """某角色有消息的月份列表（``2026-10``，新 → 旧），供界面下拉框。"""
    rows = await db.fetchall(
        "SELECT DISTINCT substr(created_at, 1, 7) AS m FROM messages "
        "WHERE character_id = ? AND created_at != '' ORDER BY m DESC",
        (character_id,),
    )
    return [str(row["m"]) for row in rows if row.get("m")]


async def message_days(db: Database, character_id: str, month: str = "") -> List[str]:
    """某角色在某月份（不限则全部）有消息的日期列表（新 → 旧）。"""
    if month:
        rows = await db.fetchall(
            "SELECT DISTINCT substr(created_at, 1, 10) AS d FROM messages "
            "WHERE character_id = ? AND substr(created_at, 1, 7) = ? AND created_at != '' "
            "ORDER BY d DESC",
            (character_id, str(month)[:7]),
        )
    else:
        rows = await db.fetchall(
            "SELECT DISTINCT substr(created_at, 1, 10) AS d FROM messages "
            "WHERE character_id = ? AND created_at != '' ORDER BY d DESC",
            (character_id,),
        )
    return [str(row["d"]) for row in rows if row.get("d")]


# ===================================================== 对话压缩摘要（V0.2.2） ===
async def last_summary_covered(db: Database, character_id: str) -> int:
    """该角色已有摘要覆盖到的最大 messages.id（0 = 还没有摘要）。"""
    value = await db.scalar(
        "SELECT MAX(covered_to) AS v FROM memory_summaries WHERE character_id = ?",
        (character_id,),
        default=0,
    )
    return int(value or 0)


async def unsummarized_old_messages(
    db: Database,
    character_id: str,
    before: str,
    covered_to: int,
    limit: int = 60,
) -> List[Dict[str, Any]]:
    """取「早于 ``before`` 且还没被摘要覆盖」的消息（升序，最多 ``limit`` 条），
    供压缩器分批处理。"""
    rows = await db.fetchall(
        "SELECT * FROM messages WHERE character_id = ? AND id > ? AND created_at != '' "
        "AND created_at < ? ORDER BY id ASC LIMIT ?",
        (character_id, int(covered_to), before, max(1, int(limit))),
    )
    return list(rows)


async def insert_summary(
    db: Database, character_id: str, covered_to: int, content: str
) -> int:
    return await db.insert(
        "memory_summaries",
        {
            "character_id": character_id,
            "covered_to": int(covered_to),
            "content": content,
            "created_at": iso_now(),
        },
    )


async def mark_messages_summarized(db: Database, message_ids: List[int]) -> int:
    if not message_ids:
        return 0
    marks = ",".join("?" for _ in message_ids)
    cursor = await db.execute(
        "UPDATE messages SET summarized = 1 WHERE id IN (%s)" % marks, tuple(message_ids)
    )
    return int(cursor.rowcount or 0)


async def recent_summaries(db: Database, character_id: str, limit: int = 8) -> List[Dict[str, Any]]:
    """该角色最近的若干条摘要（旧 → 新），供系统提示词。"""
    rows = await db.fetchall(
        "SELECT * FROM memory_summaries WHERE character_id = ? ORDER BY id DESC LIMIT ?",
        (character_id, max(1, int(limit))),
    )
    rows.reverse()
    return rows


async def update_last_message_media(
    db: Database, character_id: str, kind: str, media_path: str
) -> int:
    """把该角色最后一条 assistant 消息标记为带媒体（V0.2.2：记录图片/语音）。"""
    cursor = await db.execute(
        "UPDATE messages SET kind = ?, media_path = ? WHERE id = ("
        " SELECT MAX(id) FROM messages WHERE character_id = ? AND role = 'assistant'"
        ")",
        (kind or "text", media_path or "", character_id),
    )
    return int(cursor.rowcount or 0)


async def list_conversations(db: Database) -> List[Dict[str, Any]]:
    """按角色汇总会话，用于 GUI 的会话列表。"""
    return await db.fetchall(
        """
        SELECT c.id            AS character_id,
               c.name          AS name,
               c.avatar_path   AS avatar_path,
               c.enabled       AS enabled,
               COUNT(m.id)     AS message_count,
               MAX(m.created_at) AS last_at,
               (SELECT content FROM messages m2
                 WHERE m2.character_id = c.id ORDER BY m2.id DESC LIMIT 1) AS last_content,
               (SELECT role FROM messages m3
                 WHERE m3.character_id = c.id ORDER BY m3.id DESC LIMIT 1) AS last_role
          FROM characters c
          LEFT JOIN messages m ON m.character_id = c.id
         GROUP BY c.id
         ORDER BY last_at IS NULL, last_at DESC, c.sort_order ASC
        """
    )


async def clear_messages(db: Database, character_id: Optional[str] = None) -> int:
    if character_id:
        cursor = await db.execute("DELETE FROM messages WHERE character_id = ?", (character_id,))
    else:
        cursor = await db.execute("DELETE FROM messages")
    return int(cursor.rowcount or 0)


async def count_messages(db: Database, character_id: Optional[str] = None) -> int:
    if character_id:
        return int(
            await db.scalar(
                "SELECT COUNT(*) AS n FROM messages WHERE character_id = ?",
                (character_id,),
                default=0,
            )
            or 0
        )
    return int(await db.scalar("SELECT COUNT(*) AS n FROM messages", default=0) or 0)


# ============================================================ 长期记忆 =======
async def add_memory(
    db: Database, character_id: str, content: str, weight: float = 1.0
) -> int:
    content = (content or "").strip()
    if not content:
        return 0
    return await db.insert(
        "memories",
        {
            "character_id": character_id,
            "content": content[:500],
            "weight": float(weight),
            "created_at": iso_now(),
        },
    )


async def list_memories(db: Database, character_id: str) -> List[Dict[str, Any]]:
    return await db.fetchall(
        "SELECT * FROM memories WHERE character_id = ? ORDER BY id DESC",
        (character_id,),
    )


async def memory_exists(db: Database, character_id: str, content: str) -> bool:
    row = await db.fetchone(
        "SELECT id FROM memories WHERE character_id = ? AND content = ? LIMIT 1",
        (character_id, (content or "").strip()[:500]),
    )
    return row is not None


async def delete_memory(db: Database, memory_id: int) -> bool:
    cursor = await db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
    return bool(cursor.rowcount)


async def touch_memory(db: Database, memory_id: int, weight: float) -> None:
    await db.execute(
        "UPDATE memories SET weight = ? WHERE id = ?", (float(weight), int(memory_id))
    )


async def count_memories(db: Database, character_id: Optional[str] = None) -> int:
    if character_id:
        return int(
            await db.scalar(
                "SELECT COUNT(*) AS n FROM memories WHERE character_id = ?",
                (character_id,),
                default=0,
            )
            or 0
        )
    return int(await db.scalar("SELECT COUNT(*) AS n FROM memories", default=0) or 0)


# ========================================================== 主动消息日志 =====
async def log_proactive(
    db: Database,
    character_id: str,
    trigger_type: str,
    content: str = "",
    status: str = "sent",
    character_name: str = "",
    sent_at: Optional[str] = None,
    bot_id: str = "",
    bot_name: str = "",
) -> int:
    return await db.insert(
        "proactive_log",
        {
            "character_id": character_id or "",
            "character_name": character_name,
            "bot_id": bot_id or "",
            "bot_name": bot_name or "",
            "trigger_type": trigger_type,
            "content": content,
            "status": status,
            "sent_at": sent_at or iso_now(),
        },
    )


async def proactive_count_today(
    db: Database, character_id: Optional[str] = None, bot_id: Optional[str] = None
) -> int:
    """今日已发送的主动消息数。``character_id`` / ``bot_id`` 可叠加过滤
    （V0.2.2：多机器人各自独立计数，传入 bot_id 时只数该机器人的）。"""
    where = ["status = 'sent'"]
    params: List[Any] = []
    if character_id:
        where.append("character_id = ?")
        params.append(character_id)
    if bot_id:
        where.append("bot_id = ?")
        params.append(bot_id)
    where.append("substr(sent_at, 1, 10) = ?")
    params.append(today_str())
    return int(
        await db.scalar(
            "SELECT COUNT(*) AS n FROM proactive_log WHERE %s" % " AND ".join(where),
            tuple(params),
            default=0,
        )
        or 0
    )


async def proactive_counts_today_by_character(db: Database) -> Dict[str, int]:
    rows = await db.fetchall(
        "SELECT character_id, COUNT(*) AS n FROM proactive_log "
        "WHERE status = 'sent' AND substr(sent_at, 1, 10) = ? GROUP BY character_id",
        (today_str(),),
    )
    return {str(row["character_id"]): int(row["n"]) for row in rows}


async def last_proactive(
    db: Database, character_id: Optional[str] = None, bot_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """最近一条已发送的主动消息。可叠加 character_id / bot_id 过滤（V0.2.2）。"""
    where = ["status = 'sent'"]
    params: List[Any] = []
    if character_id:
        where.append("character_id = ?")
        params.append(character_id)
    if bot_id:
        where.append("bot_id = ?")
        params.append(bot_id)
    return await db.fetchone(
        "SELECT * FROM proactive_log WHERE %s ORDER BY id DESC LIMIT 1" % " AND ".join(where),
        tuple(params),
    )


async def recent_proactive_logs(db: Database, limit: int = 50) -> List[Dict[str, Any]]:
    return await db.fetchall(
        "SELECT * FROM proactive_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
    )


# ============================================================ 用户状态 =======
async def set_last_user_message(db: Database, timestamp: Optional[str] = None) -> None:
    stamp = timestamp or iso_now()
    await db.execute(
        "INSERT INTO user_state(id, last_user_message_at, updated_at) VALUES(1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_user_message_at = excluded.last_user_message_at, "
        "updated_at = excluded.updated_at",
        (stamp, stamp),
    )


async def get_last_user_message(db: Database) -> Optional[str]:
    value = await db.scalar(
        "SELECT last_user_message_at FROM user_state WHERE id = 1", default=None
    )
    return str(value) if value else None


async def set_last_self_id(db: Database, self_id: str) -> None:
    if not self_id:
        return
    await db.execute(
        "INSERT INTO user_state(id, last_self_id, updated_at) VALUES(1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_self_id = excluded.last_self_id, "
        "updated_at = excluded.updated_at",
        (str(self_id), iso_now()),
    )


async def get_last_self_id(db: Database) -> Optional[str]:
    value = await db.scalar("SELECT last_self_id FROM user_state WHERE id = 1", default=None)
    return str(value) if value else None


# ====================================================== 多机器人状态 =========
async def set_bot_state(db: Database, bot_id: str, key: str, value: Any) -> None:
    """按机器人保存运行时状态（学习到的 self_id / 最近对话对象等）。"""
    await set_setting(db, "bot:%s:%s" % (bot_id or "bot1", key), value)


async def get_bot_state(db: Database, bot_id: str, key: str, default: Any = "") -> Any:
    return await get_setting(db, "bot:%s:%s" % (bot_id or "bot1", key), default)


# ============================================================== 键值 =========
async def get_setting(db: Database, key: str, default: Any = None) -> Any:
    value = await db.scalar("SELECT value FROM settings WHERE key = ?", (key,), default=None)
    return default if value is None else value


async def set_setting(db: Database, key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )


# ============================================================== 统计 =========
async def stats_today(db: Database) -> Dict[str, Any]:
    """GUI 仪表盘用的今日统计。"""
    per_character = await proactive_counts_today_by_character(db)
    characters = await list_characters(db)
    name_map = {str(item["id"]): str(item["name"]) for item in characters}
    breakdown = [
        {
            "character_id": cid,
            "name": name_map.get(cid, cid or "未知角色"),
            "count": count,
        }
        for cid, count in sorted(per_character.items(), key=lambda kv: -kv[1])
    ]
    return {
        "date": today_str(),
        "proactive_total": await proactive_count_today(db),
        "proactive_by_character": breakdown,
        "character_total": len(characters),
        "character_enabled": len([c for c in characters if int(c.get("enabled") or 0) == 1]),
        "message_total": await count_messages(db),
        "memory_total": await count_memories(db),
    }
