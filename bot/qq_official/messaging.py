"""消息发送层（QQ 官方机器人通道）。

每个 :class:`~bot.accounts.BotAccount` 拥有**自己的** :class:`Messaging`，
所有参数都取自该机器人的配置（凭据、目标、拆分长度、markdown……），
因此多个机器人之间互不干扰。

对外只暴露：

* :meth:`Messaging.target_peer` —— 主动消息发给谁（openid / 群 openid）
* :meth:`Messaging.send_text` —— 发送文本（自动拆段）
* :meth:`Messaging.probe` / :meth:`Messaging.describe` —— 状态与诊断

调度器与 HTTP API 都只依赖这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.logging_setup import get_logger
from common.utils import truncate

from ..database import crud
from .client import OfficialQQClient, OfficialQQError, segments_for_official

log = get_logger("bot.messaging")

MODE_OFFICIAL = "official"
MODE_LABELS = {MODE_OFFICIAL: "QQ 官方机器人"}


@dataclass
class Peer:
    """发送目标。``kind`` 为 ``private`` 或 ``group``。"""

    kind: str
    peer_id: str
    is_group: bool = False

    def describe(self) -> str:
        return "%s:%s" % ("群" if self.is_group else "私聊", truncate(self.peer_id, 24))


class Messaging:
    """某个机器人的发送通道。"""

    def __init__(self, runtime: Any, bot: Any = None):
        self.rt = runtime
        # 兼容旧调用：Messaging(runtime) 时自动使用第一个机器人
        self._bot = bot
        self._official: Optional[OfficialQQClient] = None

    # ---------------------------------------------------------------- 绑定
    @property
    def bot(self) -> Any:
        if self._bot is not None:
            return self._bot
        bots = getattr(self.rt, "bots", None) or []
        if bots:
            self._bot = bots[0]
        return self._bot

    @property
    def spec(self) -> Any:
        bot = self.bot
        return getattr(bot, "spec", None)

    def _qq(self, key: str, default: Any = None) -> Any:
        spec = self.spec
        if spec is None:  # pragma: no cover - 极端兜底
            return self.rt.config.get("qq.%s" % key, default)
        return spec.qq(key, default)

    # ============================================================== 通道信息
    @property
    def mode(self) -> str:
        return MODE_OFFICIAL

    def mode_label(self) -> str:
        return MODE_LABELS[MODE_OFFICIAL]

    def is_official(self) -> bool:
        return True

    def official_config(self) -> Dict[str, Any]:
        spec = self.spec
        if spec is None:  # pragma: no cover - 极端兜底
            return dict(self.rt.config.get("qq.official", {}) or {})
        return spec.official_config

    def official_client(self) -> OfficialQQClient:
        """官方客户端（懒加载，配置变更后自动同步）。"""
        cfg = self.official_config()
        if self._official is None:
            self._official = OfficialQQClient(
                app_id=str(cfg.get("app_id", "") or ""),
                app_secret=str(cfg.get("app_secret", "") or ""),
                api_domain=str(cfg.get("api_domain", "https://api.sgroup.qq.com") or ""),
                token_url=str(cfg.get("token_url", "https://api.bot.qq.com/app/getAppAccessToken") or ""),
                sandbox=bool(cfg.get("sandbox", False)),
                gateway_path=str(cfg.get("gateway_path", "/gateway") or "/gateway"),
            )
        else:
            self._official.configure(
                app_id=str(cfg.get("app_id", "") or ""),
                app_secret=str(cfg.get("app_secret", "") or ""),
                sandbox=bool(cfg.get("sandbox", False)),
                api_domain=str(cfg.get("api_domain", "") or ""),
                token_url=str(cfg.get("token_url", "") or ""),
                gateway_path=str(cfg.get("gateway_path", "") or ""),
            )
        return self._official

    def official_ready(self) -> bool:
        gateway = getattr(self.bot, "gateway", None)
        return bool(gateway is not None and gateway.connected)

    def intents(self) -> int:
        cfg = self.official_config()
        try:
            return int(cfg.get("intents") or 0) or (1 << 25)
        except Exception:
            return 1 << 25

    async def close(self) -> None:
        if self._official is not None:
            await self._official.close()

    # ============================================================== 目标
    async def target_peer(self) -> tuple:
        """返回 ``(Peer | None, 原因)``。openid 支持手动填写或自动学习。"""
        cfg = self.official_config()
        manual = str(cfg.get("target_openid", "") or "").strip()
        group = str(cfg.get("group_openid", "") or "").strip()
        if group:
            return Peer("group", group, is_group=True), ""
        if manual:
            return Peer("private", manual), ""
        learned = getattr(self.bot, "last_user_openid", "") or ""
        if not learned:
            try:
                learned = str(
                    await crud.get_bot_state(
                        self.rt.db, getattr(self.bot, "id", "bot1"), "official.last_user_openid", ""
                    )
                    or ""
                )
            except Exception:  # pragma: no cover
                learned = ""
        if not learned and getattr(self.bot, "spec", None) is not None and self.bot.spec.is_primary:
            learned = getattr(self.rt, "last_user_openid", "") or ""
        if learned:
            return Peer("private", str(learned)), ""
        return None, (
            "官方机器人只能按 openid 发送：请先在 QQ 里给机器人发一条消息（会自动记住），"
            "或在「机器人」页面手动填写目标 openid"
        )

    # ============================================================== 发送
    async def send_text(
        self,
        text: str,
        peer: Peer,
        character_name: str = "",
        reply_to: str = "",
        msg_seq: int = 1,
    ) -> Dict[str, Any]:
        """发送文本。返回 ``{"ok": bool, "sent": n, "error": str}``。"""
        cfg = self.official_config()
        client = self.official_client()
        segments = segments_for_official(
            text,
            max_len=int(cfg.get("reply_segment_max_len", 200) or 200),
            max_segments=int(cfg.get("max_reply_segments", 3) or 3),
        )
        if not segments:
            return {"ok": False, "sent": 0, "error": "消息内容为空"}
        if not self.official_ready():
            # 官方平台要求机器人网关在线才能发消息
            return {
                "ok": False,
                "sent": 0,
                "error": "官方机器人网关未连接，无法发送（请检查 AppID/AppSecret，或查看日志）",
            }
        markdown = bool(cfg.get("markdown", False))
        sent = 0
        try:
            for index, segment in enumerate(segments):
                if peer.is_group:
                    await client.send_group(
                        peer.peer_id, segment, msg_id=reply_to, msg_seq=msg_seq + index, markdown=markdown
                    )
                else:
                    await client.send_c2c(
                        peer.peer_id, segment, msg_id=reply_to, msg_seq=msg_seq + index, markdown=markdown
                    )
                sent += 1
        except OfficialQQError as exc:
            return {"ok": False, "sent": sent, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "sent": sent, "error": str(exc)}
        return {"ok": True, "sent": sent, "error": ""}

    # ============================================================== 状态
    async def probe(self) -> Dict[str, Any]:
        """探测可用性（GUI 状态栏与「测试连接」都用它）。"""
        client = self.official_client()
        info = await client.probe()
        gateway = getattr(self.bot, "gateway", None)
        info.update(
            {
                "mode": MODE_OFFICIAL,
                "mode_label": self.mode_label(),
                "gateway_connected": bool(gateway and gateway.connected),
                "api_domain": client.api_domain,
                "sandbox": bool(self.official_config().get("sandbox", False)),
                "bot_id": getattr(self.bot, "id", ""),
                "bot_name": getattr(self.bot, "name", ""),
            }
        )
        if not info.get("error") and gateway is not None and not gateway.connected:
            info["error"] = gateway.last_error or "网关正在连接…"
        return info

    def describe(self) -> Dict[str, Any]:
        """同步返回静态状态（不访问网络）。"""
        gateway = getattr(self.bot, "gateway", None)
        cfg = self.official_config()
        return {
            "mode": MODE_OFFICIAL,
            "mode_label": self.mode_label(),
            "configured": bool(str(cfg.get("app_id", "")).strip() and str(cfg.get("app_secret", "")).strip()),
            "app_id": str(cfg.get("app_id", "") or ""),
            "sandbox": bool(cfg.get("sandbox", False)),
            "connected": bool(gateway and gateway.connected),
            "gateway": gateway.status() if gateway is not None else {},
            "api_domain": self.official_client().api_domain if self._official else str(cfg.get("api_domain", "")),
            "target_openid": str(cfg.get("target_openid", "") or ""),
            "group_openid": str(cfg.get("group_openid", "") or ""),
            "bot_id": getattr(self.bot, "id", ""),
            "bot_name": getattr(self.bot, "name", ""),
        }


__all__ = ["MODE_LABELS", "MODE_OFFICIAL", "Messaging", "Peer"]
