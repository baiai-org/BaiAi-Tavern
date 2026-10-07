"""主动消息触发条件判断（纯函数，便于单测与解释“为什么没发”）。

每个检查函数返回 :class:`Decision`，包含是否允许以及人类可读的原因，
调度器会把原因写入日志，GUI 也能看到“跳过原因”，方便用户排查。
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from common.utils import (
    humanize_duration,
    in_hhmm_range,
    in_minute_range,
    minutes_of_day,
    now,
    safe_float,
    safe_int,
    seconds_since,
)

TRIGGER_LABELS = {
    "scheduled": "定时触发",
    "idle": "空闲触发",
    "random": "随机触发",
    "manual": "手动触发",
    "startup": "启动触发",
}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason}


def _ok() -> Decision:
    return Decision(True, "")


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


# =========================================================== 时间窗口 ========
def check_proactive_enabled(config: Any) -> Decision:
    if not bool(config.get("proactive.enabled", True)):
        return _deny("主动消息总开关已关闭")
    return _ok()


def check_active_hours(config: Any, when: Optional[dt.datetime] = None) -> Decision:
    section = config.get("proactive.active_hours", {}) or {}
    if not bool(section.get("enabled", True)):
        return _ok()
    start = str(section.get("start", "08:00"))
    end = str(section.get("end", "23:00"))
    moment = when or now()
    if in_hhmm_range(moment, start, end):
        return _ok()
    return _deny("当前 %s 不在活跃时段 %s-%s 内" % (moment.strftime("%H:%M"), start, end))


def check_dnd(config: Any, when: Optional[dt.datetime] = None) -> Decision:
    section = config.get("proactive.dnd_hours", {}) or {}
    if not bool(section.get("enabled", True)):
        return _ok()
    start = str(section.get("start", "23:00"))
    end = str(section.get("end", "08:00"))
    moment = when or now()
    if in_hhmm_range(moment, start, end):
        return _deny("当前处于免打扰时段 %s-%s" % (start, end))
    return _ok()


# =========================================================== 频率限制 ========
def check_global_limit(config: Any, today_total: int) -> Decision:
    limit = safe_int(config.get("proactive.global_daily_limit", 10), 10)
    if limit <= 0:
        return _ok()
    if int(today_total) >= limit:
        return _deny("今日主动消息已达上限 %d 条（V0.2.2 起每个机器人独立计算）" % limit)
    return _ok()


def check_min_interval(config: Any, last_sent_at: Optional[str], when: Optional[dt.datetime] = None) -> Decision:
    interval = safe_int(config.get("proactive.min_interval_minutes", 30), 30)
    if interval <= 0:
        return _ok()
    elapsed = seconds_since(last_sent_at)
    if elapsed is None:
        return _ok()
    if elapsed < interval * 60:
        remaining = interval * 60 - elapsed
        return _deny("距上次主动消息仅 %s，需再等 %s" % (humanize_duration(elapsed), humanize_duration(remaining)))
    return _ok()


def check_idle(config: Any, last_user_message_at: Optional[str], when: Optional[dt.datetime] = None) -> Decision:
    """空闲触发的前置条件：用户超过 N 小时没有发言。"""
    hours = safe_float(config.get("proactive.idle_hours", 6), 6.0)
    if hours <= 0:
        return _ok()
    elapsed = seconds_since(last_user_message_at)
    if elapsed is None:
        return _ok()  # 还没有任何用户消息记录，视为长期空闲
    if elapsed >= hours * 3600:
        return _ok()
    remaining = hours * 3600 - elapsed
    return _deny("用户 %s 前才说过话，空闲不足 %.1f 小时（还差 %s）" % (
        humanize_duration(elapsed),
        hours,
        humanize_duration(remaining),
    ))


def character_over_limit(config: Any, per_character_counts: Dict[str, int], character_id: str) -> bool:
    limit = safe_int(config.get("proactive.per_character_daily_limit", 3), 3)
    if limit <= 0:
        return False
    return int(per_character_counts.get(str(character_id), 0)) >= limit


def check_probability(config: Any, force: bool = False, rng: Optional[random.Random] = None) -> Decision:
    if force:
        return _ok()
    probability = safe_float(config.get("proactive.probability", 0.7), 0.7)
    probability = max(0.0, min(1.0, probability))
    if probability >= 1.0:
        return _ok()
    if probability <= 0.0:
        return _deny("触发概率配置为 0")
    rng = rng or random
    if rng.random() <= probability:
        return _ok()
    return _deny("本次未通过概率判定（概率 %.0f%%）" % (probability * 100))


# =========================================================== 角色选择 ========
def filter_candidates(
    candidates: Sequence[Dict[str, Any]],
    config: Any,
    per_character_counts: Dict[str, int],
    last_character_id: Optional[str] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """过滤掉已达单角色上限的角色，并按需排除上次发言的角色。"""
    pool = [dict(item) for item in candidates]
    if not force:
        pool = [
            item
            for item in pool
            if not character_over_limit(config, per_character_counts, str(item.get("id", "")))
        ]
    if bool(config.get("proactive.avoid_repeat", True)) and len(pool) > 1 and last_character_id:
        filtered = [item for item in pool if str(item.get("id")) != str(last_character_id)]
        if filtered:
            pool = filtered
    return pool


def pick_character(
    candidates: Sequence[Dict[str, Any]],
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    rng = rng or random
    return dict(rng.choice(list(candidates)))


def weighted_pick_character(
    candidates: Sequence[Dict[str, Any]],
    per_character_counts: Dict[str, int],
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """偏向今日发言较少的角色，让角色出场更均衡。"""
    if not candidates:
        return None
    rng = rng or random
    weights = []
    for item in candidates:
        used = int(per_character_counts.get(str(item.get("id", "")), 0))
        weights.append(1.0 / (1.0 + used))
    total = sum(weights)
    threshold = rng.random() * total
    cursor = 0.0
    for item, weight in zip(candidates, weights):
        cursor += weight
        if cursor >= threshold:
            return dict(item)
    return dict(candidates[-1])


# =========================================================== 随机时间 ========
def next_active_datetime(config: Any, moment: Optional[dt.datetime] = None) -> dt.datetime:
    """把时间点校正到活跃时段内。"""
    section = config.get("proactive.active_hours", {}) or {}
    target = moment or now()
    if not bool(section.get("enabled", True)):
        return target
    start = minutes_of_day(section.get("start", "08:00"), 8 * 60)
    end = minutes_of_day(section.get("end", "23:00"), 23 * 60)
    current = target.hour * 60 + target.minute
    if in_minute_range(current, start, end):
        return target
    candidate = target.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if candidate <= target:
        candidate += dt.timedelta(days=1)
    return candidate


def next_random_time(
    config: Any,
    when: Optional[dt.datetime] = None,
    rng: Optional[random.Random] = None,
) -> dt.datetime:
    """计算下一次“随机间隔触发”的时间点。"""
    rng = rng or random
    minimum = safe_int(config.get("proactive.random_min_interval_minutes", 120), 120)
    maximum = safe_int(config.get("proactive.random_max_interval_minutes", 300), 300)
    minimum = max(1, minimum)
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    base = when or now()
    delay = rng.uniform(minimum, maximum)
    target = base + dt.timedelta(minutes=delay)
    target = next_active_datetime(config, target)
    return target + dt.timedelta(seconds=rng.uniform(0, 300))


def next_scheduled_time(times: Sequence[str], when: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    moments = sorted(minutes_of_day(item, -1) for item in times or [])
    moments = [item for item in moments if item >= 0]
    if not moments:
        return None
    base = when or now()
    current = base.hour * 60 + base.minute
    for moment in moments:
        if moment > current:
            return base.replace(hour=moment // 60, minute=moment % 60, second=0, microsecond=0)
    first = moments[0]
    candidate = base.replace(hour=first // 60, minute=first % 60, second=0, microsecond=0)
    return candidate + dt.timedelta(days=1)


def trigger_label(trigger_type: str) -> str:
    return TRIGGER_LABELS.get(trigger_type, trigger_type)
