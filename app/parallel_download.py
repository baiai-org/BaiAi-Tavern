"""多线程（分段 Range）下载（V0.2.2）。

旧版是单连接顺序下载，官方 GitHub Release 限速时「安装与更新」的下载慢得
无法接受。现在：

1. 先 ``HEAD`` / ``Range: bytes=0-0`` 探测服务器是否支持分段下载与文件总大小；
2. 支持则把文件切成 ``max_workers`` 段（每段 >= ``min_range_bytes``），
   每个线程独立 ``GET Range`` 写入同一份预分配（sparse）文件的对应偏移；
   段太大时自动减少并发数（小文件开 16 个线程反而互相拖累）；
3. 服务器不支持分段（或探测失败）时回退到单连接整文件下载。

进度回调 ``on_progress(done_bytes, total_bytes)``。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from common.logging_setup import get_logger

log = get_logger("app.updater")

# 默认 16 线程（用户要求「下载线程拉满」）；小文件按大小自动收缩
MAX_WORKERS = 16
MIN_RANGE_BYTES = 512 * 1024  # 每段最小 512KB，低于它就没必要再开更多线程

ProgressFn = Callable[[int, int], None]


def probe_url(
    client: httpx.Client,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], bool]:
    """探测 URL：返回 ``(总字节数或 None, 是否支持 Range)``。

    依次尝试 ``HEAD`` → 带 ``Range: bytes=0-0`` 的 ``GET``；都不支持时返回
    ``(None, False)``，调用方据此回退整文件下载。
    """
    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", "Mozilla/5.0 BaiAi-Tavern/0.2.2")

    # 1) HEAD
    try:
        response = client.head(url, headers=req_headers, follow_redirects=True)
        if response.status_code < 400:
            total = _int_header(response.headers.get("Content-Length"))
            # 注意：Accept-Ranges 的标准值是 ``bytes``（不带等号），
            # 写成 "bytes=" 会永远判定为不支持分段
            supports = "bytes" in str(response.headers.get("Accept-Ranges", "")).lower()
            if total is not None and supports:
                return total, True
            if total is not None:
                return total, False
    except Exception as exc:
        log.debug("HEAD 探测失败（试 Range GET）：%s", exc)

    # 2) Range GET 探测
    try:
        probe = dict(req_headers)
        probe["Range"] = "bytes=0-0"
        response = client.get(url, headers=probe, follow_redirects=True)
        if response.status_code == 206:
            total = None
            cr = str(response.headers.get("Content-Range", ""))
            if "/" in cr:
                total = _int_header(cr.rsplit("/", 1)[-1])
            return total, True
        if response.status_code < 400:
            return _int_header(response.headers.get("Content-Length")), False
    except Exception as exc:
        log.debug("Range 探测失败（按不支持分段处理）：%s", exc)
    return None, False


def _int_header(value: Optional[str]) -> Optional[int]:
    try:
        number = int(str(value).strip())
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def plan_ranges(total: int, workers: int) -> List[Tuple[int, int]]:
    """把 ``total`` 字节均分成尽量多段，返回 ``[(start, end), ...]``（闭区间）。

    段数 = min(workers, total // MIN_RANGE_BYTES)；小文件自动退化成 1 段。
    """
    if total <= 0:
        return []
    count = max(1, min(max(1, int(workers)), int(total) // MIN_RANGE_BYTES or 1))
    count = min(count, int(total) or 1)
    size = (total + count - 1) // count
    ranges: List[Tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + size - 1, total - 1)
        ranges.append((start, end))
        start = end + 1
    return ranges


def download_file(
    url: str,
    dest: Path,
    on_progress: Optional[ProgressFn] = None,
    max_workers: int = MAX_WORKERS,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Path:
    """把 ``url`` 下载到 ``dest``（先写 ``.part`` 再改名）。返回最终路径。

    支持分段时多线程并发下载；否则单连接顺序写。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    client = httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        total, supports_range = probe_url(client, url, headers)
        if supports_range and total and total > 0 and max_workers > 1:
            ranges = plan_ranges(total, max_workers)
            log.info(
                "并行下载 %s：%d 字节，%d 个分段（每段约 %.1f KB）",
                url, total, len(ranges), (total / len(ranges)) / 1024,
            )
            _download_ranges(client, url, part, total, ranges, on_progress, headers)
        else:
            log.info("整文件下载 %s（服务器不支持分段或文件太小）", url)
            _download_whole(client, url, part, total, on_progress, headers)
        part.replace(dest)
        return dest
    finally:
        client.close()
        if part.exists():
            try:
                part.unlink()
            except Exception:
                pass


# --------------------------------------------------------------------- 内部
class _Progress:
    """线程安全的已下载字节计数。"""

    def __init__(self, total: int):
        self.total = int(total or 0)
        self.done = 0
        self._lock = threading.Lock()
        self._last_report = 0.0

    def add(self, count: int, on_progress: Optional[ProgressFn]) -> None:
        import time

        with self._lock:
            self.done += count
            now = time.time()
            if now - self._last_report > 0.2 or self.done >= self.total:
                self._last_report = now
                snapshot = self.done
        if on_progress is not None and snapshot:
            try:
                on_progress(snapshot, self.total)
            except Exception:  # pragma: no cover - 回调异常不该中断下载
                log.debug("下载进度回调异常（忽略）")


def _download_ranges(
    client: httpx.Client,
    url: str,
    part: Path,
    total: int,
    ranges: List[Tuple[int, int]],
    on_progress: Optional[ProgressFn],
    headers: Optional[Dict[str, str]],
) -> None:
    # 预分配 sparse 文件（NTFS 上只占元数据，瞬间完成）
    with open(part, "wb") as handle:
        handle.truncate(total)
    progress = _Progress(total)
    errors: List[str] = []
    errors_lock = threading.Lock()

    def worker(index: int, start: int, end: int) -> None:
        offset = 0
        attempt = 0
        while offset <= (end - start):
            attempt += 1
            try:
                req_headers = dict(headers or {})
                req_headers["Range"] = "bytes=%d-%d" % (start + offset, end)
                with client.stream("GET", url, headers=req_headers) as response:
                    if response.status_code not in (200, 206):
                        raise RuntimeError("HTTP %d" % response.status_code)
                    with open(part, "r+b") as handle:
                        handle.seek(start + offset)
                        for chunk in response.iter_bytes(64 * 1024):
                            handle.write(chunk)
                            progress.add(len(chunk), on_progress)
                break
            except Exception as exc:
                # 重试 3 次，仍失败则该段报错（整体失败由上层处理）
                if attempt >= 3:
                    with errors_lock:
                        errors.append("分段 %d 下载失败：%s" % (index, exc))
                    log.warning("分段 %d 下载失败（重试 3 次后放弃）：%s", index, exc)
                    return
                log.info("分段 %d 下载中断，重试：%s", index, exc)

    with ThreadPoolExecutor(max_workers=max(1, len(ranges))) as pool:
        futures = [
            pool.submit(worker, index, start, end)
            for index, (start, end) in enumerate(ranges)
        ]
        for future in futures:
            future.result()
    if errors:
        raise RuntimeError("；".join(errors[:3]))
    if progress.done != total:
        raise RuntimeError("下载不完整：%d / %d 字节" % (progress.done, total))


def _download_whole(
    client: httpx.Client,
    url: str,
    part: Path,
    total: Optional[int],
    on_progress: Optional[ProgressFn],
    headers: Optional[Dict[str, str]],
) -> None:
    progress = _Progress(total or 0)
    with client.stream("GET", url, headers=headers or {}) as response:
        if response.status_code >= 400:
            raise RuntimeError("HTTP %d" % response.status_code)
        with open(part, "wb") as handle:
            for chunk in response.iter_bytes(64 * 1024):
                handle.write(chunk)
                progress.add(len(chunk), on_progress)


__all__ = ["MAX_WORKERS", "download_file", "plan_ranges", "probe_url"]
