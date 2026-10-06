"""收发的媒体文件落地与清理。

统一放在 ``data/media/`` 下：
* ``inbox/``   用户发来的图片 / 语音（下载后）
* ``outbox/``  机器人要发出去的图 / 语音（生成 / 合成后）

文件命名带时间戳，避免覆盖；超过 ``media.temp_days`` 的自动清理。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

from common.logging_setup import get_logger
from common.paths import data_dir

log = get_logger("bot.media.store")

INBOX = "inbox"
OUTBOX = "outbox"


def media_root() -> Path:
    path = data_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sub(name: str) -> Path:
    path = media_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_inbox(data: bytes, ext: str) -> Path:
    """把用户发来的媒体存到 inbox，返回文件路径。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = _sub(INBOX) / ("%s_%s%s" % (stamp, _rand4(), _ext(ext)))
    path.write_bytes(data)
    log.info("用户媒体已落地：%s（%d 字节）", path.name, len(data))
    return path


def save_outbox(data: bytes, ext: str) -> Path:
    """把机器人要发的媒体存到 outbox，返回文件路径。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = _sub(OUTBOX) / ("%s_%s%s" % (stamp, _rand4(), _ext(ext)))
    path.write_bytes(data)
    log.info("机器人媒体已落地：%s（%d 字节）", path.name, len(data))
    return path


def _rand4() -> str:
    import os

    return os.urandom(2).hex()


def _ext(ext: str) -> str:
    text = (ext or "").strip().lstrip(".").lower()
    return text or "bin"


def cleanup(keep_days: int = 3) -> int:
    """删除超过 ``keep_days`` 天的媒体文件，返回删除数量。"""
    if keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for sub in (INBOX, OUTBOX):
        base = _sub(sub)
        for path in base.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                continue
    if removed:
        log.info("清理过期媒体文件 %d 个（保留 %d 天）", removed, keep_days)
    return removed


def list_recent(sub: str, limit: int = 20) -> List[Path]:
    base = _sub(sub)
    items = [p for p in base.iterdir() if p.is_file()]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return items[: max(0, limit)]


__all__ = ["INBOX", "OUTBOX", "cleanup", "list_recent", "media_root", "save_inbox", "save_outbox"]
