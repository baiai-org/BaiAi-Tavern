"""异步小工具。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class LoopSafeLock:
    """绑定到「当前正在运行的事件循环」的互斥锁。

    为什么需要它？
    --------------
    Python 3.9 的 ``asyncio.Lock()`` 在**构造时**就会绑定当时的事件循环
    （内部调用 ``asyncio.get_event_loop()``）。如果锁是在事件循环启动之前
    创建的（例如在进程初始化阶段构造 Runtime / 数据库对象），它会被绑定到
    一个永远不会运行的循环上；之后在协程中加锁并等待时就会抛出
    ``got Future <Future pending> attached to a different loop``。

    Python 3.10+ 改为惰性绑定，但为了让同一份代码在 3.9 与 3.10+ 上都可靠，
    统一使用本类：锁在第一次真正加锁时才创建/校正到当前循环。
    """

    def __init__(self) -> None:
        self._lock: Optional[asyncio.Lock] = None
        self._acquired: Optional[asyncio.Lock] = None

    def _lock_for_running_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._lock
        bound = getattr(lock, "_loop", None) if lock is not None else None
        if lock is None or (bound is not None and bound is not loop):
            # 旧锁绑定在别的（通常是已经停止/关闭的）循环上，重新创建
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    def locked(self) -> bool:
        lock = self._lock
        return bool(lock is not None and lock.locked())

    async def acquire(self) -> bool:
        lock = self._lock_for_running_loop()
        await lock.acquire()
        self._acquired = lock
        return True

    def release(self) -> None:
        lock = self._acquired if self._acquired is not None else self._lock
        self._acquired = None
        if lock is not None and lock.locked():
            lock.release()

    async def __aenter__(self) -> "LoopSafeLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<LoopSafeLock locked=%s>" % self.locked()


__all__ = ["LoopSafeLock"]
