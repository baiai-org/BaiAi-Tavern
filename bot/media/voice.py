"""TTS（文字转语音）。

引擎：
* ``edge-tts``：本地免费、无需 Key，按 ``voice`` 音色发声，产出 mp3；
  支持 ``rate`` / ``pitch`` / ``volume``（微软 Edge TTS 原生 SSML 参数）。
* ``openai``：``POST /audio/speech``（json），产出 mp3 / wav，支持 ``speed`` 倍率。
* ``dashscope``：阿里云百炼原生接口（**非** OpenAI 兼容协议，走 ``/audio/speech``
  会 404）：按模型名自动路由——
  ``cosyvoice-*`` / ``qwen-audio-*-tts*`` 走 ``/api/v1/services/audio/tts/SpeechSynthesizer``，
  ``qwen3-tts-*`` / ``qwen-tts*`` 走 ``/api/v1/services/aigc/multimodal-generation/generation``。
  返回 JSON（``output.audio.url`` 临时地址或 ``output.audio.data`` Base64），产出 wav。

语音转文字（ASR）直接用 QQ 官方平台随消息事件推送的参考转写
（``MessageAttachment.asr_refer_text``），无需单独配置线路。

所有函数都是 async，返回原始字节或文本，错误抛 :class:`VoiceError`（带可读提示）。
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from common.logging_setup import get_logger
from common.providers import ENGINE_DASHSCOPE, ENGINE_EDGE_TTS, ENGINE_OPENAI, ProviderSpec

log = get_logger("bot.media.voice")

try:
    import edge_tts

    EDGE_TTS_AVAILABLE = True
    EDGE_TTS_IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover
    edge_tts = None
    EDGE_TTS_AVAILABLE = False
    EDGE_TTS_IMPORT_ERROR = str(_exc)


class VoiceError(RuntimeError):
    """TTS 调用失败。"""


def _join_url(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith(suffix):
        return base
    if not base.endswith("/v1") and not base.endswith("/v3") and not base.endswith("/api"):
        pass
    return base + suffix


def _normalize_edge_rate(value: str) -> str:
    """把「+10% / 10 / 10% / -20」之类统一成 edge-tts 要求的 ``+N%`` / ``-N%``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    import re

    match = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if not match:
        return ""
    number = float(match.group(1))
    if number == 0:
        return ""
    return ("+%.0f%%" % number) if number > 0 else ("%.0f%%" % number)


def _normalize_edge_pitch(value: str) -> str:
    """把「+5Hz / 5 / 5Hz」统一成 edge-tts 要求的 ``+NHz`` / ``-NHz``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    import re

    match = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
    if not match:
        return ""
    number = float(match.group(1))
    if number == 0:
        return ""
    return ("+%dHz" % number) if number > 0 else ("%dHz" % number)


#: 试听文案池：每次点「试听」随机取一句，方便多听几句判断音色效果
PREVIEW_TEXTS: Tuple[str, ...] = (
    "今天天气不错，要不要出去走走？",
    "我刚才看到一只橘猫趴在墙上晒太阳，可爱极了。",
    "晚上想吃什么？我想了三四个菜，你挑一个吧。",
    "这部电影我看了两遍，第二遍才发现结尾藏着一个小细节。",
    "别着急，慢慢说，我听着呢。",
    "周末我打算把阳台重新布置一下，再添两盆绿植。",
    "你上次推荐的那家面馆，汤底真的很鲜。",
    "晚安，明天见。记得早点睡，别又熬夜。",
    "我学会了一道新菜，等做成了第一个请你尝尝。",
    "路上慢点，到了给我发个消息。",
    "这件事交给我吧，你放心好了。",
    "哈哈，你讲的我怎么觉得哪里不对劲呢？",
)


def random_preview_text() -> str:
    import random

    return random.choice(PREVIEW_TEXTS)


# 百炼 Qwen3-TTS 常用系统音色（voice 参数值；完整清单见官方「Qwen-TTS 音色列表」，
# 含各地方言音色 Jada/Dylan/Sunny/Peter/Rocky/Kiki 等，可手动输入）
QWEN_TTS_VOICES: Tuple[str, ...] = (
    "Cherry",
    "Serena",
    "Ethan",
    "Chelsie",
    "Momo",
    "Vivian",
    "Moon",
    "Maia",
    "Kai",
    "Nofish",
    "Bella",
    "Jennifer",
    "Ryan",
    "Katerina",
    "Mia",
    "Seren",
    "Nini",
    "Stella",
    "Bunny",
    "Neil",
    "Elias",
    "Arthur",
    "Vincent",
    "Aiden",
    "Eldric Sage",
    "Jada",
    "Dylan",
    "Sunny",
    "Peter",
    "Rocky",
    "Kiki",
)

# 百炼 CosyVoice 常用系统音色（cosyvoice-v3 系列；完整清单见官方「CosyVoice 音色列表」。
# 注意：v2/v3 的音色名与 v1 不同，longxiaochun 只在 cosyvoice-v1 上有效）
COSYVOICE_VOICES: Tuple[str, ...] = (
    "longanyang",
    "longanhuan",
    "longhua_v2",
    "longxiaochun",
    "longxiaoxia_v2",
    "longshange_v3",
    "longanhuan_v3",
    "longjiaxin_v3",
    "longlaotie_v3",
)

# 百炼 Qwen-Audio-TTS 系统音色（qwen-audio-3.1-tts-flash；见官方「Qwen-Audio-TTS 音色列表」。
# 三族音色不能跨模型混用，混用会 400「Engine error [411]」。
# 前 4 个多语种音色支持方言（上海/广东/东北…）与日韩法德等 8 语种）
QWEN_AUDIO_VOICES: Tuple[str, ...] = (
    "longanhuan_v3.1",
    "longanlingxin_v3.1",
    "longanfengyue_v3.1",
    "xunanchuan_v3.1",
    "yuxiaoyun_v3.1",
    "qiaoxiaojiao_v3.1",
    "xiaxiaochen_v3.1",
    "anmingyuan_v3.1",
    "wenhuaiqing_v3.1",
    "anxiaolan_v3.1",
    "xieshurou_v3.1",
    "baiqinglan_v3.1",
    "xuyuyuan_v3.1",
    "anruorou_v3.1",
    "wenhuaizhi_v3.1",
    "xiaoxingzhi_v3.1",
    "guyunshu_v3.1",
    "huozhuoshi_v3.1",
    "yeqinghe_v3.1",
    "yunhuanhuan_v3.1",
    "xuxiaoqiao_v3.1",
    "baianran_v3.1",
    "xuyanchu_v3.1",
    "yezhiqing_v3.1",
    "andi_v3.1",
    "anyuqing_v3.1",
    "Emily_v3.1",
    "Luna_v3.1",
    "Eric_v3.1",
    "Luca_v3.1",
    "Abby_v3.1",
    "Annie_v3.1",
    "Ava_v3.1",
    "longanhuan_v3.6",
)


def dashscope_voices() -> List[Dict[str, str]]:
    """百炼引擎的候选音色（三族按所填模型自行选择：
    qwen3-tts 用 Cherry/Ethan…，cosyvoice 用 longanyang…，qwen-audio 用 yuxiaoyun_v3.1…）。
    """
    items = [
        {"short_name": name, "locale": "zh-CN", "gender": "qwen-tts"} for name in QWEN_TTS_VOICES
    ]
    items += [{"short_name": name, "locale": "zh-CN", "gender": "cosyvoice"} for name in COSYVOICE_VOICES]
    items += [{"short_name": name, "locale": "zh-CN", "gender": "qwen-audio"} for name in QWEN_AUDIO_VOICES]
    return items


class TTS:
    """文字转语音。引擎：edge-tts（本地免费）/ openai（/audio/speech）。

    可调参数（角色 / 全局音色设置里配置，经 ``synthesize`` 传入）：
    * edge-tts：``rate``（语速，如 "+10%" / "-20%"）、``pitch``（音调，如 "+5Hz"）、
      ``volume``（音量，如 "-10%"）——微软 Edge TTS 原生支持这三个 SSML 参数；
    * openai 兼容：``speed``（语速倍率，OpenAI /audio/speech 官方支持 0.25~4.0，
      多数兼容服务商同样接受，不支持的会忽略）；
    * dashscope：Qwen-Audio-TTS 系列（``qwen-audio-*``）支持官方全量参数——
      ``rate`` 语速（0.5~2.0 倍）/ ``pitch`` 音调（0.5~2.0 倍）/ ``volume`` 音量（0~100）/
      ``language_hints`` 目标语言（自动判定）/ ``format`` + ``sample_rate``（显式 wav 24kHz），
      由 GUI 音色调节值自动映射（见 ``_dashscope_qwen_audio_params``）；
      Qwen3-TTS-Instruct 系列额外支持 ``instruction`` 自然语言指令（官方指令控制，
      由主模型按官方格式生成，见 :mod:`bot.media.instruct`），
      合成时自动带 ``language_type``（中文/英文自动判定）。
      三族模型各有自己的音色（不能跨族混用，否则 400「Engine error [411]」）：
      qwen3-tts* 用 Cherry/Ethan 等英文名，cosyvoice* 用 longanyang 等，
      qwen-audio* 用 yuxiaoyun_v3.1 等（见模块内三个 VOICES 常量）。
    """

    def __init__(self, spec: ProviderSpec, timeout: float = 60.0):
        self.spec = spec
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.spec.configured

    async def synthesize(
        self,
        text: str,
        voice: str = "",
        rate: str = "",
        volume: str = "",
        pitch: str = "",
        speed: float = 0.0,
        instruction: str = "",
    ) -> Dict[str, Any]:
        """合成语音，返回 ``{"data": bytes, "format": str, "voice": str}``。

        ``instruction`` 仅百炼引擎的 Qwen3-TTS-Instruct 系列生效（官方
        ``input.instructions`` 指令控制，配合 ``optimize_instructions``；
        指令由主模型按官方格式生成，见 :mod:`bot.media.instruct`）。
        """
        spec = self.spec
        text = (text or "").strip()
        if not text:
            raise VoiceError("要合成的文字为空")
        voice = (voice or spec.voice or "").strip()
        if spec.engine == ENGINE_EDGE_TTS:
            data, fmt = await self._edge_tts(text, voice, rate=rate, volume=volume, pitch=pitch)
        elif spec.engine == ENGINE_OPENAI:
            data, fmt = await self._openai(text, voice, speed=speed)
        elif spec.engine == ENGINE_DASHSCOPE:
            data, fmt = await self._dashscope(
                text, voice, instruction=instruction, rate=rate, volume=volume, pitch=pitch, speed=speed
            )
        else:  # pragma: no cover
            raise VoiceError("未知的 TTS 引擎：%s" % spec.engine)
        if not data:
            raise VoiceError("语音合成没有返回音频数据")
        return {"data": data, "format": fmt, "voice": voice}

    # ---------------------------------------------------------------- 引擎
    async def _edge_tts(
        self,
        text: str,
        voice: str,
        rate: str = "",
        volume: str = "",
        pitch: str = "",
    ) -> "tuple[bytes, str]":
        if not EDGE_TTS_AVAILABLE:
            raise VoiceError("未安装 edge-tts 依赖：%s" % EDGE_TTS_IMPORT_ERROR)
        voice = voice or "zh-CN-XiaoxiaoNeural"
        # edge-tts 是纯异步网络库，放线程池跑避免阻塞事件循环；
        # rate / volume / pitch 是微软 Edge TTS 的原生 SSML 参数（如 "+10%" / "-5Hz"）
        path = Path(__file__).parent / ("_tmp_edge_%d.mp3" % id(self))

        async def _save() -> None:
            kwargs: Dict[str, str] = {}
            if rate:
                kwargs["rate"] = _normalize_edge_rate(rate)
            if volume:
                kwargs["volume"] = _normalize_edge_rate(volume)
            if pitch:
                kwargs["pitch"] = _normalize_edge_pitch(pitch)
            communicate = edge_tts.Communicate(text, voice, **kwargs)
            await communicate.save(str(path))

        try:
            await asyncio.wait_for(_save(), timeout=self.timeout)
            data = path.read_bytes()
        except asyncio.TimeoutError as exc:
            raise VoiceError("edge-tts 合成超时（%d 秒）" % int(self.timeout)) from exc
        except Exception as exc:
            raise VoiceError("edge-tts 合成失败：%s" % exc) from exc
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return data, "mp3"

    async def _openai(self, text: str, voice: str, speed: float = 0.0) -> "tuple[bytes, str]":
        spec = self.spec
        url = _join_url(spec.base_url, "/audio/speech")
        if not url:
            raise VoiceError("文字转语音未填写 Base URL")
        body: Dict[str, Any] = {"model": spec.model or "tts-1", "input": text, "response_format": "mp3"}
        if voice:
            body["voice"] = voice
        # OpenAI /audio/speech 官方支持 speed（0.25~4.0），多数兼容服务商同样接受
        try:
            if speed and 0.25 <= float(speed) <= 4.0:
                body["speed"] = round(float(speed), 2)
        except (TypeError, ValueError):
            pass
        headers = {"Content-Type": "application/json"}
        if spec.api_key:
            headers["Authorization"] = "Bearer %s" % spec.api_key
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise VoiceError("文字转语音请求失败：%s" % exc) from exc
        if response.status_code >= 400:
            raise VoiceError(
                "文字转语音失败（HTTP %d）：%s" % (response.status_code, response.text[:200])
            )
        return response.content, "mp3"

    # ---------------------------------------------------------- 百炼（DashScope）
    @staticmethod
    def _dashscope_root(base_url: str) -> str:
        """把 Base URL 归一成百炼服务根地址（剥掉 OpenAI 兼容路径 / 版本段）。"""
        base = (base_url or "").strip().rstrip("/")
        for suffix in ("/compatible-mode/v1", "/compatible-mode", "/api/v1", "/v1"):
            if base.lower().endswith(suffix):
                base = base[: -len(suffix)]
                break
        return base

    @staticmethod
    def _dashscope_path(model: str) -> str:
        """按模型系列路由到对应端点（官方规定各系列端点不可混用）。"""
        name = (model or "").strip().lower()
        if name.startswith("cosyvoice") or name.startswith("qwen-audio"):
            return "/api/v1/services/audio/tts/SpeechSynthesizer"
        return "/api/v1/services/aigc/multimodal-generation/generation"

    @staticmethod
    def _detect_language(text: str) -> str:
        """按文本主体判定 ``language_type``（官方：指定语种比 Auto 质量更好）。

        拿不准就不给（走 Auto），不瞎猜。
        """
        s = (text or "").strip()
        if not s:
            return ""
        cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf")
        if cjk / len(s) >= 0.3:
            return "Chinese"
        letters = sum(1 for ch in s if ch.isascii() and ch.isalpha())
        if letters / len(s) >= 0.6:
            return "English"
        return ""

    @staticmethod
    def _dashscope_qwen_audio_params(
        rate: str, volume: str, pitch: str, speed: float
    ) -> Dict[str, Any]:
        """把 GUI 的音色调节值映射成 Qwen-Audio-TTS 官方参数（HTTP API 参考）：

        * ``rate``   语速 0.5~2.0（默认 1.0）——优先取 OpenAI 式语速倍率，
          否则把 edge-tts 式「+10%」换算成倍率；
        * ``pitch``  音调 0.5~2.0（默认 1.0）——edge-tts 式「+5Hz」按 1+Hz/200 近似换算；
        * ``volume`` 音量 0~100（默认 50）——edge-tts 式百分比相对 50 换算。
        """
        params: Dict[str, Any] = {}
        mult: Optional[float] = None
        try:
            if speed and 0.25 <= float(speed) <= 4.0:
                mult = float(speed)
        except (TypeError, ValueError):
            pass
        if mult is None and rate:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)", str(rate))
            if match:
                mult = 1.0 + float(match.group(1)) / 100.0
        if mult is not None:
            params["rate"] = round(min(2.0, max(0.5, mult)), 2)
        if pitch:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)", str(pitch))
            if match:
                params["pitch"] = round(min(2.0, max(0.5, 1.0 + float(match.group(1)) / 200.0)), 2)
        if volume:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)", str(volume))
            if match:
                params["volume"] = int(min(100, max(0, 50 + float(match.group(1)))))
        return params

    async def _dashscope(
        self,
        text: str,
        voice: str,
        instruction: str = "",
        rate: str = "",
        volume: str = "",
        pitch: str = "",
        speed: float = 0.0,
    ) -> "tuple[bytes, str]":
        spec = self.spec
        base = self._dashscope_root(spec.base_url)
        if not base:
            raise VoiceError("文字转语音未填写 Base URL（百炼地址，如 https://dashscope.aliyuncs.com）")
        model = (spec.model or "").strip()
        if not model:
            raise VoiceError("文字转语音未填写模型（如 qwen3-tts-flash / cosyvoice-v3-flash）")
        input_body: Dict[str, Any] = {"text": text, "voice": voice or "Cherry"}
        # language_type：官方明确「指定语种能显著提升合成质量」（仅 Qwen-TTS 系有该参数）
        if model.lower().startswith(("qwen3-tts", "qwen-tts")):
            language = self._detect_language(text)
            if language:
                input_body["language_type"] = language
        # Qwen-Audio-TTS：官方 HTTP API 支持的全量可调参数
        # （format / sample_rate / rate / pitch / volume / language_hints / instruction）
        if model.lower().startswith("qwen-audio"):
            input_body["format"] = "wav"
            input_body["sample_rate"] = 24000
            input_body.update(self._dashscope_qwen_audio_params(rate, volume, pitch, speed))
            language = self._detect_language(text)
            if language == "Chinese":
                input_body["language_hints"] = ["zh"]
            elif language == "English":
                input_body["language_hints"] = ["en"]
        # 指令控制：Qwen3-TTS-Instruct 系列用 input.instructions（+optimize_instructions）；
        # Qwen-Audio-TTS 系列参数名是 instruction（单数，任意指令，官方规定）
        if instruction and "instruct" in model.lower():
            input_body["instructions"] = instruction
            input_body["optimize_instructions"] = True
        elif instruction and model.lower().startswith("qwen-audio"):
            input_body["instruction"] = instruction
        headers = {"Content-Type": "application/json"}
        if spec.api_key:
            headers["Authorization"] = "Bearer %s" % spec.api_key
        url = base + self._dashscope_path(model)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json={"model": model, "input": input_body})
        except httpx.HTTPError as exc:
            raise VoiceError("百炼语音合成请求失败：%s" % exc) from exc
        if response.status_code >= 400:
            detail = response.text[:200]
            try:
                payload = response.json()
                code = str(payload.get("code") or "")
                message = str(payload.get("message") or "")
                if code or message:
                    detail = "%s: %s" % (code, message)
            except Exception:
                pass
            # 411 / 418 都是「音色与模型不匹配」，给出口语化的排查提示
            if "411" in detail or "418" in detail:
                detail += "（音色与模型不同系列不能混用：qwen3-tts 用 Cherry/Ethan 等，cosyvoice 用 longanyang 等，qwen-audio 用 yuxiaoyun_v3.1 等）"
            raise VoiceError("百炼语音合成失败（HTTP %d）：%s" % (response.status_code, detail))
        try:
            payload = response.json()
        except Exception as exc:
            raise VoiceError("百炼语音合成返回了非 JSON 内容") from exc
        code = str(payload.get("code") or "")
        if code and code != "200":
            raise VoiceError("百炼语音合成失败：%s: %s" % (code, payload.get("message") or "未知错误"))
        audio = (payload.get("output") or {}).get("audio") or {}
        data_b64 = str(audio.get("data") or "")
        audio_url = str(audio.get("url") or "")
        if data_b64:
            try:
                data = base64.b64decode(data_b64)
            except Exception as exc:
                raise VoiceError("百炼返回的音频 Base64 解析失败") from exc
            if data:
                return data, "wav"
        if audio_url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    audio_response = await client.get(audio_url)
            except httpx.HTTPError as exc:
                raise VoiceError("下载百炼音频文件失败：%s" % exc) from exc
            if audio_response.status_code >= 400:
                raise VoiceError("下载百炼音频文件失败（HTTP %d）" % audio_response.status_code)
            return audio_response.content, "wav"
        raise VoiceError("百炼语音合成没有返回音频（output.audio 为空，请检查模型与音色是否匹配）")

    async def list_voices(self) -> List[Dict[str, str]]:
        """edge-tts 音色列表（中文优先）/ 百炼候选音色（静态清单）。"""
        if self.spec.engine == ENGINE_DASHSCOPE:
            return dashscope_voices()
        if self.spec.engine != ENGINE_EDGE_TTS or not EDGE_TTS_AVAILABLE:
            return []

        async def _list() -> List[Dict[str, str]]:
            raw = await edge_tts.list_voices()
            out = []
            for item in raw or []:
                out.append(
                    {
                        "short_name": item.get("ShortName", ""),
                        "locale": item.get("Locale", ""),
                        "gender": item.get("Gender", ""),
                    }
                )
            return out

        try:
            items = await asyncio.wait_for(_list(), timeout=30)
        except Exception as exc:
            log.warning("获取 edge-tts 音色列表失败：%s", exc)
            return []
        zh = [i for i in items if i["locale"].startswith("zh")]
        other = [i for i in items if not i["locale"].startswith("zh")]
        return zh + other


__all__ = [
    "COSYVOICE_VOICES",
    "EDGE_TTS_AVAILABLE",
    "PREVIEW_TEXTS",
    "QWEN_AUDIO_VOICES",
    "QWEN_TTS_VOICES",
    "TTS",
    "VoiceError",
    "dashscope_voices",
    "random_preview_text",
]
