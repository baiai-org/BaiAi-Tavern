"""日志初始化：同时输出到控制台与按天滚动的日志文件。

日志文件按天保存：当天写 ``data/logs/<name>.log``，每天午夜滚动为
``<name>.log.YYYY-MM-DD``，只保留最近 ``backup_count`` 天（默认 3 天），
更早的日志文件自动删除——日志不会无限增长。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from .paths import logs_dir

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured: set = set()


def setup_logging(
    name: str = "app",
    filename: Optional[str] = None,
    level: str = "INFO",
    console: bool = True,
    max_bytes: int = 2097152,
    backup_count: int = 3,
    force: bool = False,
) -> logging.Logger:
    """配置根 logger。

    ``name`` 同时决定默认日志文件名（``data/logs/<name>.log``）。
    日志按天分文件保存，只保留最近 ``backup_count`` 天（默认 3 天），
    超出的旧日志文件自动删除。

    ``max_bytes`` 保留参数以兼容旧配置，按天滚动后不再使用。
    重复调用默认是幂等的，除非 ``force=True``。
    """
    root = logging.getLogger()
    if name in _configured and not force:
        return logging.getLogger(name)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        log_file = logs_dir() / (filename or ("%s.log" % name))
        # 按天滚动：每天午夜切换文件（<name>.log.YYYY-MM-DD），
        # backupCount=3 → 今天 + 最近 3 天，更早的旧文件自动删除
        file_handler = logging.handlers.TimedRotatingFileHandler(
            str(log_file),
            when="midnight",
            interval=1,
            backupCount=int(backup_count),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as exc:  # pragma: no cover - 磁盘不可写时退化为仅控制台
        print("无法创建日志文件: %s" % exc, file=sys.stderr)

    if console and sys.stdout is not None:
        try:
            # Windows 控制台默认是 GBK，中文日志会乱码或抛 UnicodeEncodeError
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured.add(name)
    return logging.getLogger(name)


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(name)


def tail_file(path: Path, lines: int = 200) -> list:
    """读取文本文件末尾 N 行（日志查看页面使用）。"""
    if not path.exists():
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines + 1:
                step = min(block, size)
                size -= step
                handle.seek(size)
                data = handle.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-lines:]
    except Exception:
        return []
