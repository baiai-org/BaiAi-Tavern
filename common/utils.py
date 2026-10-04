"""通用小工具：时间解析、格式化、文本处理。"""

from __future__ import annotations

import datetime as _dt
import random
import re
from typing import Any, Optional, Tuple

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")


def now() -> _dt.datetime:
    """当前本地时间（naive）。全项目统一使用本地时间，便于“今日数量”与免打扰判断。"""
    return _dt.datetime.now()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def today_str(when: Optional[_dt.datetime] = None) -> str:
    return (when or now()).strftime("%Y-%m-%d")


def parse_hhmm(value: Any, default: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
    """解析 ``"09:30"`` / ``"9：30"`` 为 ``(hour, minute)``，失败时返回默认值。

    也接受 ``datetime`` / ``time`` 对象（内部统一用「当天第几分钟」做时段判断）。
    """
    if isinstance(value, _dt.datetime) or isinstance(value, _dt.time):
        return value.hour % 24, value.minute % 60
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]) % 24, int(value[1]) % 60
        except Exception:
            return default
    match = _TIME_RE.match(str(value or ""))
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def format_hhmm(value: Any, default: str = "00:00") -> str:
    hour, minute = parse_hhmm(value, (-1, -1))
    if hour < 0:
        return default
    return "%02d:%02d" % (hour, minute)


def minutes_of_day(value: Any, default: int = 0) -> int:
    hour, minute = parse_hhmm(value, (-1, -1))
    if hour < 0:
        return default
    return hour * 60 + minute


def in_minute_range(current: int, start: int, end: int) -> bool:
    """判断 ``current`` 是否落在 [start, end) 区间内，支持跨零点（如 23:00 -> 08:00）。"""
    current %= 1440
    start %= 1440
    end %= 1440
    if start == end:
        return True  # 起止相同视为“全天”
    if start < end:
        return start <= current < end
    return current >= start or current < end


def in_hhmm_range(when: Any, start: Any, end: Any) -> bool:
    return in_minute_range(minutes_of_day(when), minutes_of_day(start), minutes_of_day(end))


def seconds_since(timestamp: Optional[str]) -> Optional[float]:
    """ISO 时间戳到现在的秒数；无法解析时返回 None。"""
    if not timestamp:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(timestamp))
    except Exception:
        return None
    return (now() - parsed).total_seconds()


def humanize_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时 %d 分钟" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d 天" % (seconds // 86400)


def truncate(text: Any, limit: int = 120, suffix: str = "…") -> str:
    text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").strip()
    if limit and len(text) > limit:
        return text[: max(0, limit - len(suffix))] + suffix
    return text


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def jitter(seconds: float, ratio: float = 0.3) -> float:
    """在给定秒数附近加入 ±ratio 的随机抖动。"""
    if seconds <= 0:
        return 0.0
    delta = seconds * ratio
    return max(0.0, random.uniform(seconds - delta, seconds + delta))
