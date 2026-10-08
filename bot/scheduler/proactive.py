"""主动消息调度。

三种触发方式：

* ``scheduled`` —— APScheduler ``CronTrigger``，每天固定时刻（如 09:00 / 21:00）
* ``idle`` —— ``IntervalTrigger`` 周期检查“用户多久没说话”
* ``random`` —— 在活跃时段内随机安排一个 ``DateTrigger``，触发后重新排下一次

所有触发都会经过统一的准入检查（免打扰 / 活跃时段 / 频率限制 / 最小间隔 / 概率），
被跳过的原因会写入日志，并能通过 API 返回给 GUI。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from common.async_utils import LoopSafeLock
from common.logging_setup import get_logger
from common.utils import iso_now, now, parse_hhmm, seconds_since, truncate

from ..database import crud
from . import triggers

log = get_logger("bot.scheduler")


class _SectionView:
    """把某个机器人的生效 proactive 段包装成触发函数认识的点号配置视图。

    ``triggers.check_*`` 一律读 ``proactive.xxx`` 键；这里把「全局 + 该机器人
    覆盖」合并后的段按同一套键名暴露，触发函数无需感知按机器人覆盖的存在。
    """

    def __init__(self, section: Dict[str, Any]):
        self._section = section or {}

    def get(self, key: Any, default: Any = None) -> Any:
        if isinstance(key, str) and key.startswith("proactive."):
            return self._section.get(key.split(".", 1)[1], default)
        return default


def _bot_section(bot: Any) -> Dict[str, Any]:
    """机器人的生效 proactive 段（取不到时回退全局，兼容假 runtime 的测试对象）。"""
    try:
        return bot.effective_proactive()
    except Exception:  # pragma: no cover
        config = getattr(bot, "rt", None)
        return dict((getattr(config, "data", {}) or {}).get("proactive", {}) or {})


def _bot_media_section(bot: Any) -> Dict[str, Any]:
    """机器人的生效 media 段（取不到时返回 None = 读全局）。"""
    try:
        return bot.effective_media()
    except Exception:  # pragma: no cover
        return None

try:  # APScheduler 3.x
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except Exception:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore
    CronTrigger = DateTrigger = IntervalTrigger = None  # type: ignore


class ProactiveScheduler:
    def __init__(self, runtime: Any):
        self.rt = runtime
        self.scheduler: Optional[Any] = None
        self._lock = LoopSafeLock()
        self.last_result: Dict[str, Any] = {}
        self.last_skip_reason: str = ""

    # ============================================================== 生命周期
    @property
    def running(self) -> bool:
        return bool(self.scheduler and getattr(self.scheduler, "running", False))

    def start(self) -> None:
        if AsyncIOScheduler is None:  # pragma: no cover
            log.error("APScheduler 未安装，主动消息功能不可用")
            return
        if self.scheduler is None:
            try:
                from tzlocal import get_localzone

                timezone = get_localzone()
            except Exception:  # pragma: no cover
                timezone = None
            self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._install_jobs()
        if not self.scheduler.running:
            self.scheduler.start()
        log.info("主动消息调度器已启动")

    def shutdown(self) -> None:
        if self.scheduler and self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:  # pragma: no cover
                pass
        self.scheduler = None
        log.info("主动消息调度器已停止")

    def reschedule(self) -> None:
        """配置变更后重建任务。"""
        if self.scheduler is None:
            return
        try:
            self._install_jobs()
        except Exception as exc:  # pragma: no cover
            log.warning("重建调度任务失败: %s", exc)

    # ============================================================== 任务安装
    def _add(self, func, trigger, job_id: str, name: str) -> None:
        """注册一个调度任务。

        ``func`` 必须本身是**协程函数**（可以是 ``(协程函数, kwargs 字典)`` 元组
        来附带参数）。不要用同步 lambda 包装：APScheduler 只认协程函数，
        同步函数会被丢进线程池执行，lambda 里产生的协程永远不会被 await，
        任务「到点静默消失」（V0.2.2 曾因此踩坑）。
        """
        assert self.scheduler is not None
        kwargs: Dict[str, Any] = {}
        if isinstance(func, tuple):
            func, kwargs = func[0], dict(func[1])
        try:
            self.scheduler.add_job(
                func,
                trigger=trigger,
                id=job_id,
                name=name,
                replace_existing=True,
                misfire_grace_time=120,
                coalesce=True,
                max_instances=1,
                kwargs=kwargs or None,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("注册任务 %s 失败: %s", job_id, exc)

    def _install_jobs(self) -> None:
        """按「每个已启用机器人一套任务」安装（V0.2.2）。

        定时 / 空闲 / 随机三类触发都是**每机器人独立**的：各自的配置（「消息
        设置」页可按机器人单独设置，未单独设置的跟随全局）、时间抖动、概率
        判定、频率限制与重排，多机器人不再同一秒集体发消息。
        """
        if self.scheduler is None:
            return
        for job in list(self.scheduler.get_jobs()):
            try:
                self.scheduler.remove_job(job.id)
            except Exception:
                pass

        bots = list(self.rt.enabled_bots())

        # 定时触发：每个机器人用自己的时间点（各自配置）
        for bot in bots:
            section = _bot_section(bot)
            if not bool(section.get("scheduled_enabled", True)):
                continue
            for item in section.get("scheduled_times", []) or []:
                hour, minute = parse_hhmm(item, (-1, -1))
                if hour < 0:
                    continue
                # 必须直接传协程函数（bound method）+ kwargs：若用同步 lambda 包装，
                # APScheduler 会判定为普通函数丢进线程池，lambda 返回的协程永远不会
                # 被 await，任务「到点静默消失」（V0.2.2 曾因此踩坑）。
                self._add(
                    (self._job_scheduled, {"bot_id": bot.id}),
                    CronTrigger(hour=hour, minute=minute, jitter=90),
                    "proactive_scheduled_%02d%02d_%s" % (hour, minute, bot.id),
                    "定时主动消息 %02d:%02d · %s" % (hour, minute, bot.name),
                )

        # 空闲触发：每个机器人用自己的检查周期
        for bot in bots:
            section = _bot_section(bot)
            if not bool(section.get("idle_enabled", True)):
                continue
            interval = max(1, int(section.get("idle_check_interval_minutes", 15) or 15))
            self._add(
                (self._job_idle, {"bot_id": bot.id}),
                IntervalTrigger(minutes=interval, jitter=60),
                "proactive_idle_%s" % bot.id,
                "空闲检查 · %s（每 %d 分钟）" % (bot.name, interval),
            )

        # 随机触发：每个机器人各自独立的重排链（时间互不相同）
        for bot in bots:
            section = _bot_section(bot)
            if bool(section.get("random_enabled", False)):
                self._schedule_next_random(bot.id, bot.name)

        log.info(
            "主动消息调度任务安装完成：共 %d 个",
            len(self.scheduler.get_jobs()),
        )

        # 配置热重载
        self._add(
            self._job_watch_config,
            IntervalTrigger(seconds=30),
            "config_watch",
            "配置热重载检查",
        )

    def _schedule_next_random(self, bot_id: str = "", bot_name: str = "") -> None:
        assert self.scheduler is not None
        bot = self.rt.bot_by_id(bot_id)
        if bot is not None:
            section = _bot_section(bot)
        else:
            section = self.rt.config.get("proactive", {}) or {}
        when = triggers.next_random_time(_SectionView(section))
        delay_minutes = max(0.0, (when - now()).total_seconds() / 60)
        log.info(
            "机器人「%s」的下次随机主动消息安排在 %s（%.0f 分钟后）",
            bot_name or bot_id or "默认",
            when.strftime("%m-%d %H:%M"),
            delay_minutes,
        )
        self._add(
            (self._job_random, {"bot_id": bot_id}),
            DateTrigger(run_date=when),
            "proactive_random_%s" % (bot_id or "default"),
            "随机主动消息 · %s" % (bot_name or "默认"),
        )

    # ================================================================== 任务
    async def _job_scheduled(self, bot_id: str) -> None:
        log.info("定时主动消息任务触发（bot=%s）", bot_id)
        await self.run("scheduled", bot_id=bot_id)

    async def _job_idle(self, bot_id: str) -> None:
        await self.run("idle", bot_id=bot_id)

    async def _job_random(self, bot_id: str) -> None:
        await self.run("random", bot_id=bot_id)
        bot = self.rt.bot_by_id(bot_id)
        if bot is not None and bool(_bot_section(bot).get("random_enabled", False)):
            self._schedule_next_random(bot_id, bot.name)

    async def _job_watch_config(self) -> None:
        try:
            if self.rt.sync_config():
                log.info("检测到 config.yaml 变更，已热重载")
                self.rt.apply_config()
                self.reschedule()
                self.rt.publish({"type": "config_reloaded"})
        except Exception as exc:  # pragma: no cover
            log.warning("配置热重载失败: %s", exc)

    # ================================================================ 准入检查
    async def _evaluate_for_bot(self, bot: Any, trigger_type: str, force: bool) -> Tuple[bool, str]:
        """按**这个机器人自己的生效配置**（全局 + 它自己的覆盖）做准入检查。"""
        if force:
            return True, ""

        view = _SectionView(_bot_section(bot))
        decisions = [
            triggers.check_proactive_enabled(view),
            triggers.check_dnd(view),
            triggers.check_active_hours(view),
        ]
        for decision in decisions:
            if not decision.allowed:
                return False, decision.reason

        if trigger_type == "idle":
            last_user = await crud.get_last_user_message(self.rt.db)
            decision = triggers.check_idle(view, last_user)
            if not decision.allowed:
                return False, decision.reason

        # 每日上限与最小间隔都按**单个机器人**独立计算
        today_total = await crud.proactive_count_today(self.rt.db, bot_id=bot.id)
        decision = triggers.check_global_limit(view, today_total)
        if not decision.allowed:
            return False, decision.reason

        last = await crud.last_proactive(self.rt.db, bot_id=bot.id)
        decision = triggers.check_min_interval(view, (last or {}).get("sent_at"))
        if not decision.allowed:
            return False, decision.reason

        decision = triggers.check_probability(view)
        if not decision.allowed:
            return False, decision.reason

        return True, ""

    # ================================================================ 主流程
    async def run(
        self,
        trigger_type: str = "manual",
        force: bool = False,
        character_id: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self._lock.locked():
            return self._skip(trigger_type, "上一条主动消息仍在生成中")

        async with self._lock:
            try:
                return await self._run_inner(trigger_type, force, character_id, bot_id)
            except Exception as exc:
                log.exception("主动消息流程异常: %s", exc)
                result = {"ok": False, "skipped": True, "reason": "内部错误: %s" % exc, "scheduled": False, "trigger": trigger_type}
                self.last_result = result
                self.rt.publish({"type": "proactive_failed", "trigger": trigger_type, "error": str(exc)})
                return result

    def _targets(self, trigger_type: str, bot_id: Optional[str]) -> Tuple[List[Any], str]:
        """本次要发言的机器人列表。

        * 指定了 ``bot_id`` → 只让那个机器人发言；``bot_id="all"`` 表示所有已启用的机器人；
        * 手动触发（界面上的「立即触发一次」）未指定时 → 只用第一个机器人，
          避免用户点一下就让所有机器人一起发消息；
        * 定时/空闲/随机触发 → 所有已启用的机器人各自发言（各自的角色与目标）。
        """
        if bot_id == "all":
            bots = self.rt.enabled_bots()
            if not bots:
                return [], "没有已启用的机器人，请先在「机器人管理」里添加并启用"
            return bots, ""
        if bot_id:
            bot = self.rt.bot_by_id(bot_id)
            if bot is None:
                return [], "指定的机器人不存在（可能已被删除）"
            if not bot.enabled:
                return [], "机器人「%s」已被停用" % bot.name
            return [bot], ""

        bots = self.rt.enabled_bots()
        if not bots:
            return [], "没有已启用的机器人，请先在「机器人管理」里添加并启用"
        if trigger_type == "manual":
            primary = self.rt.primary_bot()
            return ([primary] if primary is not None else bots[:1]), ""
        return bots, ""

    async def _run_inner(
        self,
        trigger_type: str,
        force: bool,
        character_id: Optional[str],
        bot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.rt.sync_config():
            self.reschedule()

        targets, reason = self._targets(trigger_type, bot_id)
        if not targets:
            return self._skip(trigger_type, reason)

        outcomes: List[Dict[str, Any]] = []
        # 同一轮里已经选过的角色（多机器人同轮触发时避免都用同一个角色，
        # 让几条主动消息内容各不相同）
        used_character_ids: set = set()
        for bot in targets:
            # 准入检查按**该机器人自己的生效配置**（V0.2.2 起可按机器人设置）
            allowed, deny_reason = await self._evaluate_for_bot(bot, trigger_type, force)
            if not allowed:
                outcomes.append(
                    {
                        "ok": False,
                        "skipped": True,
                        "reason": deny_reason,
                        "bot_id": bot.id,
                        "bot_name": bot.name,
                        "trigger": trigger_type,
                    }
                )
                self.rt.publish(
                    {
                        "type": "proactive_skipped",
                        "reason": deny_reason,
                        "bot_id": bot.id,
                        "bot_name": bot.name,
                    }
                )
                continue
            outcome = await self._run_for_bot(bot, trigger_type, force, character_id, used_character_ids)
            outcomes.append(outcome)
            picked = str(outcome.get("character_id") or "")
            if picked:
                used_character_ids.add(picked)
            # 定时/空闲/随机：每个机器人都要发；但配置错误（被跳过）没必要反复重试
            if outcome.get("ok"):
                continue

        successes = [item for item in outcomes if item.get("ok")]
        if successes:
            result = dict(successes[0])
            result["bots"] = outcomes
            if len(successes) > 1:
                result["sent_bots"] = [item.get("bot_name") for item in successes]
            self.last_result = result
            self.last_skip_reason = ""
            return result

        # 全部失败/跳过：把每个机器人的原因合并成一个可读的说明
        reasons = [
            "%s：%s" % (item.get("bot_name") or item.get("bot_id") or "机器人", item.get("reason") or "未知原因")
            for item in outcomes
        ]
        result = self._skip(trigger_type, "；".join(reasons) or "没有可用的机器人")
        result["bots"] = outcomes
        return result

    async def _run_for_bot(
        self,
        bot: Any,
        trigger_type: str,
        force: bool,
        character_id: Optional[str],
        used_character_ids: Optional[set] = None,
    ) -> Dict[str, Any]:
        """让一个机器人按它自己绑定的角色发一条主动消息。"""
        base = {"bot_id": bot.id, "bot_name": bot.name, "trigger": trigger_type}

        def _fail(reason: str, skipped: bool = True) -> Dict[str, Any]:
            log.info("[%s] 机器人「%s」跳过：%s", triggers.trigger_label(trigger_type), bot.name, reason)
            if skipped:
                self.rt.publish({"type": "proactive_skipped", "reason": reason, **base})
            return {"ok": False, "skipped": skipped, "reason": reason, **base}

        # ---------------------------------------------------------- 目标
        peer, reason = await bot.messaging.target_peer()
        if peer is None:
            return _fail(reason)

        # ---------------------------------------------------------- 选角色
        characters = await self.rt.registry.enabled()  # type: ignore[union-attr]
        if not characters:
            return _fail("没有启用中的角色，请先在「角色管理」里导入并启用角色")

        if character_id:
            pool = [item for item in characters if str(item.get("id")) == str(character_id)]
            if not pool:
                return _fail("指定的角色不存在或未启用")
            bound_forced = True
        elif bot.character_id:
            bound = await bot.resolved_character()
            if bound is None:
                return _fail(
                    "机器人「%s」绑定的角色不存在或已停用，请在「机器人管理」里重新指定"
                    % bot.name
                )
            pool = [bound]
            bound_forced = True
        else:
            pool = characters
            bound_forced = False
            # 同轮里其它机器人已经选走的角色优先避开（绑定指定时不回避）
            if used_character_ids:
                filtered = [
                    item for item in pool if str(item.get("id")) not in used_character_ids
                ]
                if filtered:
                    pool = filtered

        counts = await crud.proactive_counts_today_by_character(self.rt.db)
        last = await crud.last_proactive(self.rt.db)
        last_character_id = str((last or {}).get("character_id") or "")

        candidates = triggers.filter_candidates(
            pool,
            _SectionView(_bot_section(bot)),
            counts,
            last_character_id,
            force=force or bound_forced or bool(character_id),
        )
        if not candidates:
            return _fail("所有可用角色都已达到今日发言上限")

        character = triggers.weighted_pick_character(candidates, counts)
        if not character:
            return _fail("未能选出合适的角色")

        # ---------------------------------------------------------- 生成
        hint = self._hint_text(trigger_type)
        hub = getattr(self.rt, "media", None)
        media_section = _bot_media_section(bot)
        if hub is not None:
            try:
                media_hint = hub.media_hint(media_section)
                if media_hint:
                    hint = (hint + "\n" + media_hint).strip()
            except Exception:  # pragma: no cover
                pass
        outcome = await self.rt.engine.proactive(character, trigger_type=trigger_type, hint=hint)  # type: ignore[union-attr]
        content = str(outcome.get("content") or "").strip()
        if not content:
            await crud.log_proactive(
                self.rt.db,
                str(character.get("id") or ""),
                trigger_type,
                content="",
                status="failed",
                character_name=str(character.get("name") or ""),
                bot_id=bot.id,
                bot_name=bot.name,
            )
            return {
                "ok": False,
                "skipped": False,
                "reason": "消息生成失败：%s" % (outcome.get("error") or "未知错误"),
                "character": character.get("name"),
                **base,
            }

        # ---------------------------------------------------------- 发送（含语音 / 图片多媒体）
        from ..media.hub import OutgoingReply

        out = OutgoingReply(text=content, body=content)
        if hub is not None:
            try:
                out = await hub.compose(character, content, media_section)
            except Exception as exc:
                log.warning("主动消息多媒体组装失败（按纯文字继续）：%s", exc)
                out = OutgoingReply(text=content, body=content)
            outcome_send = await hub.send_outgoing(
                bot,
                out,
                is_group=bool(peer.is_group),
                peer_id="" if peer.is_group else peer.peer_id,
                group_id=peer.peer_id if peer.is_group else "",
            )
        else:
            outcome_send = await bot.messaging.send_text(
                content,
                peer,
                character_name=str(character.get("name") or ""),
            )
        if not outcome_send.get("ok"):
            exc = outcome_send.get("error") or "未知错误"
            log.error("主动消息发送失败（机器人 %s / 角色 %s）：%s", bot.name, character.get("name"), exc)
            await crud.log_proactive(
                self.rt.db,
                str(character.get("id") or ""),
                trigger_type,
                content=content,
                status="failed",
                character_name=str(character.get("name") or ""),
                bot_id=bot.id,
                bot_name=bot.name,
            )
            self.rt.publish(
                {
                    "type": "proactive_failed",
                    "trigger": trigger_type,
                    "character": character.get("name"),
                    "error": str(exc),
                    **base,
                }
            )
            return {
                "ok": False,
                "skipped": False,
                "reason": "发送失败：%s" % exc,
                "trigger": trigger_type,
                "character": character.get("name"),
                "content": content,
                **base,
            }

        await crud.log_proactive(
            self.rt.db,
            str(character.get("id") or ""),
            trigger_type,
            content=content,
            status="degraded" if outcome.get("degraded") else "sent",
            character_name=str(character.get("name") or ""),
            bot_id=bot.id,
            bot_name=bot.name,
        )
        # V0.2.2：主动消息以图片/语音发出时，记录里那条消息标记为带媒体
        if out.has_media:
            try:
                if out.voice_paths:
                    await crud.update_last_message_media(
                        self.rt.db, str(character.get("id") or ""), "voice", str(out.voice_paths[0])
                    )
                elif out.image_path:
                    await crud.update_last_message_media(
                        self.rt.db, str(character.get("id") or ""), "image", str(out.image_path)
                    )
            except Exception as exc:  # pragma: no cover
                log.debug("标记主动消息媒体记录失败：%s", exc)
        # V0.2.2：顺手压缩过期对话（后台跑）
        try:
            asyncio.create_task(self.rt.maybe_summarize(str(character.get("id") or "")))
        except Exception:  # pragma: no cover
            pass
        elapsed = seconds_since(iso_now()) or 0

        result = {
            "ok": True,
            "skipped": False,
            "reason": "",
            "trigger": trigger_type,
            "trigger_label": triggers.trigger_label(trigger_type),
            "character": character.get("name"),
            "character_id": character.get("id"),
            "content": content,
            "degraded": bool(outcome.get("degraded")),
            "error": outcome.get("error", ""),
            "sent_at": iso_now(),
            "elapsed": elapsed,
            "peer": peer.peer_id,
            "mode": bot.mode,
            **base,
        }
        log.info(
            "[%s] 机器人「%s」（%s）已向 %s 发送主动消息（角色：%s）：%s",
            triggers.trigger_label(trigger_type),
            bot.name,
            bot.mode_label,
            peer.peer_id,
            character.get("name"),
            truncate(content, 50),
        )
        self.rt.publish({"type": "proactive_sent", **result})
        return result

    def _skip(self, trigger_type: str, reason: str) -> Dict[str, Any]:
        self.last_skip_reason = reason
        result = {
            "ok": False,
            "skipped": True,
            "reason": reason,
            "trigger": trigger_type,
            "trigger_label": triggers.trigger_label(trigger_type),
        }
        self.last_result = result
        log.info("[%s] 跳过：%s", triggers.trigger_label(trigger_type), reason)
        self.rt.publish({"type": "proactive_skipped", **result})
        return result

    # ================================================================ 辅助
    def _hint_text(self, trigger_type: str) -> str:
        if trigger_type == "idle":
            hours = self.rt.config.get("proactive.idle_hours", 6)
            return "对方已经大约 %s 小时没有说话了。" % hours
        if trigger_type == "random":
            return "你只是忽然想起了对方。"
        if trigger_type == "manual":
            return "（这是手动触发，请随意发一条自然的问候。）"
        return "现在是 %s。" % now().strftime("%H:%M")

    async def trigger_manual(
        self, character_id: Optional[str] = None, bot_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """GUI 的“立即触发一次”按钮。"""
        return await self.run("manual", force=True, character_id=character_id, bot_id=bot_id)

    def jobs_info(self) -> List[Dict[str, Any]]:
        if not self.scheduler:
            return []
        info: List[Dict[str, Any]] = []
        for job in self.scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            info.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "",
                    "trigger": str(job.trigger),
                }
            )
        return info

    def status(self) -> Dict[str, Any]:
        config = self.rt.config
        bots = [bot.status() for bot in getattr(self.rt, "bots", [])]
        # 每个机器人自己的主动消息生效状态（全局 + 它自己的覆盖）
        proactive_by_id: Dict[str, bool] = {}
        for bot in getattr(self.rt, "bots", []):
            try:
                proactive_by_id[str(bot.id)] = bool(_bot_section(bot).get("enabled", True))
            except Exception:  # pragma: no cover
                proactive_by_id[str(bot.id)] = True
        return {
            "running": self.running,
            "enabled": bool(config.get("proactive.enabled", True)),
            "jobs": self.jobs_info(),
            "last_result": self.last_result,
            "last_skip_reason": self.last_skip_reason,
            "next_runs": [item for item in self.jobs_info() if item.get("next_run")][:6],
            "bots": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "enabled": item.get("enabled"),
                    "connected": item.get("connected"),
                    "character_name": item.get("character_name") or "",
                    "target": item.get("target") or "",
                    "proactive_enabled": proactive_by_id.get(str(item.get("id")), True),
                }
                for item in bots
            ],
        }
