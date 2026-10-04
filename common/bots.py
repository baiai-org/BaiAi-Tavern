"""机器人（QQ 机器人应用）配置的解析与规范化。

本程序的「机器人」= 一个 QQ **官方机器人应用**（AppID / AppSecret），
每个机器人可以**绑定一个角色**（由用户指定），于是同一个程序可以同时跑多个
「机器人 + 角色」组合，各自有独立的凭据、发送目标和人格。

配置布局（向后兼容）
-------------------
* ``qq:`` 段就是**第 1 个机器人**的连接配置（历史字段全部保留在这里），
  另外多了三个身份字段：``name`` / ``enabled`` / ``character_id``；
* ``bots:`` 是**第 2..N 个机器人**，每一项与 ``qq:`` 段同构；
* ``bots`` 为空时，程序行为与旧版本完全一致（只有一个机器人）。

这样旧配置文件不需要任何迁移：用户升级后 ``qq:`` 段自动成为「机器人 1」。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

MODE_OFFICIAL = "official"
MODE_LABELS = {MODE_OFFICIAL: "QQ 官方机器人"}

PRIMARY_PREFIX = "qq"
BOTS_KEY = "bots"

# 每个机器人自己的身份字段（其余字段与 qq.* 同构）
IDENTITY_KEYS = ("name", "enabled", "character_id", "character_name")


def mode_label(mode: str) -> str:
    return MODE_LABELS[MODE_OFFICIAL]


def normalize_mode(mode: Any) -> str:
    """只有一个连接方式（官方机器人），历史配置里的 ``mode`` 一律视为官方。"""
    return MODE_OFFICIAL


class BotSpec:
    """一个机器人的配置视图（只读，纯数据，可安全地跨线程/进程传递）。"""

    def __init__(self, index: int, prefix: str, data: Dict[str, Any]):
        self.index = int(index)
        self.prefix = str(prefix)
        self.data: Dict[str, Any] = data or {}

    # ---------------------------------------------------------------- 身份
    @property
    def id(self) -> str:
        return str(self.data.get("id") or ("bot%d" % (self.index + 1)))

    @property
    def name(self) -> str:
        text = str(self.data.get("name") or "").strip()
        return text or "机器人 %d" % (self.index + 1)

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", True))

    @property
    def character_id(self) -> str:
        return str(self.data.get("character_id") or "").strip()

    @property
    def character_name(self) -> str:
        return str(self.data.get("character_name") or "").strip()

    # ---------------------------------------------------------------- 连接
    @property
    def mode(self) -> str:
        return normalize_mode(self.data.get("mode"))

    @property
    def mode_label(self) -> str:
        return mode_label(self.mode)

    @property
    def is_official(self) -> bool:
        return self.mode == MODE_OFFICIAL

    @property
    def is_primary(self) -> bool:
        return self.index == 0

    def official(self, key: str, default: Any = None) -> Any:
        value = (self.data.get("official") or {}).get(key, default)
        return default if value is None else value

    @property
    def official_config(self) -> Dict[str, Any]:
        return dict(self.data.get("official") or {})

    def qq(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key, default)
        return default if value is None else value

    # ---------------------------------------------------------------- 目标
    def target_summary(self) -> str:
        """给界面看的目标描述（不含敏感信息）。"""
        group = str(self.official("group_openid", "") or "")
        if group:
            return "群 %s" % _short(group)
        target = str(self.official("target_openid", "") or "")
        return "私聊 %s" % _short(target) if target else "私聊（自动记住最近用户）"

    def to_public(self) -> Dict[str, Any]:
        """给界面/API 用的公开字段（不含 AppSecret 等敏感值）。"""
        return {
            "id": self.id,
            "index": self.index,
            "prefix": self.prefix,
            "name": self.name,
            "enabled": self.enabled,
            "mode": self.mode,
            "mode_label": self.mode_label,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "target": self.target_summary(),
            "is_primary": self.is_primary,
            "app_id": str(self.official("app_id", "") or ""),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<BotSpec %s %s %s>" % (self.id, self.name, self.mode)


def _short(value: str, keep: int = 10) -> str:
    text = str(value or "")
    if len(text) <= keep * 2:
        return text
    return "%s…%s" % (text[:keep], text[-4:])


def _qq_defaults() -> Dict[str, Any]:
    from .config import DEFAULTS

    return copy.deepcopy(DEFAULTS.get("qq", {}))


def bot_dicts(config: Any) -> List[Dict[str, Any]]:
    """把配置解析成机器人列表（第 1 个来自 ``qq:`` 段）。

    每个元素都补全了默认值，因此后面的代码可以直接 ``bot.qq("target_user_id")``。
    """
    defaults = _qq_defaults()
    primary: Dict[str, Any] = {}
    try:
        raw_primary = config.get("qq", {}) or {}
    except Exception:  # pragma: no cover - 传入普通 dict 时
        raw_primary = (config or {}).get("qq", {}) or {}
    if isinstance(raw_primary, dict):
        primary = _merge(defaults, raw_primary)
    else:
        primary = copy.deepcopy(defaults)
    primary.setdefault("id", "bot1")
    primary["enabled"] = bool(primary.get("enabled", True))

    result: List[Dict[str, Any]] = [primary]

    entries: Any = []
    try:
        entries = config.get(BOTS_KEY, []) or []
    except Exception:  # pragma: no cover
        entries = (config or {}).get(BOTS_KEY, []) or []
    if not isinstance(entries, list):
        entries = []
    used_ids = {str(primary.get("id") or "bot1")}
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        merged = _merge(defaults, entry)
        candidate = str(entry.get("id") or "").strip() or "bot%d" % (position + 2)
        if candidate in used_ids:
            candidate = "%s-%d" % (candidate, position + 2)
        used_ids.add(candidate)
        merged["id"] = candidate
        merged["enabled"] = bool(entry.get("enabled", merged.get("enabled", True)))
        result.append(merged)
    return result


def bot_specs(config: Any) -> List[BotSpec]:
    specs: List[BotSpec] = []
    for index, data in enumerate(bot_dicts(config)):
        prefix = PRIMARY_PREFIX if index == 0 else "%s.%d" % (BOTS_KEY, index - 1)
        specs.append(BotSpec(index, prefix, data))
    return specs


def _merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def new_bot_id(config: Any, name: str = "") -> str:
    """生成一个不与现有机器人冲突的 id。"""
    existing = {spec.id for spec in bot_specs(config)}
    index = len(existing) + 1
    while True:
        candidate = "bot%d" % index
        if candidate not in existing:
            return candidate
        index += 1


def new_bot_entry(config: Any, name: str = "") -> Dict[str, Any]:
    """新建机器人时的初始配置（空凭据，默认官方机器人）。"""
    defaults = _qq_defaults()
    entry = {
        "id": new_bot_id(config),
        "name": name or "机器人 %d" % (len(bot_specs(config)) + 1),
        "enabled": True,
        "mode": MODE_OFFICIAL,
        "character_id": "",
        "character_name": "",
        "official": defaults.get("official", {}),
    }
    return entry


def raw_bot_entries(config: Any) -> List[Dict[str, Any]]:
    """``bots:`` 段的原始内容（GUI 保存时原样回写）。"""
    try:
        entries = config.get(BOTS_KEY, []) or []
    except Exception:  # pragma: no cover
        entries = (config or {}).get(BOTS_KEY, []) or []
    if not isinstance(entries, list):
        return []
    return [copy.deepcopy(item) for item in entries if isinstance(item, dict)]


def patch_for_bot(config: Any, index: int, values: Dict[str, Any]) -> Dict[str, Any]:
    """把某个机器人的字段变更转成 ``PUT /api/config`` 的 patch。"""
    if int(index) <= 0:
        return {"qq": dict(values)}
    entries = raw_bot_entries(config)
    position = int(index) - 1
    while len(entries) <= position:
        entries.append({})
    merged = entries[position]
    for key, value in (values or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    entries[position] = merged
    return {BOTS_KEY: entries}


def bot_config_values(spec: BotSpec) -> Dict[str, Any]:
    """机器人当前的连接配置键值（用于界面回显，不含默认值噪声）。"""
    return copy.deepcopy(spec.data)


def character_map(characters: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in characters or []:
        try:
            result[str(item.get("id"))] = str(item.get("name") or "")
        except Exception:  # pragma: no cover
            continue
    return result


__all__ = [
    "BOTS_KEY",
    "IDENTITY_KEYS",
    "MODE_LABELS",
    "MODE_OFFICIAL",
    "PRIMARY_PREFIX",
    "BotSpec",
    "bot_config_values",
    "bot_dicts",
    "bot_specs",
    "character_map",
    "mode_label",
    "new_bot_entry",
    "new_bot_id",
    "normalize_mode",
    "patch_for_bot",
    "raw_bot_entries",
]
