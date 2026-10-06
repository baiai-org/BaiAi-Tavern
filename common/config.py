"""配置加载 / 保存 / 热重载。

配置文件为 ``<数据目录>/config.yaml``（默认 ``./data/config.yaml``）。
设计要点：

* 所有默认值集中在 :data:`DEFAULTS`，文件缺字段或字段写错都不会崩。
* 支持 ``${ENV_VAR}`` 环境变量展开（例如 ``api_key: "${DEEPSEEK_API_KEY}"``）。
* :class:`ConfigManager` 提供 ``patch`` 局部更新与 ``reload_if_changed`` 热重载，
  Bot 进程会在每个调度周期检查文件变化，GUI 保存后无需重启 Bot。
* 启动时会**清理历史遗留字段**（旧版的 NapCat / OneBot 配置），清完写回文件并留一份
  ``config.yaml.bak``，避免用户看着一堆没用的键发懵。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import default_config_path, ensure_dirs, example_config_path

try:  # pragma: no cover - PyYAML 为必需依赖，此处仅为极端情况兜底
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

from .logging_setup import get_logger

log = get_logger("common.config")


#: 旧版本里与 NapCat / OneBot（第三方协议）相关的字段，启动时自动清理
LEGACY_QQ_KEYS = (
    "mode",
    "napcat_api_url",
    "access_token",
    "self_id",
    "target_user_id",
    "allowed_group_ids",
    "reply_max_segments",
    "reply_segment_max_len",
    "typing_speed_cps",
    "typing_delay_max",
    "typing_delay_enabled",
    "send_retries",
)
LEGACY_SECTIONS = ("napcat", "onebot")
LEGACY_APP_KEYS = ("start_napcat_on_launch",)


DEFAULTS: Dict[str, Any] = {
    "app": {
        "onboarding_done": False,
        "start_bot_on_launch": True,
        "minimize_to_tray": True,
        "close_to_tray": True,
        "notify_on_proactive": True,
        "notify_on_error": True,
        "start_minimized": False,
        "autostart_with_windows": False,
        "theme": "dark",
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "max_tokens": 500,
        "temperature": 0.85,
        "top_p": 1.0,
        "timeout": 60,
        "max_retries": 2,
        "fallback_messages": [
            "在忙吗？突然有点想你了。",
            "刚刚走神了一下，想起你了。",
            "嗨，今天过得怎么样？",
        ],
    },
    # 多模态模型路由（V0.2）：每个能力单独配端点 / 模型 / 凭据。
    # 留空的槽位自动降级（不影响文字对话）。chat 留空时回退上面的 llm 段。
    # 引擎：openai（OpenAI 兼容端点，本地服务把 base_url 指向本机即可）/
    #       edge-tts（文字转语音专用，在线免费，无需 API Key）/
    #       gemini-native（图像生成专用，Gemini 原生接口，支持全部 Gemini 图像模型）
    # 听语音（语音转文字）不占槽位：QQ 官方平台随语音消息推送参考转写，零配置。
    # 主模型（chat 槽位）统一读 llm 段；providers.chat 仅在 llm 段未配置时兜底
    "providers": {
        "chat": {
            "engine": "openai",
            "base_url": "",
            "api_key": "",
            "model": "",
        },
        "vision": {
            "engine": "openai",
            "base_url": "",
            "api_key": "",
            "model": "",
        },
        "image": {
            "engine": "openai",
            "base_url": "",
            "api_key": "",
            "model": "",
        },
        "tts": {
            # 推荐默认：阿里云百炼 qwen-audio（自动启用情感与拟声标签 + 语气指令 +
            # 语速/音调/音量调节），用户只需填 API Key；edge-tts / openai 随时可切
            "engine": "dashscope",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "",
            "model": "qwen-audio-3.1-tts-flash",
            "voice": "yuxiaoyun_v3.1",
        },
    },
    # 富媒体（V0.2 图片 / 语音）行为
    "media": {
        "enabled": True,               # 总开关：关闭后只收发纯文字
        "voice_reply_probability": 0.1,  # 回复改用语音的概率（0.0~1.0），单聊/群聊/主动消息通用
        "allow_image": True,           # 允许角色给你发图（回复里出现 [IMG] 描述时触发生成）
        "voice_max_chars": 180,        # 单条语音对应的文字上限，超过会拆成多条语音
        "image_marker": "[IMG]",       # 角色想发图时写在回复里的标记（后跟绘图描述）
        "temp_days": 3,                # 收发的临时媒体文件保留天数
    },
    # 机器人列表：第 1 个机器人就是下面的 qq: 段，第 2..N 个写在这里
    # （每一项与 qq: 段同构，可以各自绑定不同的角色）
    "bots": [],
    "qq": {
        # 机器人身份（多机器人时用来区分；character_id 为空表示不绑定，按“上次/随机”选角色）
        "id": "bot1",
        "name": "机器人 1",
        "enabled": True,
        "character_id": "",
        "character_name": "",
        # QQ 官方机器人（QQ 开放平台）—— 唯一的接入方式
        "official": {
            "app_id": "",
            "app_secret": "",
            "sandbox": False,
            "intents": 33554432,          # 1<<25 群聊与单聊事件；频道消息再加 1<<9 / 1<<12
            "api_domain": "https://api.sgroup.qq.com",
            "token_url": "https://api.bot.qq.com/app/getAppAccessToken",
            "gateway_path": "/gateway",
            "target_openid": "",          # 主动消息目标（留空则自动记住第一个私聊用户）
            "group_openid": "",           # 主动消息目标群
            "allow_all_users": True,      # 私聊是否响应所有用户
            "allowed_users": [],          # allow_all_users=false 时的白名单（openid）
            "allowed_groups": [],         # 群白名单（group_openid），留空表示不限
            "markdown": False,            # 是否用 markdown（需平台权限）
            "max_reply_segments": 3,
            "reply_segment_max_len": 200,
        },
        "user_nickname": "你",
        "reply_enabled": True,
        "group_reply_enabled": False,
    },
    "proactive": {
        "enabled": True,
        "scheduled_enabled": True,
        "scheduled_times": ["09:00", "21:00"],
        "idle_enabled": True,
        "idle_hours": 6,
        "idle_check_interval_minutes": 15,
        "random_enabled": False,
        "random_min_interval_minutes": 120,
        "random_max_interval_minutes": 300,
        "active_hours": {"enabled": True, "start": "08:00", "end": "23:00"},
        "dnd_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
        "global_daily_limit": 10,
        "per_character_daily_limit": 3,
        "probability": 0.7,
        "avoid_repeat": True,
        "min_interval_minutes": 30,
        "max_message_chars": 120,
        "context_messages": 8,
        "include_memory": True,
    },
    "memory": {
        "short_term_max": 20,
        "long_term_retrieve": 5,
        "long_term_enabled": True,
        "auto_extract": True,
    },
    "database": {
        "path": "bot.db",
    },
    "characters": {
        "path": "characters",
        "import_builtin": True,   # 角色池为空时自动导入内置角色
    },
    "api": {
        "host": "127.0.0.1",
        "port": 8765,
        "token": "",
    },
    "logging": {
        "level": "INFO",
        "max_bytes": 2097152,
        "backup_count": 3,
        "console": True,
    },
}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class AttrDict(dict):
    """支持 ``cfg.llm.model`` 形式访问的字典，同时保持字典语义。"""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - 正常流程不会触发
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __delattr__(self, key: str) -> None:
        self.pop(key, None)

    def copy(self) -> "AttrDict":  # type: ignore[override]
        return to_attr(dict(self))


def to_attr(value: Any) -> Any:
    if isinstance(value, dict):
        return AttrDict({k: to_attr(v) for k, v in value.items()})
    if isinstance(value, list):
        return [to_attr(item) for item in value]
    return value


def to_plain(value: Any) -> Any:
    if isinstance(value, AttrDict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return value


def expand_env(value: Any) -> Any:
    """递归展开字符串中的 ``${VAR}``。未定义的环境变量展开为空串。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：``patch`` 中的字典与 ``base`` 合并，其余类型直接覆盖。"""
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def get_by_path(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _load_yaml_text(text: str) -> Dict[str, Any]:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("缺少 PyYAML 依赖，无法解析配置文件")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是字典（key: value 结构）")
    return data


def _dump_yaml(data: Dict[str, Any]) -> str:
    if yaml is None:  # pragma: no cover
        return json.dumps(data, ensure_ascii=False, indent=2)
    return yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, indent=2
    )


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_yaml(data), encoding="utf-8")


def strip_legacy_keys(raw: Dict[str, Any]) -> "tuple":
    """删除旧版本遗留的 NapCat / OneBot 字段，返回 ``(清理后的配置, 被删掉的键名)``。

    历史上程序支持过 NapCat（第三方 OneBot 协议），现在只保留「QQ 官方机器人」，
    因此这些键留着只会让用户困惑，升级时直接清掉（同时把原文件备份成 .bak）。
    """
    data = copy.deepcopy(raw or {})
    removed: List[str] = []

    def _drop_qq_keys(section: Dict[str, Any], prefix: str) -> None:
        for key in LEGACY_QQ_KEYS:
            if key in section:
                section.pop(key, None)
                removed.append("%s.%s" % (prefix, key))

    qq = data.get("qq")
    if isinstance(qq, dict):
        _drop_qq_keys(qq, "qq")
    bots = data.get("bots")
    if isinstance(bots, list):
        for index, entry in enumerate(bots):
            if isinstance(entry, dict):
                _drop_qq_keys(entry, "bots.%d" % index)

    for section in LEGACY_SECTIONS:
        if section in data:
            data.pop(section, None)
            removed.append(section)

    app = data.get("app")
    if isinstance(app, dict):
        for key in LEGACY_APP_KEYS:
            if key in app:
                app.pop(key, None)
                removed.append("app.%s" % key)

    # V0.2 开发中删除了 asr 槽位（语音转文字改用 QQ 官方平台自带的参考转写）：
    # 老配置里可能还留着 providers.asr 段，直接清掉
    providers = data.get("providers")
    if isinstance(providers, dict) and "asr" in providers:
        providers.pop("asr", None)
        removed.append("providers.asr")

    return data, removed


class ConfigManager:
    """线程安全的配置管理器。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._raw: Dict[str, Any] = {}
        self._stat: Optional[tuple] = None
        self.revision = 0

    # ---------------------------------------------------------------- 基础
    def ensure_file(self) -> bool:
        """配置文件不存在时从模板复制，返回是否新建。"""
        with self._lock:
            if self.path.exists():
                return False
            ensure_dirs()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            template = example_config_path()
            if template.exists():
                self.path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.path.write_text(
                    "# BaiAi-Tavern 配置文件（自动生成）\n" + _dump_yaml(DEFAULTS),
                    encoding="utf-8",
                )
            return True

    def load(self, force: bool = False) -> AttrDict:
        with self._lock:
            if self._data and not force:
                return self.data
            self.ensure_file()
            raw: Dict[str, Any] = {}
            try:
                raw = _load_yaml_text(self.path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            cleaned, removed = strip_legacy_keys(raw)
            if removed:
                # 首次升级时把历史遗留字段（旧版 NapCat / OneBot）清掉并备份原文件
                log.info("配置中已自动移除历史遗留字段：%s", "、".join(removed))
                try:
                    backup = self.path.with_suffix(self.path.suffix + ".bak")
                    if not backup.exists():
                        shutil.copy2(str(self.path), str(backup))
                    _write_yaml(self.path, cleaned)
                    raw = cleaned
                except Exception as exc:  # pragma: no cover - 写回失败不影响启动
                    log.warning("清理历史配置写回失败：%s", exc)
            self._raw = raw
            merged = deep_merge(DEFAULTS, raw)
            self._data = expand_env(merged)
            self._stat = self._file_stat()
            self.revision += 1
            return self.data

    @property
    def data(self) -> AttrDict:
        if not self._data:
            return self.load()
        return to_attr(self._data)

    def raw(self) -> AttrDict:
        """用户文件中的原始内容（不含默认值补齐），用于 GUI 显示。"""
        if not self._raw:
            self.load()
        return to_attr(copy.deepcopy(self._raw))

    def get(self, path: str, default: Any = None) -> Any:
        return get_by_path(self._data if self._data else self.load(), path, default)

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            if not self._data:
                self.load()
            parts = str(path).split(".")
            cursor = self._data
            for part in parts[:-1]:
                nxt = cursor.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cursor[part] = nxt
                cursor = nxt
            cursor[parts[-1]] = value
            self.revision += 1

    def patch(self, patch: Dict[str, Any], persist: bool = True) -> AttrDict:
        """局部更新配置（deep merge），可选择立即写回文件。"""
        with self._lock:
            if not self._data:
                self.load()
            self._data = expand_env(deep_merge(self._data, patch or {}))
            self.revision += 1
            if persist:
                self.save()
            return self.data

    def save(self) -> None:
        with self._lock:
            ensure_dirs()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = (
                "# BaiAi-Tavern 配置文件\n"
                "# 可直接编辑，也可在 GUI 的“系统设置”页面修改后保存。\n"
                "# 支持 ${ENV_VAR} 形式引用环境变量，例如 api_key: \"${DEEPSEEK_API_KEY}\"\n\n"
            )
            text += _dump_yaml(self._data)
            tmp = self.path.with_suffix(".yaml.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            self._stat = self._file_stat()

    # ------------------------------------------------------------- 热重载
    def _file_stat(self) -> Optional[tuple]:
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def changed_on_disk(self) -> bool:
        current = self._file_stat()
        return current is not None and current != self._stat

    def reload_if_changed(self) -> bool:
        """文件被外部修改时重新加载，返回是否发生了重载。"""
        with self._lock:
            if not self.changed_on_disk():
                return False
            self.load(force=True)
            return True

    # -------------------------------------------------------------- 便捷
    def llm_configured(self) -> bool:
        return bool(str(self.get("llm.api_key", "")).strip()) and bool(
            str(self.get("llm.base_url", "")).strip()
        )

    def proactive_enabled(self) -> bool:
        return bool(self.get("proactive.enabled", True))

    def api_base_url(self) -> str:
        host = str(self.get("api.host", "127.0.0.1") or "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return "http://%s:%d" % (host, int(self.get("api.port", 8765) or 8765))

    def effective_database_path(self) -> Path:
        from .paths import database_path

        return self._resolve_data_relative(self.get("database.path", ""), database_path())

    def effective_characters_path(self) -> Path:
        from .paths import characters_dir

        return self._resolve_data_relative(self.get("characters.path", ""), characters_dir())

    @staticmethod
    def _resolve_data_relative(raw: Any, default: Path) -> Path:
        """把配置中的相对路径解析为绝对路径。

        相对路径基于「数据目录」（默认 ``<程序目录>/data``，可用环境变量
        ``QQAI_DATA_DIR`` 重定向），因此把数据目录整体搬到别处也能正常工作。
        为兼容旧写法，``data/bot.db`` 会被等价转换为 ``<数据目录>/bot.db``。
        """
        from .paths import data_dir

        text = str(raw or "").strip()
        if not text:
            return default
        normalized = text.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith("data/"):
            normalized = normalized[len("data/") :]
        candidate = Path(normalized).expanduser()
        if not candidate.is_absolute():
            candidate = data_dir() / candidate
        return candidate

    def timezone_hint(self) -> str:
        return str(DEFAULTS["app"].get("theme", "dark"))


_instance: Optional[ConfigManager] = None
_instance_lock = threading.Lock()


def get_config(path: Optional[Path] = None) -> ConfigManager:
    """进程内单例。"""
    global _instance
    with _instance_lock:
        if _instance is None or (path is not None and Path(path) != _instance.path):
            _instance = ConfigManager(path)
        return _instance


def default_config_dict() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULTS)


def feature_flags() -> List[str]:
    """预留：未来可通过配置开启实验特性。"""
    return []
