"""多模态模型槽位（provider slot）配置与解析。

V0.2 引入「按能力分线路」的模型路由：不同能力走不同的 API 端点 / 模型 / 凭据。

* ``chat``   主模型（文字对话）—— **统一在「系统设置 → LLM」段配置**，
  本模块读取时以 ``llm`` 段为准；``providers.chat`` 段仅兼容旧配置
  （llm 段未配置时回退使用）。界面上不再重复显示该槽位。
* ``vision`` 图像理解（看图）——OpenAI 兼容多模态 ``/chat/completions``
* ``image``  图像生成——OpenAI 兼容 ``/images/generations`` 或 Gemini 原生接口
* ``tts``    文字转语音——引擎可选：
    - ``edge-tts``：本地微软 Edge 在线 TTS（免费、无需 API Key，按音色发声）
    - ``openai``：OpenAI 兼容 ``/audio/speech``
    - ``dashscope``：阿里云百炼原生接口（**不是** OpenAI 兼容协议）：
      Qwen3-TTS（``qwen3-tts-flash`` 等，音色 Cherry/Ethan…）与
      CosyVoice（``cosyvoice-v3-flash`` 等，音色 longanyang…）。
      Base URL 填百炼地址（``https://dashscope.aliyuncs.com`` 或
      工作空间 ``https://ws-xxx.cn-beijing.maas.aliyuncs.com``，
      带不带 ``/compatible-mode/v1`` 都可以，程序会自动剥掉）。

语音转文字不占槽位：QQ 官方平台随语音消息事件直接推送参考转写
（``MessageAttachment.asr_refer_text``），零配置可用。

每个槽位相互独立，互不干扰；某个槽位没配置时对应能力自动降级（不报错、给出可读提示）。

所有槽位统一 :class:`ProviderSpec`，引擎由 ``engine`` 字段决定：

* ``openai``    ——OpenAI 兼容端点（DeepSeek / 通义 / Gemini-OpenAI 兼容层 / 本地 vLLM 等）
* ``edge-tts``  ——本地微软 Edge 在线 TTS（仅 tts 槽位，免费、无需 API Key）
* ``gemini-native`` ——Gemini 原生接口（仅 image 槽位，支持全部 Gemini 图像模型）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

SLOT_CHAT = "chat"
SLOT_VISION = "vision"
SLOT_IMAGE = "image"
SLOT_TTS = "tts"

ALL_SLOTS = (SLOT_CHAT, SLOT_VISION, SLOT_IMAGE, SLOT_TTS)

#: 每个槽位的中文名（界面显示用）
SLOT_LABELS: Dict[str, str] = {
    SLOT_CHAT: "主模型（文字对话）",
    SLOT_VISION: "图像理解（看图）",
    SLOT_IMAGE: "图像生成（发图给你）",
    SLOT_TTS: "文字转语音",
}

ENGINE_OPENAI = "openai"
ENGINE_EDGE_TTS = "edge-tts"
#: 阿里云百炼 DashScope 原生接口（仅 tts 槽位）：
#: Qwen3-TTS 走 ``/api/v1/services/aigc/multimodal-generation/generation``，
#: CosyVoice 走 ``/api/v1/services/audio/tts/SpeechSynthesizer``（按模型名自动路由）。
ENGINE_DASHSCOPE = "dashscope"
#: Gemini 原生接口（/models/{model}:generateContent，AI Studio API Key）。
#: OpenAI 兼容层目前只支持个别图像模型（官方文档点名 gemini-2.5-flash-image /
#: gemini-3-pro-image-preview），新一代 Nano Banana 2 / 2 Lite 要走原生接口。
ENGINE_GEMINI = "gemini-native"

#: 各槽位支持的引擎（顺序即界面下拉顺序，首位为缺省推荐）
SLOT_ENGINES: Dict[str, List[str]] = {
    SLOT_CHAT: [ENGINE_OPENAI],
    SLOT_VISION: [ENGINE_OPENAI],
    # gemini-native：支持全部 Gemini 图像模型（含 nano banana 2 系列）
    SLOT_IMAGE: [ENGINE_OPENAI, ENGINE_GEMINI],
    # dashscope 推荐默认（情感标签 + 语气指令 + 语速/音调/音量全量官方参数）
    SLOT_TTS: [ENGINE_DASHSCOPE, ENGINE_EDGE_TTS, ENGINE_OPENAI],
}

#: 槽位缺省引擎
SLOT_DEFAULT_ENGINE: Dict[str, str] = {
    SLOT_CHAT: ENGINE_OPENAI,
    SLOT_VISION: ENGINE_OPENAI,
    SLOT_IMAGE: ENGINE_OPENAI,
    SLOT_TTS: ENGINE_DASHSCOPE,
}


def is_local_endpoint(url: str) -> bool:
    """本地 / 局域网地址：回环（127.0.0.1 / localhost / ::1）、私网段
    （10.x / 192.168.x / 172.16-31.x）、``.local`` / ``.lan`` 域名。
    这类地址不强制要求 API Key（自部署的 vLLM / SGLang 等通常无需鉴权）。

    模块级函数供各客户端（LLM / 图像等）复用，保证"测试线路提示"与
    "实际回复链路"的判定完全一致。
    """
    text = (url or "").strip().lower()
    if not text:
        return False
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(text).hostname or "").lower()
    except Exception:
        host = text.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if not host:
        return False
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    if host.endswith((".local", ".lan")):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        first, second = int(parts[0]), int(parts[1])
        return first == 10 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31)
    return False


@dataclass
class ProviderSpec:
    """一个模型槽位的完整配置。"""

    slot: str
    engine: str = ENGINE_OPENAI
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    voice: str = ""          # 仅 tts 槽位（音色）
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- 判定
    @property
    def is_local(self) -> bool:
        return is_local_endpoint(self.base_url)

    @property
    def configured(self) -> bool:
        """该槽位是否已配置到可调用。"""
        # 本地引擎：edge-tts（在线免费、无需 Key）
        if self.engine == ENGINE_EDGE_TTS:
            return True
        if not (self.base_url or "").strip():
            return False
        # OpenAI 兼容端点：本地服务不强制 Key，远程必须有 Key
        if self.is_local:
            return True
        return bool((self.api_key or "").strip())

    def missing_fields(self) -> List[str]:
        """未配置时缺哪些字段（界面提示用，精确到字段名）。"""
        if self.engine == ENGINE_EDGE_TTS:
            return []
        missing: List[str] = []
        if not (self.base_url or "").strip():
            missing.append("Base URL")
        if not (self.model or "").strip():
            missing.append("模型名")
        if missing:
            return missing  # 地址 / 模型都没填时不叠加 Key，避免提示过长
        if not self.is_local and not (self.api_key or "").strip():
            missing.append("API Key")
        return missing

    def describe(self) -> Dict[str, Any]:
        """供界面 / 状态展示（不泄露完整 Key）。"""
        key = (self.api_key or "").strip()
        masked = (key[:4] + "…%s" % key[-4:]) if len(key) > 8 else ("已填" if key else "未填")
        return {
            "slot": self.slot,
            "label": SLOT_LABELS.get(self.slot, self.slot),
            "engine": self.engine,
            "base_url": self.base_url or "",
            "model": self.model or "",
            "voice": self.voice or "",
            "key": masked,
            "configured": self.configured,
            "local": self.is_local,
        }


def _node(config: Any, slot: str) -> Mapping[str, Any]:
    raw = config.get("providers.%s" % slot) if config is not None else None
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, dict):
        return raw
    return {}


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def load_slot(config: Any, slot: str) -> ProviderSpec:
    """从配置读取某个槽位，缺失字段给合理默认。"""
    if slot not in ALL_SLOTS:
        raise ValueError("未知槽位：%s" % slot)
    node = _node(config, slot)
    engine = _as_str(node.get("engine")) or SLOT_DEFAULT_ENGINE[slot]
    if engine not in SLOT_ENGINES.get(slot, [ENGINE_OPENAI]):
        engine = SLOT_DEFAULT_ENGINE[slot]

    base_url = _as_str(node.get("base_url"))
    api_key = _as_str(node.get("api_key"))
    model = _as_str(node.get("model"))
    voice = _as_str(node.get("voice"))
    extra: Dict[str, Any] = {}
    # tts 槽位的风格参数（GUI「音色调节」写入；其他槽位忽略这几个键）
    if slot == SLOT_TTS:
        for key in ("rate", "volume", "pitch", "speed"):
            value = _as_str(node.get(key))
            if value:
                extra[key] = value

    spec = ProviderSpec(
        slot=slot,
        engine=engine,
        base_url=base_url,
        api_key=api_key,
        model=model,
        voice=voice,
        extra=extra,
    )

    # 主模型：统一以「系统设置 → LLM」段（llm.*）为准，避免与模型路由页重复配置。
    # 仅当 llm 段未配置时，才回退到 providers.chat 段（兼容早期在路由页填过主模型的老配置）。
    if slot == SLOT_CHAT and config is not None:
        legacy_base = _as_str(config.get("llm.base_url"))
        legacy_key = _as_str(config.get("llm.api_key"))
        legacy_model = _as_str(config.get("llm.model"))
        if legacy_base:
            spec.base_url = legacy_base
            if legacy_key:
                spec.api_key = legacy_key
            if legacy_model:
                spec.model = legacy_model
        elif not spec.configured and (legacy_key or legacy_model):
            # llm 段只有 Key / 模型没有地址：不硬套，保持 providers.chat 原值
            pass

    # tts 用 edge-tts 时若无音色，给一个默认中文音色
    if slot == SLOT_TTS and engine == ENGINE_EDGE_TTS and not voice:
        spec.voice = "zh-CN-XiaoxiaoNeural"
    # tts 用百炼时若无音色，给 qwen-audio 的默认音色（voice 参数必填）
    if slot == SLOT_TTS and engine == ENGINE_DASHSCOPE and not voice:
        spec.voice = "yuxiaoyun_v3.1"
    return spec


def load_all_slots(config: Any) -> Dict[str, ProviderSpec]:
    return {slot: load_slot(config, slot) for slot in ALL_SLOTS}


def configured_slots(config: Any) -> Dict[str, bool]:
    return {slot: load_slot(config, slot).configured for slot in ALL_SLOTS}


__all__ = [
    "ALL_SLOTS",
    "ENGINE_DASHSCOPE",
    "ENGINE_EDGE_TTS",
    "ENGINE_GEMINI",
    "ENGINE_OPENAI",
    "ProviderSpec",
    "SLOT_CHAT",
    "SLOT_DEFAULT_ENGINE",
    "SLOT_ENGINES",
    "SLOT_IMAGE",
    "SLOT_LABELS",
    "SLOT_TTS",
    "SLOT_VISION",
    "configured_slots",
    "load_all_slots",
    "load_slot",
]
