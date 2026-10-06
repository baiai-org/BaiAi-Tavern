"""「模型路由」各能力槽位的服务商预设（OpenAI 兼容端点为主）。

与 :mod:`app.llm_presets`（主模型）同一套交互：选中预设自动填好 Base URL 与
候选模型；真正的模型清单用「获取模型列表」从上游 ``/models`` 拉取。

预设里的模型名只是「候选/兜底」，会随服务商更新而变化。
顺序与主模型预设一致：国内直连 → 国际服务 → 本地部署。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .llm_presets import CUSTOM_LABEL, Provider

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_DOCS = "https://bailian.console.aliyun.com/"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DOCS = "https://aistudio.google.com/apikey"
OPENAI_URL = "https://api.openai.com/v1"
OPENAI_DOCS = "https://platform.openai.com/api-keys"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_DOCS = "https://console.volcengine.com/ark"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_DOCS = "https://bigmodel.cn/usercenter/apikeys"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DOCS = "https://cloud.siliconflow.cn/account/ak"

SLOT_PRESETS: Dict[str, Tuple[Provider, ...]] = {
    # ======================================================= 图像理解（看图）
    "vision": (
        Provider(
            key="dashscope-vl",
            name="阿里云百炼（通义千问 VL）",
            base_url=DASHSCOPE_URL,
            models=("qwen-vl-max", "qwen-vl-plus", "qwen2.5-vl-72b-instruct"),
            docs=DASHSCOPE_DOCS,
            note="国内直连，中文理解好",
        ),
        Provider(
            key="openai-vision",
            name="OpenAI 官方（GPT-4o）",
            base_url=OPENAI_URL,
            models=("gpt-4o", "gpt-4o-mini"),
            docs=OPENAI_DOCS,
            note="国内访问通常需要代理",
        ),
        Provider(
            key="gemini-vision",
            name="Google Gemini（AI Studio）",
            base_url=GEMINI_URL,
            models=("gemini-2.5-flash", "gemini-2.0-flash"),
            docs=GEMINI_DOCS,
            note="用 AI Studio 的 API Key，Base URL 带 /v1beta/openai",
        ),
        Provider(
            key="deepseek-flash-vision",
            name="DeepSeek 官方（deepseek-flash 看图）",
            base_url="https://api.deepseek.com/v1",
            models=("deepseek-flash",),
            docs="https://platform.deepseek.com/api_keys",
            note="官方 API：deepseek-flash 支持图像理解（base64 / 外链 / Files API，"
                 "旧模型名 deepseek-v4-flash-vision-exp 已下线、由 deepseek-flash 承接）",
        ),
        Provider(
            key="deepseek-vl",
            name="DeepSeek-VL2（硅基流动托管）",
            base_url=SILICONFLOW_URL,
            models=("deepseek-ai/DeepSeek-VL2",),
            docs=SILICONFLOW_DOCS,
            note="开源 DeepSeek-VL2 的第三方托管；官方 deepseek-flash 看图见上一条预设",
        ),
        Provider(
            key="ark-vision",
            name="火山方舟（豆包视觉）",
            base_url=ARK_URL,
            models=(),
            docs=ARK_DOCS,
            note="模型名需填「接入点 ID」（ep- 开头），开通豆包视觉模型",
        ),
        Provider(
            key="zhipu-vl",
            name="智谱（GLM-4V）",
            base_url=ZHIPU_URL,
            models=("glm-4v-plus", "glm-4v-flash"),
            docs=ZHIPU_DOCS,
        ),
        Provider(
            key="hunyuan-vision",
            name="腾讯混元（视觉）",
            base_url="https://api.hunyuan.cloud.tencent.com/v1",
            models=("hunyuan-vision",),
            docs="https://console.cloud.tencent.com/hunyuan",
        ),
        Provider(
            key="moonshot-vision",
            name="月之暗面 Kimi（视觉）",
            base_url="https://api.moonshot.cn/v1",
            models=("moonshot-v1-8k-vision-preview",),
            docs="https://platform.moonshot.cn/console/api-keys",
        ),
        Provider(
            key="siliconflow-vl",
            name="硅基流动 SiliconFlow（其他 VL 模型）",
            base_url=SILICONFLOW_URL,
            models=("Qwen/Qwen2.5-VL-72B-Instruct", "Qwen/Qwen2.5-VL-7B-Instruct", "OpenGVLab/InternVL2.5-78B"),
            docs=SILICONFLOW_DOCS,
        ),
        Provider(
            key="minimax-vision",
            name="MiniMax（视觉）",
            base_url="https://api.minimax.chat/v1",
            models=("abab6.5s-chat",),
            docs="https://platform.minimaxi.com/user-center/basic-information/interface-key",
        ),
        Provider(
            key="vllm-vl",
            name="本地 vLLM（自托管 Qwen-VL 等）",
            base_url="http://127.0.0.1:8000/v1",
            models=("qwen2.5-vl-72b-instruct",),
            docs="https://docs.vllm.ai/",
            note="需要先在本机部署多模态模型",
            local=True,
        ),
        Provider(
            key="lmstudio-vl",
            name="本地 LM Studio",
            base_url="http://127.0.0.1:1234/v1",
            models=(),
            docs="https://lmstudio.ai/",
            note="在 LM Studio 里加载一个 VL 模型并开启 Local Server",
            local=True,
        ),
    ),
    # ======================================================= 图像生成（发图给你）
    "image": (
        Provider(
            key="gemini-image-native",
            name="Google Gemini 生图（原生接口，支持全部新模型）",
            base_url=GEMINI_NATIVE_URL,
            models=(
                "gemini-3.1-flash-lite-image",
                "gemini-3.1-flash-image",
                "gemini-3-pro-image",
                "gemini-2.5-flash-image",
            ),
            docs=GEMINI_DOCS,
            note="nano banana 2 系列（3.1 flash / flash-lite / 3 pro）走原生接口；"
                 "Base URL 是 …/v1beta（不带 /openai），Key 用 AI Studio 的 API Key",
            engine="gemini-native",
        ),
        Provider(
            key="gemini-image",
            name="Google Gemini 生图（OpenAI 兼容层）",
            base_url=GEMINI_URL,
            models=("gemini-2.5-flash-image", "gemini-3-pro-image-preview"),
            docs=GEMINI_DOCS,
            note="官方文档点名兼容层只支持这两个模型；新一代 nano banana 2 请用「原生接口」预设",
        ),
        Provider(
            key="dashscope-image",
            name="阿里云百炼（通义万相）",
            base_url=DASHSCOPE_URL,
            models=("wanx2.1-t2i-turbo", "wanx2.1-t2i-plus", "wanx-v1"),
            docs=DASHSCOPE_DOCS,
        ),
        Provider(
            key="openai-image",
            name="OpenAI 官方",
            base_url=OPENAI_URL,
            models=("gpt-image-1", "dall-e-3"),
            docs=OPENAI_DOCS,
            note="国内访问通常需要代理",
        ),
        Provider(
            key="ark-image",
            name="火山方舟（即梦 Seedream）",
            base_url=ARK_URL,
            models=(),
            docs=ARK_DOCS,
            note="模型名需填「接入点 ID」（ep- 开头），开通即梦/Seedream 模型",
        ),
        Provider(
            key="zhipu-image",
            name="智谱 CogView",
            base_url=ZHIPU_URL,
            models=("cogview-4-250613", "cogview-3-plus"),
            docs=ZHIPU_DOCS,
        ),
        Provider(
            key="hunyuan-image",
            name="腾讯混元（图像）",
            base_url="https://api.hunyuan.cloud.tencent.com/v1",
            models=("hunyuan-di-t2i", "hunyuan-t2i"),
            docs="https://console.cloud.tencent.com/hunyuan",
            note="以服务商文档里的模型名为准",
        ),
        Provider(
            key="qianfan-image",
            name="百度千帆（ERNIE-ViLG）",
            base_url="https://qianfan.baidubce.com/v2",
            models=("ernie-vilg-v4-250302", "ernie-vilg-base"),
            docs="https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        ),
        Provider(
            key="siliconflow-image",
            name="硅基流动 SiliconFlow",
            base_url=SILICONFLOW_URL,
            models=("black-forest-labs/FLUX.1-dev", "black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-3-5-large"),
            docs=SILICONFLOW_DOCS,
        ),
        Provider(
            key="novita-image",
            name="Novita AI",
            base_url="https://api.novita.ai/v3/openai",
            models=("FLUX.1-dev", "SDXL-Lightning", "Stable-Diffusion-v15"),
            docs="https://novita.ai/dashboard/key",
            note="开源生图模型按量计费",
        ),
        Provider(
            key="together-image",
            name="Together AI",
            base_url="https://api.together.xyz/v1",
            models=("black-forest-labs/FLUX.1-schnell", "black-forest-labs/FLUX.1-dev"),
            docs="https://api.together.xyz/settings/api-keys",
        ),
        Provider(
            key="fireworks-image",
            name="Fireworks AI",
            base_url="https://api.fireworks.ai/inference/v1",
            models=("accounts/fireworks/models/flux-dev", "accounts/fireworks/models/flux-schnell"),
            docs="https://fireworks.ai/account/api-keys",
        ),
        Provider(
            key="deepinfra-image",
            name="DeepInfra",
            base_url="https://api.deepinfra.com/v1/openai",
            models=("black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-3-5-large"),
            docs="https://deepinfra.com/dash/api_keys",
        ),
        Provider(
            key="local-sd",
            name="本地 Stable Diffusion（ComfyUI 等）",
            base_url="http://127.0.0.1:7861/v1",
            models=(),
            docs="https://docs.comfy.org/",
            note="需要服务提供 OpenAI 兼容的 /images/generations 接口",
            local=True,
        ),
    ),
    # 文字转语音（edge-tts / OpenAI 兼容 / 百炼 DashScope 原生，按「引擎」下拉切换）
    "tts": (
        Provider(
            key="dashscope-tts",
            name="阿里云百炼（Qwen-Audio / Qwen3-TTS / CosyVoice）（推荐）",
            base_url=DASHSCOPE_URL,
            models=(
                "qwen-audio-3.1-tts-flash",
                "qwen3-tts-instruct-flash",
                "qwen3-tts-flash",
                "cosyvoice-v3-flash",
                "cosyvoice-v2",
            ),
            engine="dashscope",
            docs=DASHSCOPE_DOCS,
            note="百炼原生接口（选此预设会自动切引擎）。推荐 qwen-audio-3.1-tts-flash：自动启用"
            "情感与拟声标签 + 整体语气指令双机制（主模型按语境生成），支持语速/音调/音量"
            "官方参数调节；qwen3-tts-instruct-flash 走指令风格。MaaS 工作空间把 Base URL 换成 "
            "https://ws-你的WorkspaceId.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 即可。"
            "音色与模型必须同系列（混用报 411）：qwen3-tts 用 Cherry/Ethan 等，"
            "cosyvoice 用 longanyang 等，qwen-audio 用 yuxiaoyun_v3.1 等。"
            "（「获取模型列表」不含 qwen-audio / cosyvoice 系列，属百炼上游列表行为，不影响使用）",
        ),
        Provider(
            key="openai-tts",
            name="OpenAI 官方",
            base_url=OPENAI_URL,
            models=("gpt-4o-mini-tts", "tts-1", "tts-1-hd"),
            docs=OPENAI_DOCS,
            note="国内访问通常需要代理",
        ),
        Provider(
            key="minimax-tts",
            name="MiniMax（语音合成）",
            base_url="https://api.minimax.chat/v1",
            models=("speech-02-turbo", "speech-02-hd"),
            docs="https://platform.minimaxi.com/user-center/basic-information/interface-key",
            note="音色（voice）需填 MiniMax 文档里的音色 ID",
        ),
        Provider(
            key="elevenlabs-tts",
            name="ElevenLabs（多语种高品质）",
            base_url="https://api.elevenlabs.io/v1",
            models=("eleven_multilingual_v2", "eleven_turbo_v2"),
            docs="https://elevenlabs.io/app/settings/api-keys",
            note="音色（voice）需填 ElevenLabs 的 voice ID；国内访问通常需要代理",
        ),
        Provider(
            key="novita-tts",
            name="Novita AI（XTTS / MiniMax 语音）",
            base_url="https://api.novita.ai/v3/openai",
            models=("XTTSv2",),
            docs="https://novita.ai/dashboard/key",
            note="开源 / 第三方语音模型按量计费",
        ),
        Provider(
            key="fireworks-tts",
            name="Fireworks AI（TTS）",
            base_url="https://api.fireworks.ai/inference/v1",
            models=("accounts/fireworks/models/tts-1",),
            docs="https://fireworks.ai/account/api-keys",
        ),
        Provider(
            key="local-tts",
            name="本地 TTS 服务（自托管）",
            base_url="http://127.0.0.1:8000/v1",
            models=(),
            docs="",
            note="需要服务提供 OpenAI 兼容的 /audio/speech 接口",
            local=True,
        ),
    ),
}


def labels(slot: str) -> List[str]:
    return [item.name for item in SLOT_PRESETS.get(slot, ())] + [CUSTOM_LABEL]


def by_label(slot: str, label: str) -> Optional[Provider]:
    text = (label or "").strip()
    for item in SLOT_PRESETS.get(slot, ()):
        if item.name == text:
            return item
    return None


def by_base_url(slot: str, base_url: str) -> Optional[Provider]:
    """根据已保存的 Base URL 反查预设（用于回显下拉框）。

    同一 Base URL 可能对应多个能力（如 dashscope 同时有视觉 / 生图 / ASR / TTS），
    所以按「槽位 + URL」精确匹配，不会串槽位。
    """
    text = (base_url or "").strip().rstrip("/").lower()
    if not text:
        return None
    for item in SLOT_PRESETS.get(slot, ()):
        if item.base_url.rstrip("/").lower() == text:
            return item
    return None


__all__ = [
    "CUSTOM_LABEL",
    "DASHSCOPE_URL",
    "GEMINI_DOCS",
    "GEMINI_NATIVE_URL",
    "GEMINI_URL",
    "SLOT_PRESETS",
    "by_base_url",
    "by_label",
    "labels",
]
