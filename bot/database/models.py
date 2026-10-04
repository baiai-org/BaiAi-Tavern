"""数据库表结构与建表语句。

时间字段统一使用本地时间的 ISO 字符串（``YYYY-MM-DDTHH:MM:SS``），
便于按 ``substr(created_at, 1, 10)`` 做“今日”统计。
"""

from __future__ import annotations

from typing import List

SCHEMA_VERSION = 4

SCHEMA_STATEMENTS: List[str] = [
    # ------------------------------------------------------------ 元信息
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # ----------------------------------------------------------- 角色表
    """
    CREATE TABLE IF NOT EXISTS characters (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        description   TEXT DEFAULT '',
        personality   TEXT DEFAULT '',
        scenario      TEXT DEFAULT '',
        first_mes     TEXT DEFAULT '',
        mes_example   TEXT DEFAULT '',
        system_prompt TEXT DEFAULT '',
        creator_notes TEXT DEFAULT '',
        tags          TEXT DEFAULT '',
        avatar_path   TEXT DEFAULT '',
        source_path   TEXT DEFAULT '',
        card_spec     TEXT DEFAULT '',
        greeting_used INTEGER DEFAULT 0,
        enabled       INTEGER DEFAULT 1,
        sort_order    INTEGER DEFAULT 0,
        created_at    TEXT DEFAULT '',
        updated_at    TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_characters_enabled ON characters(enabled)",
    # ----------------------------------------------------------- 消息表
    """
    CREATE TABLE IF NOT EXISTS messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        character_id  TEXT NOT NULL,
        role          TEXT NOT NULL,
        content       TEXT NOT NULL,
        is_proactive  INTEGER DEFAULT 0,
        created_at    TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_character ON messages(character_id, id)",
    # --------------------------------------------------------- 长期记忆表
    """
    CREATE TABLE IF NOT EXISTS memories (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        character_id TEXT NOT NULL,
        content      TEXT NOT NULL,
        weight       REAL DEFAULT 1.0,
        created_at   TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_character ON memories(character_id)",
    # --------------------------------------------------------- 主动消息日志
    """
    CREATE TABLE IF NOT EXISTS proactive_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        character_id TEXT DEFAULT '',
        character_name TEXT DEFAULT '',
        bot_id       TEXT DEFAULT '',
        bot_name     TEXT DEFAULT '',
        trigger_type TEXT DEFAULT 'manual',
        content      TEXT DEFAULT '',
        status       TEXT DEFAULT 'sent',
        sent_at      TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_proactive_sent ON proactive_log(sent_at)",
    # ------------------------------------------------------------- 用户状态
    """
    CREATE TABLE IF NOT EXISTS user_state (
        id                   INTEGER PRIMARY KEY CHECK (id = 1),
        last_user_message_at TEXT DEFAULT '',
        last_self_id         TEXT DEFAULT '',
        updated_at           TEXT DEFAULT ''
    )
    """,
    # --------------------------------------------------------------- 键值表
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )
    """,
]

# 旧版本数据库升级用：表已存在时 CREATE TABLE IF NOT EXISTS 不会补字段，
# 因此需要显式 ADD COLUMN（列已存在时跳过，重复执行安全）。
MIGRATION_COLUMNS: List[tuple] = [
    ("proactive_log", "bot_id", "TEXT DEFAULT ''"),
    ("proactive_log", "bot_name", "TEXT DEFAULT ''"),
]


__all__ = ["MIGRATION_COLUMNS", "SCHEMA_STATEMENTS", "SCHEMA_VERSION"]

