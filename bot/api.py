"""对外 HTTP API（GUI 与 Bot 进程之间的控制通道）。

所有接口挂在 ``/api`` 前缀下，状态推送走 ``/ws/events``。
当 ``api.token`` 非空时，请求需带 ``X-Tavern-Token`` 头或 ``?token=`` 查询参数。
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.config import DEFAULTS, deep_merge, to_plain
from common.logging_setup import get_logger, tail_file
from common.paths import data_dir, logs_dir
from common.utils import iso_now, truncate

from . import __version__
from .runtime import Runtime

log = get_logger("bot.api")

try:
    from fastapi import (
        APIRouter,
        Body,
        Depends,
        File,
        HTTPException,
        Query,
        Request,
        UploadFile,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.responses import FileResponse, JSONResponse

    _FASTAPI_READY = True
except Exception as exc:  # pragma: no cover
    _FASTAPI_READY = False
    log.error("FastAPI 不可用: %s", exc)

_runtime: Optional[Runtime] = None
_shutdown_handler: Optional[Any] = None


def _b64_preview(data: bytes, limit_kb: int = 2048) -> str:
    """TTS 试听用的 base64（GUI 与 Bot 同机走 127.0.0.1，直接给完整音频；
    2MB 上限只是防极端长文本把接口载荷撑爆）。"""
    import base64

    return base64.b64encode(data[: limit_kb * 1024]).decode("ascii")


def iso_now_from_stat(mtime: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")


def set_runtime(runtime: Runtime) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未初始化")
    return _runtime


def set_shutdown_handler(handler: Any) -> None:
    global _shutdown_handler
    _shutdown_handler = handler


def _check_token(request: Request) -> None:
    runtime = get_runtime()
    token = str(runtime.config.get("api.token", "") or "").strip()
    if not token:
        return
    provided = request.headers.get("x-tavern-token") or request.query_params.get("token")
    if provided != token:
        raise HTTPException(status_code=401, detail="invalid token")


router = APIRouter(prefix="/api", dependencies=[Depends(_check_token)])


# ============================================================== 基础状态 =====
@router.get("/health")
async def health() -> Dict[str, Any]:
    runtime = _runtime
    return {
        "ok": True,
        "version": __version__,
        "ready": bool(runtime and runtime.ready),
        "started_at": runtime.started_at if runtime else "",
    }


@router.get("/status")
async def status() -> Dict[str, Any]:
    runtime = get_runtime()
    return await runtime.snapshot()


@router.get("/stats/today")
async def stats_today() -> Dict[str, Any]:
    from .database import crud

    runtime = get_runtime()
    return await crud.stats_today(runtime.db)


@router.get("/logs")
async def logs(
    source: str = Query("bot", description="bot / gui"),
    lines: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    filename = "bot.log" if source == "bot" else "gui.log"
    path = logs_dir() / filename
    return {
        "source": source,
        "path": str(path),
        "lines": tail_file(path, lines=lines),
    }


# ============================================================== Bot 控制 =====
@router.post("/bot/start")
async def bot_start() -> Dict[str, Any]:
    runtime = get_runtime()
    if runtime.scheduler.running:
        return {"ok": True, "already_running": True, "message": "Bot 已在运行"}
    runtime.scheduler.start()
    runtime.publish({"type": "bot_started"})
    log.info("Bot 已启动（主动消息调度开启）")
    return {"ok": True, "already_running": False, "message": "Bot 已启动"}


@router.post("/bot/stop")
async def bot_stop() -> Dict[str, Any]:
    runtime = get_runtime()
    if not runtime.scheduler.running:
        return {"ok": True, "already_stopped": True, "message": "Bot 已停止"}
    runtime.scheduler.shutdown()
    runtime.publish({"type": "bot_stopped"})
    log.info("Bot 已停止（主动消息调度关闭）")
    return {"ok": True, "already_stopped": False, "message": "Bot 已停止"}


@router.post("/bot/restart")
async def bot_restart() -> Dict[str, Any]:
    runtime = get_runtime()
    runtime.scheduler.shutdown()
    await asyncio.sleep(0.2)
    runtime.scheduler.start()
    runtime.publish({"type": "bot_started"})
    return {"ok": True, "message": "Bot 已重启"}


@router.post("/shutdown")
async def shutdown() -> Dict[str, Any]:
    runtime = get_runtime()
    runtime.publish({"type": "shutting_down"})

    def _trigger() -> None:  # pragma: no cover - 进程退出路径
        try:
            if _shutdown_handler is not None:
                _shutdown_handler()
            else:
                signal.raise_signal(signal.SIGINT)
        except Exception:
            os._exit(0)

    asyncio.get_event_loop().call_later(0.4, _trigger)
    return {"ok": True, "message": "正在退出"}


# ========================================================== 机器人管理 =====
@router.get("/bots")
async def list_bots() -> Dict[str, Any]:
    """所有机器人（账号）及其连接状态、绑定角色。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    characters = await runtime.character_map()
    items = []
    for bot in runtime.bots:
        state = bot.status()
        state["character_name"] = state.get("character_name") or characters.get(bot.character_id, "")
        items.append(state)
    return {
        "ok": True,
        "count": len(items),
        "bots": items,
        "characters": [{"id": key, "name": value} for key, value in characters.items()],
    }


@router.post("/bots")
async def create_bot(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """新增一个机器人（写入配置的 ``bots:`` 段）。"""
    from common.bots import new_bot_entry, raw_bot_entries

    runtime = get_runtime()
    name = str(payload.get("name") or "").strip()
    entry = new_bot_entry(runtime.config, name)
    if payload.get("mode"):
        entry["mode"] = str(payload["mode"])
    if payload.get("character_id"):
        entry["character_id"] = str(payload["character_id"])
    entries = raw_bot_entries(runtime.config)
    entries.append(entry)
    runtime.config.patch({"bots": entries}, persist=True)
    runtime.config.load(force=True)
    runtime.apply_config()
    runtime.publish({"type": "bots_changed"})
    return {"ok": True, "bot": entry, "bots": await runtime.bots_status()}


@router.put("/bots/{bot_id}")
async def update_bot(bot_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """更新一个机器人（第 1 个机器人写在 ``qq:`` 段，其余写在 ``bots:`` 段）。"""
    from common.bots import patch_for_bot

    runtime = get_runtime()
    bot = runtime.bot_by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    values = {key: value for key, value in (payload or {}).items() if key != "index"}
    if not values:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    patch = patch_for_bot(runtime.config, bot.index, values)
    runtime.config.patch(patch, persist=True)
    runtime.config.load(force=True)
    runtime.apply_config()
    runtime.publish({"type": "bots_changed"})
    updated = runtime.bot_by_id(bot_id)
    return {"ok": True, "bot": updated.status() if updated is not None else {}}


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: str) -> Dict[str, Any]:
    """删除一个机器人（不能删除第一个机器人，它是 ``qq:`` 段）。"""
    from common.bots import raw_bot_entries

    runtime = get_runtime()
    bot = runtime.bot_by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    if bot.index == 0:
        raise HTTPException(status_code=400, detail="不能删除第一个机器人，可以把它停用")
    entries = [item for item in raw_bot_entries(runtime.config) if str(item.get("id") or "") != bot_id]
    runtime.config.patch({"bots": entries}, persist=True)
    runtime.config.load(force=True)
    runtime.apply_config()
    runtime.publish({"type": "bots_changed"})
    return {"ok": True, "bots": await runtime.bots_status()}


@router.post("/bots/{bot_id}/test")
async def test_bot(bot_id: str) -> Dict[str, Any]:
    """测试某个机器人的连接（凭证 → 机器人信息 → 网关地址）。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    bot = runtime.bot_by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    info = await bot.probe()
    info["bot_id"] = bot.id
    info["bot_name"] = bot.name
    return info


@router.post("/bots/{bot_id}/reconnect")
async def reconnect_bot(bot_id: str) -> Dict[str, Any]:
    """重连某个机器人（重建官方网关连接，并强制刷新 access_token）。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    bot = runtime.bot_by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    await bot.stop_gateway()
    started = await bot.start_gateway()
    return {
        "ok": started,
        "bot_id": bot.id,
        "mode": "official",
        "message": "「%s」的官方机器人网关已重新连接" % bot.name
        if started
        else "「%s」未配置 AppID / AppSecret，无法连接" % bot.name,
    }


@router.post("/bots/{bot_id}/forget-openid")
async def forget_bot_openid(bot_id: str) -> Dict[str, Any]:
    """清除某个机器人自动记住的 openid（换人聊天时用）。"""
    runtime = get_runtime()
    bot = runtime.bot_by_id(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    await bot.forget_openid()
    if bot.index == 0:
        runtime.last_user_openid = ""
        runtime.last_group_openid = ""
    return {"ok": True, "bot_id": bot.id}


# ========================================================== QQ 连接状态 =====
@router.get("/qq/status")
async def qq_status(bot_id: str = Query("", description="机器人 id，留空表示第一个机器人")) -> Dict[str, Any]:
    """某个机器人（默认第一个）的连接状态。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    bot = runtime.bot_by_id(bot_id) if bot_id else runtime.primary_bot()
    state = await runtime.qq_status(bot_id)
    state["reply_enabled"] = bool(
        bot.spec.qq("reply_enabled", True) if bot is not None else runtime.config.get("qq.reply_enabled", True)
    )
    state["group_reply_enabled"] = bool(
        bot.spec.qq("group_reply_enabled", False)
        if bot is not None
        else runtime.config.get("qq.group_reply_enabled", False)
    )
    state["last_user_openid"] = (bot.last_user_openid if bot is not None else runtime.last_user_openid)
    state["last_group_openid"] = (bot.last_group_openid if bot is not None else runtime.last_group_openid)
    state["bots"] = await runtime.bots_status()
    return state


@router.post("/qq/test")
async def qq_test(bot_id: str = Query("", description="机器人 id，留空表示第一个机器人")) -> Dict[str, Any]:
    """测试连接：凭证 → 机器人信息 → 网关地址。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    bot = runtime.bot_by_id(bot_id) if bot_id else runtime.primary_bot()
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    info = await bot.probe()
    info["mode"] = bot.mode
    info["mode_label"] = bot.mode_label
    info["bot_id"] = bot.id
    info["bot_name"] = bot.name
    return info


@router.post("/qq/reconnect")
async def qq_reconnect(bot_id: str = Query("", description="机器人 id，留空表示第一个机器人")) -> Dict[str, Any]:
    """重连官方网关（会重建连接并刷新 access_token）。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    bot = runtime.bot_by_id(bot_id) if bot_id else runtime.primary_bot()
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    await bot.stop_gateway()
    started = await bot.start_gateway()
    return {
        "ok": started,
        "bot_id": bot.id,
        "mode": "official",
        "message": "官方机器人网关已重新连接" if started else "未配置 AppID / AppSecret，无法连接",
    }


@router.post("/qq/forget-openid")
async def qq_forget_openid(bot_id: str = Query("", description="机器人 id，留空表示第一个机器人")) -> Dict[str, Any]:
    """清除自动记住的官方平台 openid（换人聊天时用）。"""
    runtime = get_runtime()
    bot = runtime.bot_by_id(bot_id) if bot_id else runtime.primary_bot()
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人不存在")
    await bot.forget_openid()
    if bot.index == 0:
        runtime.last_user_openid = ""
        runtime.last_group_openid = ""
    return {"ok": True, "bot_id": bot.id}


# ============================================================== LLM 测试 =====
@router.post("/llm/test")
async def llm_test() -> Dict[str, Any]:
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    runtime.engine.apply_config(runtime.config)  # type: ignore[union-attr]
    return await runtime.engine.test_llm()  # type: ignore[union-attr]


# ========================================================= 模型路由（V0.2） ====
class _SlotOverrideConfig:
    """测试线路用：把用户在表单里填的值（尚未保存）叠到已保存配置上。

    只影响 ``providers.<slot>`` 这一个节点，其余键透传已保存配置；
    表单里的空字符串字段按「没填」处理，保证测试结果与界面所见一致。
    """

    def __init__(self, base, slot: str, override: Dict[str, Any]) -> None:
        self._base = base
        self._slot = slot
        self._override = override

    def get(self, key, default=None):
        if key == "providers.%s" % self._slot:
            node = self._base.get(key)
            node = dict(node) if isinstance(node, dict) else {}
            for field, value in self._override.items():
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    node.pop(field, None)
                else:
                    node[field] = value
            return node
        return self._base.get(key, default)


@router.get("/providers")
async def providers_status() -> Dict[str, Any]:
    """各模型槽位（chat/vision/image/asr/tts）的配置与就绪状态。"""
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    return runtime.media.status()


@router.post("/providers/test")
async def providers_test(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """按当前配置测试某个槽位是否真正可用（GUI「模型路由」页的测试按钮）。"""
    from common.providers import (
        ALL_SLOTS,
        SLOT_IMAGE,
        SLOT_LABELS,
        SLOT_TTS,
        SLOT_VISION,
        load_slot,
    )

    runtime = get_runtime()
    runtime.config.reload_if_changed()
    slot = str(payload.get("slot") or "").strip()
    voice_override = str(payload.get("voice") or "").strip()  # tts 槽位试听指定音色
    if slot not in ALL_SLOTS:
        raise HTTPException(status_code=400, detail="未知的模型槽位：%s" % slot)
    # 表单当前值优先（未点「保存」也能按界面所见测试）：表单值叠在已保存配置上
    override = payload.get("values")
    if not isinstance(override, dict):
        override = {}
    cfg = _SlotOverrideConfig(runtime.config, slot, override) if override else runtime.config
    spec = load_slot(cfg, slot)
    if not spec.configured:
        label = SLOT_LABELS.get(slot, slot)
        missing = spec.missing_fields() or ["Base URL / 模型"]
        message = "%s还没填完整（缺：%s）" % (label, "、".join(missing))
        if "API Key" in missing:
            message += "（本地 / 局域网地址可留空）"
        if slot == SLOT_VISION:
            message += "。看图线路独立于图像生成，两条线路互不影响"
        return {"ok": False, "slot": slot, "message": message}
    from .ai_engine.llm_client import LLMClient
    from .media.images import ImageError, ImageGenerator, Vision, builtin_test_image_data_url
    from .media.voice import TTS, VoiceError

    try:
        if slot == "chat":
            client = LLMClient(
                base_url=spec.base_url,
                api_key=spec.api_key,
                model=spec.model,
                timeout=30,
                max_retries=0,
            )
            result = await client.test_connection()
            return {
                "ok": bool(result.get("ok")),
                "slot": slot,
                "message": result.get("reply") or result.get("error") or "",
                "detail": result,
            }
        if slot == SLOT_VISION:
            vision = Vision(spec, timeout=60)
            # 内置红色测试图（独立于图像生成）：能答出「红色」才算真正看了图
            answer = await vision.describe(
                builtin_test_image_data_url(),
                question="这张图片的主色是什么？只回答颜色。",
            )
            if answer:
                if any(word in answer.lower() for word in ("红", "red")):
                    return {"ok": True, "slot": slot, "message": "看图正常（内置红色测试图识别为红色）：%s" % truncate(answer, 40)}
                return {
                    "ok": False,
                    "slot": slot,
                    "message": "端点通了，但没认出内置红色测试图（回答：%s）——请确认所选模型支持看图" % truncate(answer, 60),
                }
            return {"ok": False, "slot": slot, "message": "端点通了，但没有返回内容"}
        if slot == SLOT_IMAGE:
            generator = ImageGenerator(spec, timeout=120)
            data, ext = await generator.generate("一只可爱的小猫，白色背景", size="512x512")
            return {
                "ok": True,
                "slot": slot,
                "message": "生成成功（%s，%d KB）" % (ext, len(data) // 1024),
            }
        if slot == SLOT_TTS:
            from .media.instruct import (
                generate_qwen_audio_tags,
                generate_tts_instruction,
                is_qwen_audio_tts_model,
                is_tts_instruct_model,
                local_qwen_audio_tags,
            )
            from .media.voice import random_preview_text

            tts = TTS(spec, timeout=60)
            # 试听文案：客户端可指定，否则从文案池随机取一句（多听几句才判断得准）
            text = str(payload.get("text") or "").strip() or random_preview_text()
            style: Dict[str, Any] = {}
            for key in ("rate", "volume", "pitch"):
                value = str(spec.extra.get(key) or "").strip()
                if value:
                    style[key] = value
            speed = str(spec.extra.get("speed") or "").strip()
            if speed:
                try:
                    style["speed"] = float(speed)
                except ValueError:
                    pass
            # 百炼两套官方调教机制：instruct 系列走指令，qwen-audio 系列走
            # 情感/拟声标签（text）+ 整体语气指令（instruction），主模型失败时用本地兜底
            speech_text = text
            instruction = ""
            if is_tts_instruct_model(spec.model):
                instruction = await generate_tts_instruction(runtime.engine.llm, text)
            elif is_qwen_audio_tts_model(spec.model):
                tag_result = await generate_qwen_audio_tags(runtime.engine.llm, text)
                if tag_result.get("text"):
                    speech_text = tag_result["text"]
                    instruction = tag_result.get("instruction") or ""
                else:
                    speech_text, instruction = local_qwen_audio_tags(text)
            result = await tts.synthesize(speech_text, voice=voice_override, instruction=instruction, **style)
            message = "合成成功（%s，%d KB）· 试听文案：%s" % (
                result["format"],
                len(result["data"]) // 1024,
                text,
            )
            if instruction and speech_text == text:
                message += " · 指令风格生效：%s" % truncate(instruction, 40)
            if speech_text != text:
                message += " · 情感标签生效：%s" % truncate(speech_text, 40)
                if instruction:
                    message += " · 语气指令：%s" % truncate(instruction, 30)
            return {
                "ok": True,
                "slot": slot,
                "message": message,
                "preview_b64": _b64_preview(result["data"]),
                "preview_text": speech_text,
            }
    except ImageError as exc:
        return {"ok": False, "slot": slot, "message": str(exc)}
    except VoiceError as exc:
        return {"ok": False, "slot": slot, "message": str(exc)}
    except Exception as exc:  # pragma: no cover
        log.exception("模型槽位测试失败（%s）", slot)
        return {"ok": False, "slot": slot, "message": "测试失败：%s" % exc}
    return {"ok": False, "slot": slot, "message": "未知的测试类型"}


@router.get("/media/voices")
async def media_voices(engine: str = Query("", description="可选：按引擎取音色清单（edge-tts / dashscope / openai）")) -> Dict[str, Any]:
    """可用音色清单：edge-tts 全量（中文在前）/ 百炼候选清单，供角色音色选择。"""
    from common.providers import ENGINE_DASHSCOPE, ENGINE_EDGE_TTS, ProviderSpec, SLOT_TTS, load_slot

    from .media.voice import TTS

    runtime = get_runtime()
    spec = load_slot(runtime.config, SLOT_TTS)
    engine = (engine or "").strip()
    if engine and engine in (ENGINE_EDGE_TTS, ENGINE_DASHSCOPE):
        spec = ProviderSpec(slot=SLOT_TTS, engine=engine, base_url=spec.base_url, api_key=spec.api_key, model=spec.model, voice=spec.voice)
    if spec.engine not in (ENGINE_EDGE_TTS, ENGINE_DASHSCOPE):
        return {"ok": True, "voices": []}
    voices = await TTS(spec).list_voices()
    return {"ok": True, "voices": voices}


@router.get("/media/file")
async def media_file(name: str = Query(..., description="媒体文件名（data/media 下）")):
    """给 GUI 的会话页 / 角色音色试听读取媒体文件。"""
    from .media import store

    get_runtime()  # 保证数据目录已就绪
    safe_name = Path(name).name
    for sub in (store.INBOX, store.OUTBOX):
        path = store.media_root() / sub / safe_name
        if path.is_file():
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".ogg": "audio/ogg",
            }.get(path.suffix.lower(), "application/octet-stream")
            return FileResponse(str(path), media_type=media_type)
    raise HTTPException(status_code=404, detail="媒体文件不存在")


@router.get("/media/inbox")
async def media_inbox(limit: int = Query(20, ge=1, le=200)) -> List[Dict[str, Any]]:
    """最近收到的图片 / 语音（会话页展示用）。"""
    from .media import store

    get_runtime()
    items = []
    for path in store.list_recent(store.INBOX, limit):
        try:
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size": int(stat.st_size),
                    "mtime": iso_now_from_stat(stat.st_mtime),
                }
            )
        except Exception:
            continue
    return items


# ============================================================ 角色管理 ======
@router.get("/characters")
async def list_characters(enabled_only: bool = Query(False)) -> List[Dict[str, Any]]:
    runtime = get_runtime()
    rows = await runtime.registry.list_characters(enabled_only=enabled_only)  # type: ignore[union-attr]
    bindings: Dict[str, List[Dict[str, Any]]] = {}
    for bot in runtime.bots:
        character_id = getattr(bot, "character_id", "")
        if character_id:
            bindings.setdefault(character_id, []).append(
                {"id": bot.id, "name": bot.name, "mode": bot.mode, "enabled": bot.enabled}
            )
    for row in rows:
        row["bound_bots"] = bindings.get(str(row.get("id")), [])
    return rows


@router.get("/characters/{character_id}")
async def get_character(character_id: str) -> Dict[str, Any]:
    runtime = get_runtime()
    row = await runtime.registry.get(character_id)  # type: ignore[union-attr]
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    row["bound_bots"] = [
        {"id": bot.id, "name": bot.name, "mode": bot.mode, "enabled": bot.enabled}
        for bot in runtime.bots
        if getattr(bot, "character_id", "") == str(character_id)
    ]
    return row


@router.post("/characters/import")
async def import_character(file: UploadFile = File(...), overwrite: bool = Query(True)) -> Dict[str, Any]:
    from .character_manager import CharacterCardError

    runtime = get_runtime()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        outcome = await runtime.registry.import_bytes(  # type: ignore[union-attr]
            file.filename or "character.png", data, overwrite=overwrite
        )
    except CharacterCardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail="导入失败: %s" % exc) from exc
    runtime.publish({"type": "characters_changed"})
    return {
        "ok": True,
        "status": outcome["status"],
        "character": outcome["character"],
        "summary": outcome["card"].summary(),
        "missing_core_fields": outcome["card"].missing_core_fields(),
    }


@router.post("/characters")
async def create_character(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """不依赖角色卡，直接用界面填写的内容创建角色。"""
    from .character_manager import CharacterCardError

    runtime = get_runtime()
    try:
        row = await runtime.registry.create(payload or {})  # type: ignore[union-attr]
    except CharacterCardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, "character": row}


@router.post("/characters/import-builtin")
async def import_builtin_characters() -> Dict[str, Any]:
    """导入随程序分发的内置角色（首次运行开箱可用）。"""
    runtime = get_runtime()
    outcome = await runtime.registry.import_builtin()  # type: ignore[union-attr]
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, **outcome}


@router.post("/characters/import-path")
async def import_character_from_path(
    path: str = Body(..., embed=True), overwrite: bool = Query(True)
) -> Dict[str, Any]:
    """从服务器本地路径导入（用户在 GUI 里粘贴路径时使用）。"""
    from .character_manager import CharacterCardError

    runtime = get_runtime()
    target = Path(path).expanduser()
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在: %s" % target)
    try:
        if target.is_dir():
            outcome = await runtime.registry.import_directory(target)  # type: ignore[union-attr]
            runtime.publish({"type": "characters_changed"})
            return {"ok": True, "directory": str(target), **outcome}
        result = await runtime.registry.import_file(target, overwrite=overwrite)  # type: ignore[union-attr]
    except CharacterCardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    runtime.publish({"type": "characters_changed"})
    return {
        "ok": True,
        "status": result["status"],
        "character": result["character"],
        "summary": result["card"].summary(),
        "missing_core_fields": result["card"].missing_core_fields(),
    }


@router.post("/characters/scan")
async def scan_characters_directory() -> Dict[str, Any]:
    """扫描 data/characters/ 目录并导入所有角色卡。"""
    runtime = get_runtime()
    directory = runtime.config.effective_characters_path()
    outcome = await runtime.registry.import_directory(directory)  # type: ignore[union-attr]
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, "directory": str(directory), **outcome}


@router.put("/characters/{character_id}")
async def update_character(character_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    runtime = get_runtime()
    allowed_keys = {
        "name",
        "description",
        "personality",
        "scenario",
        "first_mes",
        "mes_example",
        "system_prompt",
        "creator_notes",
        "tags",
        "tts_voice",
        "tts_rate",
        "tts_pitch",
        "tts_volume",
        "tts_speed",
        "sort_order",
    }
    fields = {key: value for key, value in (payload or {}).items() if key in allowed_keys}
    if not fields:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    row = await runtime.registry.update(character_id, fields)  # type: ignore[union-attr]
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, "character": row}


@router.patch("/characters/{character_id}/enabled")
async def set_character_enabled(character_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    runtime = get_runtime()
    enabled = bool(payload.get("enabled", True))
    row = await runtime.registry.set_enabled(character_id, enabled)  # type: ignore[union-attr]
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, "enabled": enabled, "character": row}


@router.delete("/characters/{character_id}")
async def delete_character(character_id: str) -> Dict[str, Any]:
    runtime = get_runtime()
    ok = await runtime.registry.delete(character_id)  # type: ignore[union-attr]
    if not ok:
        raise HTTPException(status_code=404, detail="角色不存在")
    runtime.publish({"type": "characters_changed"})
    return {"ok": True}


@router.get("/characters/{character_id}/avatar")
async def character_avatar(character_id: str) -> Any:
    runtime = get_runtime()
    row = await runtime.registry.get(character_id)  # type: ignore[union-attr]
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    path = runtime.registry.avatar_file(row)  # type: ignore[union-attr]
    if not path:
        raise HTTPException(status_code=404, detail="该角色没有头像")
    return FileResponse(str(path))


@router.put("/characters/{character_id}/avatar")
async def set_character_avatar(character_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    """替换角色头像（用户在界面上自选图片）。"""
    from .character_manager import CharacterCardError

    runtime = get_runtime()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="头像文件为空")
    suffix = Path(file.filename or "avatar.png").suffix or ".png"
    try:
        row = await runtime.registry.update_avatar(character_id, data, suffix)  # type: ignore[union-attr]
    except CharacterCardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="角色不存在")
    runtime.publish({"type": "characters_changed"})
    return {"ok": True, "character": row}


# ============================================================ 对话 / 记忆 ===
@router.get("/conversations")
async def list_conversations() -> List[Dict[str, Any]]:
    from .database import crud

    runtime = get_runtime()
    return await crud.list_conversations(runtime.db)


@router.get("/conversations/{character_id}/messages")
async def conversation_messages(character_id: str, limit: int = Query(200, ge=1, le=2000)) -> Dict[str, Any]:
    from .database import crud

    runtime = get_runtime()
    character = await crud.get_character(runtime.db, character_id)
    rows = await crud.recent_messages(runtime.db, character_id, limit=limit)
    return {
        "character_id": character_id,
        "character_name": (character or {}).get("name", ""),
        "count": len(rows),
        "messages": rows,
    }


@router.delete("/conversations/{character_id}/messages")
async def clear_conversation(character_id: str) -> Dict[str, Any]:
    from .database import crud

    runtime = get_runtime()
    removed = await crud.clear_messages(runtime.db, character_id)
    runtime.publish({"type": "conversations_changed"})
    return {"ok": True, "removed": removed}


@router.get("/conversations/{character_id}/memories")
async def list_memories(character_id: str) -> List[Dict[str, Any]]:
    from .database import crud

    runtime = get_runtime()
    return await crud.list_memories(runtime.db, character_id)


@router.post("/conversations/{character_id}/memories")
async def add_memory(character_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    from .database import crud

    runtime = get_runtime()
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    memory_id = await crud.add_memory(
        runtime.db, character_id, content, weight=float(payload.get("weight", 1.0) or 1.0)
    )
    return {"ok": True, "id": memory_id}


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: int) -> Dict[str, Any]:
    from .database import crud

    runtime = get_runtime()
    ok = await crud.delete_memory(runtime.db, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


# ================================================================ 配置 ======
@router.get("/config")
async def get_config_api() -> Dict[str, Any]:
    runtime = get_runtime()
    runtime.config.reload_if_changed()
    return {
        "path": str(runtime.config.path),
        "data_dir": str(data_dir()),
        "config": to_plain(runtime.config.data),
        "defaults": deep_merge({}, DEFAULTS),
    }


@router.put("/config")
async def update_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    runtime = get_runtime()
    patch = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="请求体必须是非空对象")
    runtime.config.patch(patch, persist=True)
    runtime.config.load(force=True)
    runtime.apply_config()
    runtime.scheduler.reschedule()
    runtime.publish({"type": "config_reloaded"})
    log.info("配置已更新：%s", ", ".join(sorted(patch.keys())))
    return {"ok": True, "config": to_plain(runtime.config.data)}


@router.post("/config/reload")
async def reload_config() -> Dict[str, Any]:
    runtime = get_runtime()
    runtime.config.load(force=True)
    runtime.apply_config()
    runtime.scheduler.reschedule()
    runtime.publish({"type": "config_reloaded"})
    return {"ok": True, "config": to_plain(runtime.config.data)}


# ============================================================ 主动消息 ======
@router.get("/proactive/status")
async def proactive_status() -> Dict[str, Any]:
    runtime = get_runtime()
    return runtime.scheduler.status()


@router.post("/proactive/trigger")
async def proactive_trigger(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    runtime = get_runtime()
    character_id = payload.get("character_id") or None
    bot_id = payload.get("bot_id") or None
    force = bool(payload.get("force", True))
    result = await runtime.scheduler.run(
        "manual",
        force=force,
        character_id=str(character_id) if character_id else None,
        bot_id=str(bot_id) if bot_id else None,
    )
    if not result.get("ok") and not result.get("skipped"):
        raise HTTPException(status_code=502, detail=result.get("reason") or "发送失败")
    return result


@router.get("/proactive/logs")
async def proactive_logs(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
    from .database import crud

    runtime = get_runtime()
    return await crud.recent_proactive_logs(runtime.db, limit=limit)


# ================================================================ 异常兜底 ==
async def unhandled_exception_handler(request: Request, exc: Exception) -> Any:  # pragma: no cover
    log.exception("API 处理异常 %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "内部错误: %s" % exc})


# ============================================================= WebSocket ====
def register_websocket(app: Any) -> None:
    async def _events(websocket: WebSocket) -> None:
        runtime = _runtime
        if runtime is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        queue = runtime.subscribe()
        try:
            await websocket.send_json(
                {"type": "hello", "at": iso_now(), "status": await runtime.snapshot()}
            )
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover
            log.debug("WebSocket 连接结束: %s", exc)
        finally:
            runtime.unsubscribe(queue)

    for path in ("/ws/events", "/api/ws/events"):
        try:
            app.add_api_websocket_route(path, _events, name="Tavern Events %s" % path)
        except Exception as exc:  # pragma: no cover
            log.warning("注册 WebSocket %s 失败: %s", path, exc)


__all__ = [
    "get_runtime",
    "register_websocket",
    "router",
    "set_runtime",
    "set_shutdown_handler",
    "truncate",
]
