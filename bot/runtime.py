"""Bot 运行时容器。

把配置、数据库、角色注册表、对话引擎、调度器和状态集中在一个对象里，
HTTP API、消息处理器、调度器都只依赖它，避免模块间互相 import 造成循环依赖。

**多机器人**：``self.bots`` 是一组 :class:`~bot.accounts.BotAccount`，
每个机器人（= 一个 QQ 官方机器人应用）有自己的凭据、目标与绑定角色；
旧的单机器人配置（``qq:`` 段）会自动成为第一个机器人，因此旧配置无需迁移。
为兼容旧调用点，``runtime.messaging`` / ``runtime.gateway`` 指向**第一个机器人**。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set

from common.bots import bot_specs
from common.config import ConfigManager, get_config
from common.logging_setup import get_logger
from common.paths import ensure_dirs, logs_dir
from common.utils import iso_now, truncate

from .accounts import BotAccount, build_bots
from .ai_engine import AIEngine
from .character_manager import CharacterRegistry
from .database import Database, crud
from .media.hub import MediaHub
from .memory.summarizer import MemorySummarizer
from .qq_official.messaging import MODE_OFFICIAL
from .scheduler import ProactiveScheduler

log = get_logger("bot.runtime")


class Runtime:
    def __init__(self, config: Optional[ConfigManager] = None):
        self.config = config or get_config()
        self.config.ensure_file()
        self.config.load()

        self.db = Database(self.config.effective_database_path())
        self.registry: Optional[CharacterRegistry] = None
        self.engine: Optional[AIEngine] = None
        self.bots: List[BotAccount] = build_bots(self, self.config)
        self.scheduler = ProactiveScheduler(self)
        self.media = MediaHub(self)
        self.summarizer = MemorySummarizer(self)
        self._summarize_task: Optional[asyncio.Task] = None

        # ---------------------------------------------------------- 控制接口
        self.host: str = str(self.config.get("api.host", "127.0.0.1") or "127.0.0.1")
        self.port: int = int(self.config.get("api.port", 8765) or 8765)
        self.log_path: str = str(logs_dir() / "bot.log")

        # ---------------------------------------------------------- 运行状态
        self.started_at: str = iso_now()
        self.ready: bool = False
        self.last_event_at: str = ""
        self.last_error: str = ""
        self.last_reply_at: str = ""
        self.last_reply_preview: str = ""
        self.learned_self_id: str = ""
        self.last_user_openid: str = ""
        self.last_group_openid: str = ""
        self.last_openid_at: str = ""
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 已应用到各子系统的配置版本号（见 sync_config 的说明）
        self._applied_revision: int = int(getattr(self.config, "revision", 0))

    # ============================================================== 机器人
    def primary_bot(self) -> Optional[BotAccount]:
        return self.bots[0] if self.bots else None

    def enabled_bots(self) -> List[BotAccount]:
        return [bot for bot in self.bots if bot.enabled]

    def bot_by_id(self, bot_id: str) -> Optional[BotAccount]:
        for bot in self.bots:
            if bot.id == bot_id:
                return bot
        return None

    def bot_by_self_id(self, self_id: str) -> Optional[BotAccount]:
        """按官方平台返回的机器人 ID 找机器人（找不到时退回第一个）。"""
        value = str(self_id or "").strip()
        if value:
            for bot in self.bots:
                if str(bot.effective_self_id() or "") == value or bot.learned_self_id == value:
                    return bot
        return self.primary_bot()

    def bot_by_index(self, index: int) -> Optional[BotAccount]:
        if 0 <= index < len(self.bots):
            return self.bots[index]
        return None

    @property
    def messaging(self) -> Any:
        bot = self.primary_bot()
        return bot.messaging if bot is not None else None

    @property
    def gateway(self) -> Any:
        bot = self.primary_bot()
        return bot.gateway if bot is not None else None

    @property
    def official_receiver(self) -> Any:
        bot = self.primary_bot()
        return bot.receiver if bot is not None else None

    def rebuild_bots(self) -> None:
        """配置热重载后重建机器人列表（保留仍然存在的机器人对象与其连接）。"""
        specs = bot_specs(self.config)
        existing = {bot.id: bot for bot in self.bots}
        rebuilt: List[BotAccount] = []
        for spec in specs:
            bot = existing.get(spec.id)
            if bot is None:
                bot = BotAccount(self, spec)
            else:
                bot.apply_spec(spec)
            rebuilt.append(bot)
        for bot in self.bots:
            if bot.id not in {item.id for item in rebuilt}:
                asyncio.ensure_future(self._dispose_bot(bot))
        self.bots = rebuilt

    async def _dispose_bot(self, bot: BotAccount) -> None:
        try:
            await bot.close()
            log.info("已移除机器人「%s」", bot.name)
        except Exception as exc:  # pragma: no cover
            log.debug("清理机器人「%s」失败：%s", bot.name, exc)

    # ============================================================== 生命周期
    async def setup(self) -> "Runtime":
        ensure_dirs()
        await self.db.connect()
        self.registry = CharacterRegistry(self.db, self.config.effective_characters_path())
        self.engine = AIEngine(self.db, self.config)

        stored_self_id = await self._load_self_id()
        if stored_self_id:
            self.learned_self_id = stored_self_id
        try:
            self.last_user_openid = str(
                await crud.get_setting(self.db, "qq.official.last_user_openid", "") or ""
            )
            self.last_group_openid = str(
                await crud.get_setting(self.db, "qq.official.last_group_openid", "") or ""
            )
        except Exception:
            pass

        for bot in self.bots:
            await bot.load_state()

        self._loop = asyncio.get_event_loop()
        self.ready = True
        self._start_summarizer_loop()
        log.info(
            "运行时初始化完成（%d 个机器人：%s），数据库：%s",
            len(self.bots),
            "、".join("%s/%s" % (bot.name, bot.mode_label) for bot in self.bots) or "无",
            self.db.path,
        )
        return self

    async def teardown(self) -> None:
        self.ready = False
        try:
            self.scheduler.shutdown()
        except Exception:
            pass
        if self._summarize_task is not None:
            self._summarize_task.cancel()
            self._summarize_task = None
        await self.stop_gateway()
        if self.engine is not None:
            await self.engine.close()
        for bot in list(self.bots):
            try:
                await bot.close()
            except Exception:  # pragma: no cover
                pass
        await self.db.close()
        log.info("运行时已关闭")

    # ========================================================= 对话压缩（V0.2.2）
    async def maybe_summarize(self, character_id: str) -> None:
        """回复 / 主动消息后顺手压缩一次过期消息（失败静默，下次再试）。"""
        if self.summarizer is None or not self.summarizer.enabled():
            return
        try:
            await self.summarizer.maybe_summarize(character_id, max_batches=1)
        except Exception as exc:  # pragma: no cover - 压缩不能影响主链路
            log.debug("对话压缩跳过（%s）：%s", character_id, exc)

    def _start_summarizer_loop(self) -> None:
        """周期任务：每 60 秒扫描一次所有角色的过期消息（兜底无聊天时的压缩）。"""

        async def _loop() -> None:
            await asyncio.sleep(60)
            while True:
                try:
                    await self.summarizer.scan_all(max_batches=1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover
                    log.debug("对话压缩周期扫描失败：%s", exc)
                await asyncio.sleep(60)

        self._summarize_task = asyncio.create_task(_loop(), name="memory-summarizer")

    # ============================================================== 官方网关
    async def start_gateway(self) -> bool:
        """启动所有官方机器人的网关（仅 official 模式下有意义）。"""
        started = False
        for bot in self.bots:
            if await bot.start_gateway():
                started = True
        return started

    async def stop_gateway(self) -> None:
        for bot in list(self.bots):
            await bot.stop_gateway()

    async def start_bot_gateway(self, bot_id: str) -> bool:
        bot = self.bot_by_id(bot_id) if bot_id else self.primary_bot()
        if bot is None:
            return False
        return await bot.start_gateway()

    async def stop_bot_gateway(self, bot_id: str) -> None:
        bot = self.bot_by_id(bot_id) if bot_id else self.primary_bot()
        if bot is not None:
            await bot.stop_gateway()

    @property
    def bot_started(self) -> bool:
        return self.ready and self.scheduler.running

    async def _load_self_id(self) -> str:
        try:
            value = await crud.get_last_self_id(self.db)
            return str(value or "")
        except Exception:
            return ""

    # ============================================================== 配置变更
    def sync_config(self) -> bool:
        """把**磁盘上的配置变化**同步到各子系统，返回是否真的应用了变更。

        注意：``config.reload_if_changed()`` 会被大量只读接口调用（``/api/status``
        等，GUI 每 3 秒轮询一次）。谁先调用谁就把文件变化“消费”掉了，
        因此不能靠它来判断“是否有变更”，而要比较**已应用的配置版本号**，
        否则手改 config.yaml 后可能一直不生效（界面显示新值、Bot 还在用旧配置）。
        """
        changed = self.config.reload_if_changed()
        revision = int(getattr(self.config, "revision", 0))
        if not changed and revision == self._applied_revision:
            return False
        self.apply_config()
        return True

    def apply_config(self) -> None:
        """配置热重载后同步各子系统（含新增/删除机器人）。"""
        cfg = self.config
        if self.engine is not None:
            self.engine.apply_config(cfg)
        self.rebuild_bots()
        self._applied_revision = int(getattr(self.config, "revision", 0))
        # 官方机器人：凭据/模式变化时需要重连网关
        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self._sync_gateway())
        log.info(
            "运行时配置已刷新（%d 个机器人：%s）",
            len(self.bots),
            "、".join("%s/%s" % (bot.name, bot.mode_label) for bot in self.bots) or "无",
        )

    async def _sync_gateway(self) -> None:
        """按当前模式与凭据启动/停止每个机器人的官方网关。"""
        for bot in list(self.bots):
            await bot.sync_gateway()
        try:
            self.publish({"type": "bots_changed"})
        except Exception:  # pragma: no cover
            pass

    # ============================================================== 状态快照
    async def snapshot(self) -> Dict[str, Any]:
        self.config.reload_if_changed()
        today = await crud.stats_today(self.db)
        last_proactive = await crud.last_proactive(self.db)
        last_user_message = await crud.get_last_user_message(self.db)
        primary = self.primary_bot()
        qq_state = await self.qq_status()
        bots_state = [bot.status() for bot in self.bots]
        return {
            "online": self.bot_started,
            "ready": self.ready,
            "started_at": self.started_at,
            "qq_mode": (primary.mode if primary is not None else MODE_OFFICIAL),
            "qq_mode_label": (primary.mode_label if primary is not None else ""),
            "qq": qq_state,
            "bot_count": len(self.bots),
            "bots": bots_state,
            # ID 用字符串：平台给的是 20 位数字，转 int 会超出 Qt/JS 的安全整数范围
            "self_id": str(self.effective_self_id() or ""),
            "self_id_source": ("机器人信息" if qq_state.get("user_id") else ("网关学习" if self.learned_self_id else "未知")),
            "llm": {
                "model": self.config.get("llm.model", ""),
                "base_url": self.config.get("llm.base_url", ""),
                "configured": self.config.llm_configured(),
            },
            "media": self._media_status(),
            "characters": {
                "total": today.get("character_total", 0),
                "enabled": today.get("character_enabled", 0),
            },
            "today": today,
            "proactive": self.scheduler.status(),
            "last_proactive": last_proactive,
            "last_user_message_at": last_user_message,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "last_reply_at": self.last_reply_at,
            "last_reply_preview": self.last_reply_preview,
            "last_user_openid": self.last_user_openid or await self._learned_openid(),
        }

    def _media_status(self) -> Dict[str, Any]:
        """多媒体能力状态（槽位是否配置、语音概率等），供 GUI 状态栏 / 模型路由页。"""
        try:
            return self.media.status()
        except Exception as exc:  # pragma: no cover - 状态快照不应因多媒体报错
            log.warning("读取多媒体状态失败：%s", exc)
            return {"enabled": True, "any_configured": False, "slots": {}}

    async def _learned_openid(self) -> str:
        try:
            return str(await crud.get_setting(self.db, "qq.official.last_user_openid", "") or "")
        except Exception:
            return ""

    async def qq_status(self, bot_id: str = "") -> Dict[str, Any]:
        """某个机器人（默认第一个）的连接状态（GUI 显示 / 诊断用）。"""
        bot = self.bot_by_id(bot_id) if bot_id else self.primary_bot()
        if bot is None:
            return {
                "mode": MODE_OFFICIAL,
                "mode_label": "",
                "configured": False,
                "connected": False,
                "available": False,
                "error": "没有可用的机器人配置",
            }
        info = bot.status()
        info["available"] = bool(info.get("connected"))
        if not info.get("error"):
            info["error"] = "" if info.get("connected") else "正在连接…"
        return info

    async def bots_status(self) -> List[Dict[str, Any]]:
        return [bot.status() for bot in self.bots]

    def effective_self_id(self) -> int:
        bot = self.primary_bot()
        return bot.effective_self_id() if bot is not None else 0

    async def probe_bots(self, bot_id: str = "") -> Dict[str, Any]:
        """联网探测机器人连接是否可用（「测试连接」按钮用）。

        ``bot_id`` 为空时探测**所有**机器人；返回其中第一个机器人的结果。
        """
        targets = [self.bot_by_id(bot_id)] if bot_id else list(self.bots)
        primary: Optional[Dict[str, Any]] = None
        for bot in targets:
            if bot is None:
                continue
            try:
                info = await bot.probe()
            except Exception as exc:  # pragma: no cover
                info = {"available": False, "error": str(exc)}
                bot.info = info
            if bot.index == 0 or (len(targets) == 1 and bot_id):
                primary = info
            if not info.get("available") and info.get("error"):
                self.last_error = "%s：%s" % (bot.name, info["error"])
        return primary if primary is not None else {}

    async def recall_last_openid(self) -> None:
        """重新从数据库读取自动记住的 openid（界面刷新用）。"""
        try:
            self.last_user_openid = str(
                await crud.get_setting(self.db, "qq.official.last_user_openid", "") or ""
            )
            self.last_group_openid = str(
                await crud.get_setting(self.db, "qq.official.last_group_openid", "") or ""
            )
        except Exception:
            pass
        for bot in self.bots:
            await bot.load_state()

    # ============================================================== 事件推送
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Dict[str, Any]) -> None:
        """向所有 WebSocket 订阅者广播事件（非阻塞，可在任意上下文调用）。"""
        if not self._subscribers:
            return
        payload = dict(event)
        payload.setdefault("at", iso_now())
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    pass

    # ============================================================== 消息处理
    async def remember_self_id(self, self_id: str, bot_id: str = "") -> None:
        """记录机器人自己的 ID（网关 READY 时由官方平台给出）。"""
        bot = self.bot_by_id(bot_id) if bot_id else self.bot_by_self_id(str(self_id))
        if bot is not None:
            await bot.remember_self_id(self_id)
        if not self_id or str(self_id) == self.learned_self_id:
            return
        self.learned_self_id = str(self_id)
        try:
            await crud.set_last_self_id(self.db, str(self_id))
        except Exception:
            pass

    async def remember_openid(self, openid: str, group_openid: str = "", bot_id: str = "") -> None:
        """记住官方平台的对话对象（官方接口只能按 openid 发送）。"""
        bot = self.bot_by_id(bot_id) if bot_id else self.primary_bot()
        if bot is not None:
            await bot.remember_openid(openid=openid, group_openid=group_openid)
        changed = False
        if openid and openid != self.last_user_openid:
            self.last_user_openid = openid
            changed = True
        if group_openid and group_openid != self.last_group_openid:
            self.last_group_openid = group_openid
            changed = True
        if not changed:
            return
        try:
            if openid:
                await crud.set_setting(self.db, "qq.official.last_user_openid", openid)
            if group_openid:
                await crud.set_setting(self.db, "qq.official.last_group_openid", group_openid)
        except Exception:
            pass

    async def note_user_message(self) -> None:
        await crud.set_last_user_message(self.db)
        self.last_event_at = iso_now()

    def note_reply(self, text: str, character_name: str = "") -> None:
        self.last_reply_at = iso_now()
        self.last_reply_preview = "%s：%s" % (character_name, truncate(text, 60)) if character_name else truncate(text, 60)

    def note_error(self, message: str) -> None:
        self.last_error = message

    # ============================================================== 便捷查询
    async def enabled_characters(self) -> List[Dict[str, Any]]:
        assert self.registry is not None
        return await self.registry.enabled()

    async def find_character(self, name: str) -> Optional[Dict[str, Any]]:
        from .database import crud

        row = await crud.get_character_by_name(self.db, name)
        return row

    async def character_map(self) -> Dict[str, str]:
        """``角色 id → 名字``，用于界面显示机器人绑定的角色。"""
        assert self.registry is not None
        rows = await self.registry.list_characters()
        return {str(item.get("id")): str(item.get("name") or "") for item in rows}

