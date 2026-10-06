"""常用 LLM 服务商预设（OpenAI 兼容端点）。

作用：
* 让用户在配置引导 / 系统设置里**一键选择服务商**，自动填好 Base URL；
* 在选择后给出一组常见模型名作为候选（真正的模型列表用「获取模型列表」从上游拉取）；
* 提供申请 API Key 的入口链接。

模型名会随服务商更新而变化，所以预设里的模型名只是「候选/兜底」，
界面上的模型输入框是可编辑下拉框：可以获取后选择，也可以自己填。

顺序：国内直连 → 国际服务 → 聚合平台 → 自建网关 → 本地部署。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

CUSTOM_LABEL = "自定义 / 其他"


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    base_url: str
    models: Tuple[str, ...] = ()
    docs: str = ""
    note: str = ""
    local: bool = False  # 本地部署：不需要 API Key
    engine: str = ""  # 槽位预设专用：指定引擎（如 gemini-native），LLM 预设留空


PRESETS: Tuple[Provider, ...] = (
    # ============================================================ 国内直连
    Provider(
        key="deepseek",
        name="DeepSeek 官方",
        base_url="https://api.deepseek.com/v1",
        models=("deepseek-chat", "deepseek-reasoner"),
        docs="https://platform.deepseek.com/api_keys",
        note="性价比高，中文自然，推荐先用它",
    ),
    Provider(
        key="dashscope",
        name="阿里云百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen3-max", "qwen-plus", "qwen-turbo"),
        docs="https://bailian.console.aliyun.com/",
    ),
    Provider(
        key="ark",
        name="火山方舟（豆包）",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        models=(),
        docs="https://console.volcengine.com/ark",
        note="模型名需填「接入点 ID」（ep- 开头）",
    ),
    Provider(
        key="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-4.5", "glm-4-plus", "glm-4-flash"),
        docs="https://bigmodel.cn/usercenter/apikeys",
    ),
    Provider(
        key="moonshot",
        name="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
        docs="https://platform.moonshot.cn/console/api-keys",
    ),
    Provider(
        key="siliconflow",
        name="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        models=("deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B", "Qwen/Qwen2.5-7B-Instruct"),
        docs="https://cloud.siliconflow.cn/account/ak",
        note="一个 Key 可用多家开源模型",
    ),
    Provider(
        key="minimax",
        name="MiniMax",
        base_url="https://api.minimax.chat/v1",
        models=("abab6.5s-chat",),
        docs="https://platform.minimaxi.com/user-center/basic-information/interface-key",
    ),
    Provider(
        key="stepfun",
        name="阶跃星辰 StepFun",
        base_url="https://api.stepfun.com/v1",
        models=("step-2-16k", "step-1-8k"),
        docs="https://platform.stepfun.com/interface-key",
    ),
    Provider(
        key="lingyiwanwu",
        name="零一万物 Yi",
        base_url="https://api.lingyiwanwu.com/v1",
        models=("yi-lightning", "yi-large"),
        docs="https://api.lingyiwanwu.com/apikeys",
    ),
    Provider(
        key="baichuan",
        name="百川智能",
        base_url="https://api.baichuan-ai.com/v1",
        models=("Baichuan4", "Baichuan3-Turbo"),
        docs="https://platform.baichuan-ai.com/console/apikey",
    ),
    Provider(
        key="qianfan",
        name="百度千帆（文心）",
        base_url="https://qianfan.baidubce.com/v2",
        models=("ernie-4.5-8k-preview", "ernie-4.0-turbo-8k", "ernie-speed-8k"),
        docs="https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
    ),
    Provider(
        key="spark",
        name="讯飞星火",
        base_url="https://spark-api-open.xf-yun.com/v1",
        models=("generalv4", "generalv3.5", "general"),
        docs="https://console.xfyun.cn/",
    ),
    Provider(
        key="hunyuan",
        name="腾讯混元",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        models=("hunyuan-turbo", "hunyuan-standard"),
        docs="https://console.cloud.tencent.com/hunyuan",
    ),
    # ============================================================ 国际服务
    Provider(
        key="openai",
        name="OpenAI 官方",
        base_url="https://api.openai.com/v1",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
        docs="https://platform.openai.com/api-keys",
        note="国内访问通常需要代理",
    ),
    Provider(
        key="anthropic",
        name="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        models=("claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-3-5-haiku-latest"),
        docs="https://console.anthropic.com/settings/keys",
        note="OpenAI 兼容层；国内访问通常需要代理",
    ),
    Provider(
        key="gemini",
        name="Google Gemini（AI Studio）",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        models=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"),
        docs="https://aistudio.google.com/apikey",
        note="Base URL 带 /v1beta/openai 段",
    ),
    Provider(
        key="mistral",
        name="Mistral",
        base_url="https://api.mistral.ai/v1",
        models=("mistral-large-latest", "mistral-small-latest", "codestral-latest"),
        docs="https://console.mistral.ai/api-keys",
    ),
    Provider(
        key="xai",
        name="xAI（Grok）",
        base_url="https://api.x.ai/v1",
        models=("grok-3", "grok-3-mini", "grok-2-latest"),
        docs="https://console.x.ai/",
    ),
    Provider(
        key="groq",
        name="Groq（超快推理）",
        base_url="https://api.groq.com/openai/v1",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "deepseek-r1-distill-llama-70b"),
        docs="https://console.groq.com/keys",
        note="Llama 系模型速度极快",
    ),
    Provider(
        key="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        models=("meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-R1"),
        docs="https://api.together.xyz/settings/api-keys",
    ),
    Provider(
        key="fireworks",
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        models=("accounts/fireworks/models/llama-v3p1-70b-instruct", "accounts/fireworks/models/deepseek-r1"),
        docs="https://fireworks.ai/account/api-keys",
    ),
    Provider(
        key="cerebras",
        name="Cerebras（超快推理）",
        base_url="https://api.cerebras.ai/v1",
        models=("llama-3.3-70b", "llama-3.1-8b"),
        docs="https://cloud.cerebras.ai/api_keys",
    ),
    Provider(
        key="deepinfra",
        name="DeepInfra",
        base_url="https://api.deepinfra.com/v1/openai",
        models=("deepseek-ai/DeepSeek-R1", "meta-llama/Meta-Llama-3.1-70B-Instruct"),
        docs="https://deepinfra.com/dash/api_keys",
    ),
    Provider(
        key="sambanova",
        name="SambaNova",
        base_url="https://api.sambanova.ai/v1",
        models=("Meta-Llama-3.1-405B-Instruct", "Meta-Llama-3.3-70B-Instruct"),
        docs="https://cloud.sambanova.ai/settings",
    ),
    Provider(
        key="cohere",
        name="Cohere",
        base_url="https://api.cohere.ai/v1",
        models=("command-r-plus", "command-r"),
        docs="https://dashboard.cohere.com/api-keys",
    ),
    Provider(
        key="perplexity",
        name="Perplexity（联网问答）",
        base_url="https://api.perplexity.ai",
        models=("sonar", "sonar-pro", "sonar-reasoning"),
        docs="https://www.perplexity.ai/settings/api",
        note="回答自带实时网络检索来源",
    ),
    Provider(
        key="novita",
        name="Novita AI",
        base_url="https://api.novita.ai/v3/openai",
        models=("DeepSeek-V3-0324", "DeepSeek-R1-0528"),
        docs="https://novita.ai/dashboard/key",
        note="开源模型按量计费，价格低",
    ),
    # ============================================================ 聚合平台
    Provider(
        key="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        models=(),
        docs="https://openrouter.ai/keys",
        note="聚合多家模型，需用「获取模型列表」挑模型",
    ),
    Provider(
        key="gate",
        name="Gate.ai",
        base_url="https://api.gate.ai/v1",
        models=("deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet"),
        docs="https://www.gate.ai/",
        note="聚合多家模型（模型名带厂商前缀）",
    ),
    # ============================================================ 自建网关
    Provider(
        key="gateway",
        name="自建网关（OneAPI / NewAPI）",
        base_url="http://127.0.0.1:3001/v1",
        models=(),
        note="把网关地址与 Key 填进来即可",
        local=True,
    ),
    # ============================================================ 本地部署
    Provider(
        key="ollama",
        name="本地 Ollama",
        base_url="http://127.0.0.1:11434/v1",
        models=("qwen2.5:7b", "llama3.1:8b", "deepseek-r1:7b"),
        docs="https://ollama.com/download",
        note="需要先在本机 ollama pull 对应模型",
        local=True,
    ),
    Provider(
        key="lmstudio",
        name="本地 LM Studio",
        base_url="http://127.0.0.1:1234/v1",
        models=(),
        docs="https://lmstudio.ai/",
        note="在 LM Studio 里开启 Local Server 后使用",
        local=True,
    ),
    Provider(
        key="vllm",
        name="本地 vLLM",
        base_url="http://127.0.0.1:8000/v1",
        models=(),
        docs="https://docs.vllm.ai/",
        local=True,
    ),
    Provider(
        key="localai",
        name="本地 LocalAI",
        base_url="http://127.0.0.1:8080/v1",
        models=(),
        docs="https://localai.io/",
        note="用「获取模型列表」查看已下载的模型",
        local=True,
    ),
)


def labels() -> List[str]:
    """下拉框条目：预设服务商 + 「自定义 / 其他」。"""
    return [item.name for item in PRESETS] + [CUSTOM_LABEL]


def by_label(label: str) -> Optional[Provider]:
    text = (label or "").strip()
    for item in PRESETS:
        if item.name == text:
            return item
    return None


def by_base_url(base_url: str) -> Optional[Provider]:
    """根据已保存的 Base URL 反查服务商（用于回显下拉框）。"""
    text = (base_url or "").strip().rstrip("/").lower()
    if not text:
        return None
    for item in PRESETS:
        if item.base_url.rstrip("/").lower() == text:
            return item
    return None


def model_suggestions(base_url: str) -> List[str]:
    provider = by_base_url(base_url)
    return list(provider.models) if provider else []


def local_placeholder_key(base_url: str) -> str:
    """本地服务通常不校验 Key，但 OpenAI SDK 要求非空，这里给个占位值。"""
    provider = by_base_url(base_url)
    return "ollama" if provider and provider.local else ""


__all__ = [
    "CUSTOM_LABEL",
    "PRESETS",
    "Provider",
    "by_base_url",
    "by_label",
    "labels",
    "local_placeholder_key",
    "model_suggestions",
]
