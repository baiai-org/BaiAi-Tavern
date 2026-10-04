"""机器人账号（BotAccount）：一个 QQ 官方机器人的完整运行时。

每个机器人有自己的：

* 凭据（AppID / AppSecret）
* 官方网关与事件处理器（每个机器人一条长连接）
* 主动消息目标（openid / 群 openid）
* **绑定角色**（由用户在界面上指定；未绑定时沿用「上次发言/随机」逻辑）

:class:`~bot.runtime.Runtime` 持有 ``bots`` 列表，其它模块通过 ``runtime.bots``
遍历所有机器人，因此单机器人配置（旧配置文件）与多机器人完全走同一套代码。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from common.bots import BotSpec, MODE_OFFICIAL, bot_specs
from common.logging_setup import get_logger
from common.utils import iso_now

from .database import crud
from .qq_official import OfficialGateway, OfficialReceiver
from .qq_official.messaging import Messaging

log = get_logger("bot.accounts")


def _resolve_path(config: Any, prefix: str, key: str, default: Any = None) -> Any:
    """按 ``qq.`` / ``bots.2.`` 前缀读取配置。"""
    if prefix == "qq":
        return config.get("%s.%s" % (prefix, key), default)
    entries = config.get("bots", []) or []
    try:
        position = int(prefix.split(".")[-1])
    except Exception:  # pragma: no cover
        return default
    if 0 <= position < len(entries) and isinstance(entries[position], dict):
        value = (entries[position] or {}).get(key, default)
        return default if value is None else value
    return default


class BotAccount:
    """一个机器人的运行时对象。"""

    def __init__(self, runtime: Any, spec: BotSpec):
        self.rt = runtime
        self.spec = spec

        self.messaging = Messaging(runtime, self)
        self.gateway: Optional[OfficialGateway] = None
        self.receiver: Optional[OfficialReceiver] = None

        # 运行状态
        self.info: Dict[str, Any] = {}
        self.last_error: str = ""
        self.last_user_openid: str = ""
        self.last_group_openid: str = ""
        self.last_openid_at: str = ""
        self.learned_self_id: str = ""

    # ============================================================== 基本信息
    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def index(self) -> int:
        return self.spec.index

    @property
    def enabled(self) -> bool:
        return self.spec.enabled

    @property
    def mode(self) -> str:
        return self.spec.mode

    @property
    def mode_label(self) -> str:
        return self.spec.mode_label

    @property
    def is_official(self) -> bool:
        return self.spec.is_official

    @property
    def character_id(self) -> str:
        return self.spec.character_id

    @property
    def character_name(self) -> str:
        return self.spec.character_name

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<BotAccount %s %s %s>" % (self.id, self.name, self.mode)

    # ============================================================== 配置同步
    def apply_spec(self, spec: BotSpec) -> None:
        """配置热重载后更新连接参数（凭据/模式变化时网关会自行重连）。"""
        self.spec = spec

    def intents(self) -> int:
        return self.messaging.intents()

    def describe_target(self) -> str:
        return self.spec.target_summary()

    # ============================================================== 状态载入
    async def load_state(self) -> None:
        """读取上次学习到的 self_id 与最近对话对象（重启后仍可发主动消息）。"""
        try:
            self.last_user_openid = str(
                await crud.get_bot_state(self.rt.db, self.id, "official.last_user_openid", "") or ""
            )
            self.last_group_openid = str(
                await crud.get_bot_state(self.rt.db, self.id, "official.last_group_openid", "") or ""
            )
            self.learned_self_id = str(
                await crud.get_bot_state(self.rt.db, self.id, "self_id", "") or ""
            )
        except Exception as exc:  # pragma: no cover
            log.debug("读取机器人 %s 的历史状态失败：%s", self.id, exc)

    async def remember_openid(self, openid: str = "", group_openid: str = "") -> None:
        changed = False
        if openid and openid != self.last_user_openid:
            self.last_user_openid = openid
            changed = True
        if group_openid and group_openid != self.last_group_openid:
            self.last_group_openid = group_openid
            changed = True
        if not changed:
            return
        if openid:
            self.last_openid_at = iso_now()
        try:
            if openid:
                await crud.set_bot_state(self.rt.db, self.id, "official.last_user_openid", openid)
            if group_openid:
                await crud.set_bot_state(self.rt.db, self.id, "official.last_group_openid", group_openid)
            if self.spec.is_primary:
                # 兼容旧版本：第一个机器人同时写历史键，老界面/脚本仍能读到
                if openid:
                    await crud.set_setting(self.rt.db, "qq.official.last_user_openid", openid)
                if group_openid:
                    await crud.set_setting(self.rt.db, "qq.official.last_group_openid", group_openid)
        except Exception as exc:  # pragma: no cover
            log.debug("保存机器人 %s 的对话对象失败：%s", self.id, exc)

    async def remember_self_id(self, self_id: str) -> None:
        value = str(self_id or "").strip()
        if not value or value == self.learned_self_id:
            return
        self.learned_self_id = value
        try:
            await crud.set_bot_state(self.rt.db, self.id, "self_id", value)
            if self.spec.is_primary:
                await crud.set_last_self_id(self.rt.db, value)
        except Exception:  # pragma: no cover
            pass

    async def forget_openid(self) -> None:
        self.last_user_openid = ""
        self.last_group_openid = ""
        try:
            await crud.set_bot_state(self.rt.db, self.id, "official.last_user_openid", "")
            await crud.set_bot_state(self.rt.db, self.id, "official.last_group_openid", "")
            if self.spec.is_primary:
                await crud.set_setting(self.rt.db, "qq.official.last_user_openid", "")
                await crud.set_setting(self.rt.db, "qq.official.last_group_openid", "")
        except Exception:  # pragma: no cover
            pass

    # ============================================================== 官方网关
    def _make_gateway(self) -> OfficialGateway:
        self.receiver = OfficialReceiver(self.rt, self)
        return OfficialGateway(
            self.messaging.official_client(),
            on_event=self.receiver.handle,
            on_state=self._on_gateway_state,
            intents=self.messaging.intents(),
        )

    def _on_gateway_state(self, connected: bool, error: str) -> None:
        if connected:
            log.info("机器人「%s」的官方网关已连接", self.name)
        elif error:
            log.warning("机器人「%s」的官方网关断开：%s", self.name, error)
        self.rt.publish(
            {
                "type": "qq_connected" if connected else "qq_disconnected",
                "mode": MODE_OFFICIAL,
                "bot_id": self.id,
                "bot_name": self.name,
                "error": error,
            }
        )

    def gateway_running(self) -> bool:
        return bool(self.gateway is not None and self.gateway.running)

    async def start_gateway(self) -> bool:
        """启动官方网关（仅官方模式的已启用机器人）。"""
        if not self.is_official or not self.enabled:
            return False
        if self.gateway is not None and self.gateway.running:
            return True
        client = self.messaging.official_client()
        if not client.configured():
            log.warning("机器人「%s」尚未填写 AppID / AppSecret，跳过网关连接", self.name)
            return False
        self.gateway = self._make_gateway()
        self.gateway.start()
        log.info("机器人「%s」已启动官方机器人网关（intents=%s）", self.name, self.messaging.intents())
        return True

    async def stop_gateway(self) -> None:
        gateway, self.gateway = self.gateway, None
        self.receiver = None
        if gateway is not None:
            await gateway.stop()

    async def sync_gateway(self) -> None:
        """按当前模式与凭据启动/停止本机器人的官方网关。"""
        try:
            if self.is_official and self.enabled:
                client = self.messaging.official_client()
                if not client.configured():
                    if self.gateway is not None:
                        await self.stop_gateway()
                    return
                if self.gateway is None or not self.gateway.running:
                    await self.start_gateway()
                elif self.gateway.intents != self.messaging.intents():
                    await self.stop_gateway()
                    await self.start_gateway()
            elif self.gateway is not None:
                await self.stop_gateway()
        except Exception as exc:  # pragma: no cover
            log.warning("同步机器人「%s」的官方网关失败：%s", self.name, exc)

    # ============================================================== 探测/状态
    async def probe(self) -> Dict[str, Any]:
        """探测本机器人当前连接方式是否可用。"""
        info = await self.messaging.probe()
        self.info = info or {}
        if not self.info.get("available") and self.info.get("error"):
            self.last_error = str(self.info.get("error"))
        return self.info

    def effective_self_id(self) -> int:
        if self.gateway is not None:
            data = getattr(self.gateway.client, "bot_info", {}) or {}
            value = data.get("id") or data.get("user_id")
            if value:
                try:
                    return int(value)
                except Exception:
                    pass
        if self.info.get("user_id"):
            try:
                return int(self.info["user_id"])
            except Exception:
                pass
        try:
            return int(self.learned_self_id or 0)
        except Exception:
            return 0

    def status(self) -> Dict[str, Any]:
        """给界面/API 的状态（不访问网络）。"""
        configured = bool(
            str(self.spec.official("app_id", "") or "").strip()
            and str(self.spec.official("app_secret", "") or "").strip()
        )
        gateway = self.gateway
        connected = bool(gateway is not None and gateway.connected)
        return {
            "id": self.id,
            "index": self.index,
            "name": self.name,
            "enabled": self.enabled,
            "mode": MODE_OFFICIAL,
            "mode_label": self.mode_label,
            "configured": configured,
            "connected": connected,
            "available": connected,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "target": self.describe_target(),
            "app_id": str(self.spec.official("app_id", "") or ""),
            "anonymous": False,
            "nickname": (gateway.client.bot_info.get("username") if gateway is not None else "") or "",
            "user_id": str(self.effective_self_id() or "") or None,
            "error": "" if connected else (
                (gateway.last_error if gateway is not None else "")
                or ("" if configured else "尚未填写 AppID / AppSecret")
                or "网关正在连接…"
            ),
            "target_openid": str(self.spec.official("target_openid", "") or "") or self.last_user_openid,
            "group_openid": str(self.spec.official("group_openid", "") or ""),
            "gateway": gateway.status() if gateway is not None else {},
        }

    # ============================================================== 清理
    async def close(self) -> None:
        await self.stop_gateway()
        try:
            await self.messaging.close()
        except Exception:  # pragma: no cover
            pass

    # ============================================================== 角色
    async def resolved_character(self) -> Optional[Dict[str, Any]]:
        """本机器人绑定的角色（未绑定/已被删除/被禁用时返回 None）。"""
        character_id = self.character_id
        if not character_id:
            return None
        try:
            row = await self.rt.registry.get(character_id)  # type: ignore[union-attr]
        except Exception:  # pragma: no cover
            return None
        if not row or not int(row.get("enabled") or 0):
            return None
        return row


def build_bots(runtime: Any, config: Any) -> List[BotAccount]:
    return [BotAccount(runtime, spec) for spec in bot_specs(config)]


__all__ = ["BotAccount", "build_bots", "_resolve_path"]
