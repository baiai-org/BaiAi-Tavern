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
) -> int:
    return await db.insert(
        "messages",
        {
            "character_id": character_id,
            "role": role,
            "content": content,
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


async def proactive_count_today(db: Database, character_id: Optional[str] = None) -> int:
    if character_id:
        return int(
            await db.scalar(
                "SELECT COUNT(*) AS n FROM proactive_log "
                "WHERE status = 'sent' AND character_id = ? AND substr(sent_at, 1, 10) = ?",
                (character_id, today_str()),
                default=0,
            )
            or 0
        )
    return int(
        await db.scalar(
            "SELECT COUNT(*) AS n FROM proactive_log "
            "WHERE status = 'sent' AND substr(sent_at, 1, 10) = ?",
            (today_str(),),
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


async def last_proactive(db: Database, character_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if character_id:
        return await db.fetchone(
            "SELECT * FROM proactive_log WHERE character_id = ? AND status = 'sent' "
            "ORDER BY id DESC LIMIT 1",
            (character_id,),
        )
    return await db.fetchone(
        "SELECT * FROM proactive_log WHERE status = 'sent' ORDER BY id DESC LIMIT 1"
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
