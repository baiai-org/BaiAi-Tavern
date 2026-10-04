"""数据层：SQLite（aiosqlite）连接管理与建表。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

import aiosqlite

from common.async_utils import LoopSafeLock
from common.utils import iso_now

from .models import MIGRATION_COLUMNS, SCHEMA_STATEMENTS, SCHEMA_VERSION


class Database:
    """极简异步数据库封装。

    主动消息场景并发量很小，因此使用单连接 + 互斥锁，避免 SQLite 写锁竞争。
    锁使用 :class:`~common.async_utils.LoopSafeLock`，因为本对象可能在事件循环
    启动之前就被创建（Python 3.9 下普通 ``asyncio.Lock`` 会绑定到已失效的循环）。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = LoopSafeLock()

    # ------------------------------------------------------------- 生命周期
    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> "Database":
        if self._conn is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.commit()
        await self.init_schema()
        return self

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.commit()
            except Exception:
                pass
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Database":
        return await self.connect()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ---------------------------------------------------------------- 建表
    async def init_schema(self) -> None:
        assert self._conn is not None, "数据库尚未连接"
        for statement in SCHEMA_STATEMENTS:
            await self._conn.execute(statement)
        await self._migrate_columns()
        await self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await self._conn.commit()

    async def _migrate_columns(self) -> None:
        """给旧数据库补上后加的字段（多机器人需要 proactive_log.bot_id 等）。"""
        assert self._conn is not None
        for table, column, ddl in MIGRATION_COLUMNS:
            try:
                cursor = await self._conn.execute("PRAGMA table_info(%s)" % table)
                rows = await cursor.fetchall()
                await cursor.close()
                existing = {str(row[1]) for row in rows}
                if not existing:
                    continue  # 表还不存在（正常建表流程会创建）
                if column in existing:
                    continue
                await self._conn.execute(
                    "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl)
                )
            except Exception:  # pragma: no cover - 迁移失败不应阻塞启动
                pass
        await self._conn.commit()

    # ------------------------------------------------------------ 基础查询
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        assert self._conn is not None, "数据库尚未连接"
        async with self._lock:
            cursor = await self._conn.execute(sql, tuple(params))
            await self._conn.commit()
            return cursor

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        assert self._conn is not None, "数据库尚未连接"
        async with self._lock:
            await self._conn.executemany(sql, [tuple(item) for item in seq])
            await self._conn.commit()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        assert self._conn is not None, "数据库尚未连接"
        async with self._lock:
            cursor = await self._conn.execute(sql, tuple(params))
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if not row:
            return default
        value = next(iter(row.values()))
        return default if value is None else value

    # ------------------------------------------------------------ 便捷方法
    async def insert(self, table: str, data: dict) -> int:
        columns = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, columns, placeholders)
        cursor = await self.execute(sql, list(data.values()))
        return int(cursor.lastrowid or 0)

    async def update(self, table: str, data: dict, where: str, params: Sequence[Any]) -> int:
        assignments = ", ".join("%s = ?" % key for key in data)
        sql = "UPDATE %s SET %s WHERE %s" % (table, assignments, where)
        cursor = await self.execute(sql, list(data.values()) + list(params))
        return int(cursor.rowcount or 0)

    async def touch(self, table: str, record_id: Any) -> None:
        try:
            await self.execute(
                "UPDATE %s SET updated_at = ? WHERE id = ?" % table, (iso_now(), record_id)
            )
        except Exception:
            pass  # 表没有 updated_at 字段时忽略
