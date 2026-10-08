"""MediaHub：多媒体收发枢纽（V0.2）。

职责（全部走「模型路由」配置里的槽位）：

入站
* `inbound_media`   解析官方事件里的 `attachments`，下载图片；
                       语音直接用 QQ 官方平台随消息推送的参考转写（`asr_refer_text`，零配置、不下载音频）。

出站
* ``compose``         处理角色回复：解析 ``[IMG]`` 绘图标记（image 槽位生成图）、
                      按 ``media.voice_reply_probability`` 概率把回复改成语音
                      （tts 槽位合成，音色取角色的 ``tts_voice``，缺省用全局）。
* ``send_outgoing``   通过官方平台富媒体接口发送：文字（msg_type 0/2）+
                      图片 / 语音（上传 → msg_type=7），被动回复自动带
                      msg_id / 递增 msg_seq。

降级原则：任何槽位没配置或调用失败，都退回纯文字并把原因写进日志，
不让多媒体能力拖垮基础聊天。
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from common.logging_setup import get_logger
from common.providers import (
    ALL_SLOTS,
    SLOT_IMAGE,
    SLOT_TTS,
    SLOT_VISION,
    load_slot,
)

from . import store
from .images import (
    ImageError,
    ImageGenerator,
    _LIGHTING_CLAUSE,
    character_reference_clause,
    fuse_image_prompt,
    resolve_style_text,
)
from .instruct import (
    generate_qwen_audio_tags,
    generate_tts_instruction,
    is_qwen_audio_tts_model,
    is_tts_instruct_model,
    local_qwen_audio_tags,
)
from .voice import TTS, VoiceError

log = get_logger("bot.media.hub")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
AUDIO_EXTS = {".silk", ".mp3", ".wav", ".ogg", ".m4a", ".amr"}

_DOWNLOAD_TIMEOUT = 60.0


# ---------------------------------------------------------------- 数据结构
@dataclass
class MediaInbound:
    """一条入站消息里解析出的多媒体内容。"""

    image_paths: List[str] = field(default_factory=list)
    voice_texts: List[str] = field(default_factory=list)   # 语音转写结果
    voice_paths: List[str] = field(default_factory=list)   # 原始 silk 文件
    file_names: List[str] = field(default_factory=list)    # 其他文件（仅记录名）
    errors: List[str] = field(default_factory=list)

    @property
    def has_image(self) -> bool:
        return bool(self.image_paths)

    @property
    def has_voice(self) -> bool:
        return bool(self.voice_texts) or bool(self.voice_paths)

    def text_note(self) -> str:
        """合并后的文字（原有文字 + 语音转写），给 LLM 用。"""
        parts: List[str] = []
        for text in self.voice_texts:
            if text.strip():
                parts.append(text.strip())
        if self.file_names:
            parts.append("（还收到了文件：%s）" % "、".join(self.file_names[:3]))
        return " ".join(parts)


@dataclass
class OutgoingReply:
    """一条出站回复的完整形态：文字（或语音）+ 可选图片。"""

    text: str = ""                    # 完整文字（日志 / 事件推送用，含 [IMG] 标记）
    body: str = ""                    # 真正发出的文字（已剥掉 [IMG] 标记）
    send_text: bool = True            # False 表示这条回复以语音形式发出
    image_path: str = ""
    image_ext: str = "png"
    voice_paths: List[str] = field(default_factory=list)
    voice_ext: str = "mp3"
    note: str = ""                    # 降级 / 失败说明（日志用）

    @property
    def has_media(self) -> bool:
        return bool(self.image_path) or bool(self.voice_paths)


# ---------------------------------------------------------------- 枢纽
class MediaHub:
    def __init__(self, runtime: Any):
        self.rt = runtime

    # ------------------------------------------------------------ 配置读取
    def _config(self) -> Any:
        return self.rt.config

    def _media_cfg(self, key: str, default: Any = None, section: Any = None) -> Any:
        """读富媒体配置。

        ``section`` 是**某个机器人**的生效 media 段（全局 + 该机器人覆盖，
        见 ``BotAccount.effective_media``）；不传则读全局配置。
        """
        if section is not None:
            value = (section or {}).get(key)
            return default if value is None else value
        return self._config().get("media.%s" % key, default)

    def enabled(self) -> bool:
        if not bool(self._media_cfg("enabled", True)):
            return False
        return any(
            load_slot(self._config(), slot).configured
            for slot in (SLOT_VISION, SLOT_IMAGE, SLOT_TTS)
        )

    def _spec(self, slot: str):
        return load_slot(self._config(), slot)

    # ------------------------------------------------------------ 入站
    async def inbound_media(self, incoming: Any, section: Any = None) -> MediaInbound:
        """解析入站消息里的附件（图片 / 语音），下载并转写。"""
        result = MediaInbound()
        raw = getattr(incoming, "raw", None) or {}
        attachments = raw.get("attachments") if isinstance(raw, dict) else None
        if not isinstance(attachments, list) or not attachments:
            return result
        if not bool(self._media_cfg("enabled", True, section)):
            return result

        vision_on = self._spec(SLOT_VISION).configured

        for index, att in enumerate(attachments[:4]):
            if not isinstance(att, dict):
                continue
            url = str(att.get("url") or "").strip()
            filename = str(att.get("filename") or "attachment").strip() or "attachment"
            content_type = str(att.get("content_type") or "").strip().lower()
            ext = Path(filename).suffix.lower()
            kind = self._classify(ext, content_type)
            if kind == "none":
                continue
            if kind == "audio":
                # 语音直接用 QQ 平台随事件推送的参考转写（零配置可听）
                await self._process_voice_attachment(result, att)
                continue
            if not url:
                result.errors.append("附件 %s 缺少下载链接" % filename)
                continue
            try:
                data = await self._download(url)
            except Exception as exc:
                result.errors.append("附件 %s 下载失败：%s" % (filename, exc))
                continue
            if not data:
                result.errors.append("附件 %s 下载内容为空" % filename)
                continue

            if kind == "image":
                path = store.save_inbox(data, ext or "png")
                result.image_paths.append(str(path))
                if not vision_on:
                    result.errors.append("收到图片但未配置图像理解，角色暂时看不见")
            else:
                result.file_names.append(filename)

        if result.errors:
            log.warning("入站多媒体处理备注：%s", "；".join(result.errors))
        return result

    async def _process_voice_attachment(self, result: "MediaInbound", att: Dict[str, Any]) -> None:
        """语音附件：QQ 平台参考转写（零配置可听）+ 下载音频落盘（V0.2.2）。

        官方平台（bot.q.qq.com）的语音消息附件自带 ``asr_refer_text``
        （腾讯识别引擎的转写文本），随事件一起推送、免费、无需任何配置。
        V0.2.2 起同时把音频下载进 inbox（优先 ``voice_wav_url`` 的 wav，
        回退 ``url`` 的 silk），让「对话记录」能保存并回放用户发来的语音。
        下载失败不影响转写使用（只少存档）。
        """
        refer = str(att.get("asr_refer_text") or "").strip()
        if refer:
            result.voice_texts.append(refer)
            log.info("语音转文字成功（QQ 平台参考转写，%d 字）", len(refer))
        else:
            result.errors.append("收到语音，但平台这次没提供参考转写，角色暂时没听清")

        wav_url = str(att.get("voice_wav_url") or "").strip()
        silk_url = str(att.get("url") or "").strip()
        url, preferred_ext = (wav_url, "wav") if wav_url else (silk_url, "silk")
        if not url:
            return
        try:
            data = await self._download(url)
            if data:
                path = store.save_inbox(data, preferred_ext)
                result.voice_paths.append(str(path))
        except Exception as exc:
            result.errors.append("语音音频存档失败（转写不受影响）：%s" % exc)

    @staticmethod
    def _classify(ext: str, content_type: str) -> str:
        if ext in IMAGE_EXTS or "image" in content_type or "picture" in content_type:
            return "image"
        if ext in AUDIO_EXTS or "audio" in content_type or "voice" in content_type or "silk" in content_type:
            return "audio"
        if ext or content_type:
            return "file"
        return "none"

    async def _download(self, url: str) -> bytes:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BaiAi-Tavern/0.2"}
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError("HTTP %d" % response.status_code)
        return response.content

    # ------------------------------------------------------------ 出站：合成
    def parse_image_prompt(self, content: str, section: Any = None) -> "tuple[str, str]":
        """从回复里剥出 ``[IMG] 描述``，返回 ``(剩余文字, 绘图描述或空)``。"""
        if not bool(self._media_cfg("allow_image", True, section)):
            return (content or "").strip(), ""
        match = self._image_marker_pattern(section).search(content or "")
        if not match:
            return (content or "").strip(), ""
        prompt = match.group(1).strip()
        text = (content or "")[: match.start()] + (content or "")[match.end():]
        return text.strip(), prompt

    def _image_marker_pattern(self, section: Any = None) -> re.Pattern:
        marker = str(self._media_cfg("image_marker", "[IMG]", section) or "[IMG]")
        # 模型常把标记写在行尾/句中（不一定单独成行）：匹配到该行行尾即为绘图描述
        return re.compile(r"%s[ \t\r\n]*(.+?)\s*$" % re.escape(marker.strip()), re.M)

    def media_hint(self, section: Any = None) -> str:
        """写进系统提示词的多媒体约定（未配置时返回空）。"""
        parts: List[str] = []
        image_spec = self._spec(SLOT_IMAGE)
        if bool(self._media_cfg("allow_image", True, section)) and image_spec.configured:
            marker = str(self._media_cfg("image_marker", "[IMG]", section) or "[IMG]")
            parts.append(
                "【发图】当你需要给对方看一张图时——对方让你画/发图，或你想用图表达"
                "（风景、表情、你脑补的画面）——就在回复的最后一行写 %s <图里的内容描述>，"
                "系统会自动把它画出来发过去。"
                "描述要**简短**，只写清楚：人物的关键外貌（按人设写，不编造）、"
                "人物在干什么、人物的表情，以及让人看懂动作的最少场景词；"
                "不用描写光线、环境摆件（画面风格、尺寸系统会自动处理，"
                "写得越多生图越抓不住重点）。"
                "如果图里要出现你自己（对方要你的照片/自拍/立绘），"
                "描述里写上自己的名字和关键外貌。"
                "只是普通聊天时不用加，别每条都发图。" % marker
            )
        return "\n".join(parts)

    async def compose(self, character: Dict[str, Any], content: str, section: Any = None) -> OutgoingReply:
        """把角色的文字回复组装成完整出站消息（图 / 语音 / 纯文字）。

        ``section``：该机器人的生效 media 段（不传 = 全局）。
        """
        out = OutgoingReply(text=(content or "").strip())
        out.body = (content or "").strip()
        if not bool(self._media_cfg("enabled", True, section)):
            return out
        content = (content or "").strip()

        # ------------------------------------------------------------ 图片
        text, image_prompt = self.parse_image_prompt(content, section)
        if image_prompt:
            spec = self._spec(SLOT_IMAGE)
            if spec.configured:
                try:
                    # 绘图描述 + 角色设定 + 画面风格（含用户自定义风格）+ 明亮阳光
                    # 的光影基调，全部先经**主模型**汇总理解融合成完整描述，
                    # 再丢给生图模型——不把任何人设 / 风格原文直接拼过去
                    override = str(self._media_cfg("image_style", "anime", section) or "anime").lower()
                    custom_keywords = str(self._media_cfg("image_style_custom", "", section) or "").strip()
                    style_text, style = resolve_style_text(character, override, custom_keywords)
                    try:
                        _chat_llm = self.rt.engine.llm
                    except AttributeError:  # pragma: no cover - 测试用的假 runtime
                        _chat_llm = None
                    fused, fused_ok = await fuse_image_prompt(_chat_llm, image_prompt, character, style_text)
                    if fused_ok:
                        image_prompt = fused
                        log.info("生图描述已由主模型融合（风格：%s / %s）", style or "未指定", character.get("name"))
                    else:
                        # 主模型不可用 / 融合失败 → 机械兜底：风格 + 角色参考（仅短描述）+ 明亮光影
                        if style_text:
                            image_prompt = image_prompt + style_text
                        if len(image_prompt) <= 40:
                            reference = character_reference_clause(character)
                            if reference:
                                image_prompt = image_prompt + reference
                        image_prompt = image_prompt + _LIGHTING_CLAUSE
                        log.info("生图走机械兜底（风格：%s）", style or "未指定")
                    gen = ImageGenerator(spec)
                    data, ext = await gen.generate(image_prompt)
                    out.image_path = str(store.save_outbox(data, ext))
                    out.image_ext = ext
                    log.info("角色 [%s] 触发生图：%s", character.get("name"), image_prompt)
                except ImageError as exc:
                    message = str(exc)
                    log.warning("图像生成失败：%s", message)
                    out.note = message if message.startswith("图像生成失败") else "图像生成失败：%s" % message
                    text = (text + "（想给你画张图，结果没画成，下次一定）").strip()
            else:
                out.note = "回复里有图片标记但图像生成未配置"
                text = (text + "（想给你看张图，但我现在还不会画）").strip()

        # ------------------------------------------------------------ 语音
        if text and self._should_use_voice(section):
            spec = self._spec(SLOT_TTS)
            tts = TTS(spec)
            if tts.available:
                voice = str(character.get("tts_voice") or "").strip()
                style = self._tts_style(character)
                # 百炼两套官方调教机制（按模型门控，主模型按官方格式/标签表生成）：
                # Qwen3-TTS-Instruct 系列 → 自然语言指令；Qwen-Audio-TTS 系列 → 情感/富语言标签
                use_instruct = is_tts_instruct_model(spec.model)
                use_tags = is_qwen_audio_tts_model(spec.model)
                try:
                    _chat_llm = self.rt.engine.llm
                except AttributeError:  # pragma: no cover - 测试用的假 runtime
                    _chat_llm = None
                try:
                    chunks = self._split_voice_text(text, section)
                    paths: List[str] = []
                    voice_format = "mp3"
                    for chunk in chunks:
                        speech_text = chunk
                        instruction = ""
                        if use_instruct:
                            instruction = await generate_tts_instruction(_chat_llm, chunk, character)
                        elif use_tags:
                            # 双机制：官方情感/拟声标签（text）+ 整体语气指令（instruction）；
                            # 主模型失败时用本地兜底，保证每次都带调教参数
                            tag_result = await generate_qwen_audio_tags(_chat_llm, chunk, character)
                            if tag_result.get("text"):
                                speech_text = tag_result["text"]
                                instruction = tag_result.get("instruction") or ""
                            else:
                                speech_text, instruction = local_qwen_audio_tags(chunk)
                            log.info(
                                "语音调教（%s）：%s | 指令：%s",
                                character.get("name"),
                                speech_text[:60],
                                instruction or "（无）",
                            )
                        result = await tts.synthesize(speech_text, voice=voice, instruction=instruction, **style)
                        voice_format = str(result.get("format") or "mp3")
                        paths.append(str(store.save_outbox(result["data"], result["format"])))
                    out.voice_paths = paths
                    out.voice_ext = voice_format
                    out.send_text = False
                    log.info(
                        "角色 [%s] 本次回复改用语音（%d 条，音色 %s%s）",
                        character.get("name"),
                        len(paths),
                        voice or "全局默认",
                        "，指令风格" if use_instruct else ("，情感标签" if use_tags else ""),
                    )
                except VoiceError as exc:
                    log.warning("语音合成失败，回退文字：%s", exc)
                    out.note = "语音合成失败：%s" % exc
                except Exception as exc:  # pragma: no cover
                    log.exception("语音合成异常：%s", exc)
                    out.note = "语音合成异常：%s" % exc
        out.body = text
        return out

    def _tts_style(self, character: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """TTS 风格参数（语速 / 音调 / 音量）。

        全局值来自「模型路由 → 文字转语音」的音色调节；角色在「角色管理 → 音色」
        里单独设置时，角色值覆盖全局（留空则跟随全局）。
        """
        spec = self._spec(SLOT_TTS)
        style: Dict[str, Any] = {}
        character = character or {}
        for key in ("rate", "volume", "pitch"):
            value = str(character.get("tts_%s" % key) or "").strip() or str(spec.extra.get(key) or "").strip()
            if value:
                style[key] = value
        speed = str(character.get("tts_speed") or "").strip() or str(spec.extra.get("speed") or "").strip()
        if speed:
            try:
                style["speed"] = float(speed)
            except ValueError:
                pass
        return style

    def _should_use_voice(self, section: Any = None) -> bool:
        if not self._spec(SLOT_TTS).configured:
            return False
        try:
            probability = float(self._media_cfg("voice_reply_probability", 0.0, section) or 0.0)
        except Exception:
            probability = 0.0
        return probability > 0 and random.random() < min(1.0, max(0.0, probability))

    def _split_voice_text(self, text: str, section: Any = None) -> List[str]:
        limit = int(self._media_cfg("voice_max_chars", 180, section) or 180)
        limit = max(20, limit)
        text = (text or "").strip()
        if len(text) <= limit:
            return [text] if text else []
        chunks: List[str] = []
        rest = text
        while rest:
            if len(rest) <= limit:
                chunks.append(rest)
                break
            window = rest[: limit + 1]
            cut = -1
            for i in range(len(window) - 1, int(limit * 0.4), -1):
                if window[i] in "。！？!?；;…\n，,":
                    cut = i + 1
                    break
            if cut <= 0:
                cut = limit
            chunk = rest[:cut].strip()
            if chunk:
                chunks.append(chunk)
            rest = rest[cut:].strip()
        return chunks

    # ------------------------------------------------------------ 出站：发送
    async def send_outgoing(
        self,
        bot: Any,
        out: OutgoingReply,
        *,
        is_group: bool,
        peer_id: str,
        group_id: str,
        message_id: str = "",
        next_seq: Optional[Callable[[], int]] = None,
    ) -> Dict[str, Any]:
        """发送完整出站消息：文字 → 图片 → 语音（被动回复自动带序号）。"""
        result: Dict[str, Any] = {"ok": True, "error": "", "sent_text": False, "sent_media": 0}
        if next_seq is None:
            next_seq = lambda: 1  # noqa: E731  主动消息：序号恒为 1
        target = group_id if is_group else peer_id
        official = self._official_config(bot)
        sender = bot.messaging.official_client()
        markdown = bool(official.get("markdown", False))
        max_len = int(official.get("reply_segment_max_len", 200) or 200)
        max_segments = int(official.get("max_reply_segments", 3) or 3)

        if not bot.messaging.official_ready():
            return {
                "ok": False,
                "error": "官方机器人网关未连接，无法发送（请检查 AppID/AppSecret，或查看日志）",
                "sent_text": False,
                "sent_media": 0,
            }

        # ---------------------------------------------------------- 文字
        if out.send_text and (out.body or "").strip():
            from ..qq_official.client import segments_for_official

            for segment in segments_for_official(out.body, max_len=max_len, max_segments=max_segments):
                try:
                    if is_group:
                        await sender.send_group(
                            target, segment, msg_id=message_id, msg_seq=next_seq(), markdown=markdown
                        )
                    else:
                        await sender.send_c2c(
                            target, segment, msg_id=message_id, msg_seq=next_seq(), markdown=markdown
                        )
                    result["sent_text"] = True
                    if out.image_path or out.voice_paths:
                        await asyncio.sleep(0.6)
                except Exception as exc:
                    result["ok"] = False
                    result["error"] = str(exc)
                    return result

        # ---------------------------------------------------------- 图片
        if out.image_path and Path(out.image_path).exists():
            if await self._send_file(
                bot, sender, is_group, target, 1, Path(out.image_path), message_id, next_seq
            ):
                result["sent_media"] += 1

        # ---------------------------------------------------------- 语音
        for path in out.voice_paths:
            if Path(path).exists():
                if await self._send_file(
                    bot, sender, is_group, target, 3, Path(path), message_id, next_seq
                ):
                    result["sent_media"] += 1

        # ------------------------------------------ 兜底：媒体全失败时改发文字
        if not result["sent_text"] and not result["sent_media"] and (out.body or "").strip():
            from ..qq_official.client import segments_for_official

            log.warning("富媒体发送未成功，回退为纯文字回复")
            for segment in segments_for_official(out.body, max_len=max_len, max_segments=max_segments):
                try:
                    if is_group:
                        await sender.send_group(
                            target, segment, msg_id=message_id, msg_seq=next_seq(), markdown=markdown
                        )
                    else:
                        await sender.send_c2c(
                            target, segment, msg_id=message_id, msg_seq=next_seq(), markdown=markdown
                        )
                    result["sent_text"] = True
                except Exception as exc:
                    result["ok"] = False
                    result["error"] = str(exc)
        return result

    async def _send_file(
        self,
        bot: Any,
        sender: Any,
        is_group: bool,
        target: str,
        file_type: int,
        path: Path,
        message_id: str,
        next_seq: Callable[[], int],
    ) -> bool:
        try:
            data = path.read_bytes()
            uploaded = await sender.upload_media_b64(is_group, target, file_type, data, path.name)
            file_info = str((uploaded or {}).get("file_info") or "")
            if not file_info:
                log.error("富媒体上传没有返回 file_info（%s）", path.name)
                return False
            await sender.send_media(
                is_group, target, file_info, msg_id=message_id, msg_seq=next_seq()
            )
            log.info(
                "机器人「%s」已发送%s：%s",
                getattr(bot, "name", ""),
                "图片" if file_type == 1 else "语音",
                path.name,
            )
            return True
        except Exception as exc:
            log.error("富媒体发送失败（%s）：%s", path.name, exc)
            self.rt.note_error("富媒体发送失败：%s" % exc)
            return False

    @staticmethod
    def _official_config(bot: Any) -> Dict[str, Any]:
        spec = getattr(bot, "spec", None)
        if spec is not None:
            try:
                return spec.official_config or {}
            except Exception:
                return {}
        return {}

    # ------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        slots = {slot: load_slot(self._config(), slot).describe() for slot in ALL_SLOTS}
        return {
            "enabled": bool(self._media_cfg("enabled", True)),
            "any_configured": self.enabled(),
            "voice_reply_probability": float(
                self._media_cfg("voice_reply_probability", 0.0) or 0.0
            ),
            "allow_image": bool(self._media_cfg("allow_image", True)),
            "image_marker": str(self._media_cfg("image_marker", "[IMG]") or "[IMG]"),
            "slots": slots,
        }


__all__ = ["MediaHub", "MediaInbound", "OutgoingReply"]
