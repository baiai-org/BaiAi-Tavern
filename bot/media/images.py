"""图像生成与图像理解（视觉）。

* 图像生成：
  - ``openai`` 引擎：OpenAI 兼容 ``POST /images/generations``。端点对参数挑剔时会
    **自动降级参数重试**（400 → 去掉 ``size`` → 再去掉 ``response_format``）——Gemini
    的 OpenAI 兼容层就是典型。部分端点只支持 ``/chat/completions`` + ``modalities``
    出图，这里自动降级尝试；也可用槽位配置 ``extra.image_api`` 强制指定。
    注意：Gemini 兼容层**只支持个别图像模型**（官方文档点名
    gemini-2.5-flash-image / gemini-3-pro-image-preview），新模型要用原生引擎。
  - ``gemini-native`` 引擎：Gemini 原生接口 ``POST /models/{model}:generateContent``
    （AI Studio API Key 走 ``?key=``），支持全部 Gemini 图像模型（nano banana 2 系列
    等），返回 ``inlineData`` base64（PNG / JPG 都可能，按 mimeType 定扩展名）。
* 图像理解：OpenAI 兼容多模态 ``/chat/completions``，图片以 base64 data URL
  放进 user 消息的 content 数组。

返回的图片统一是 PNG / JPG 字节（由端点决定），调用方自行落地。
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
from urllib.parse import quote

import httpx

from common.logging_setup import get_logger
from common.providers import ENGINE_GEMINI, ProviderSpec

log = get_logger("bot.media.images")

_DATA_URL_RE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,(.+)$", re.S)


class ImageError(RuntimeError):
    """图像生成 / 理解失败。"""


def _headers(spec: ProviderSpec) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if spec.api_key:
        headers["Authorization"] = "Bearer %s" % spec.api_key
    return headers


def _join_url(base_url: str, suffix: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith(suffix):
        return base
    return base + suffix


def _gemini_model_name(spec: ProviderSpec) -> str:
    """Gemini 的 ``/models`` 列表返回的模型名带 ``models/`` 前缀，请求时要去掉。"""
    model = (spec.model or "").strip()
    is_gemini = spec.engine == ENGINE_GEMINI or "generativelanguage.googleapis.com" in (spec.base_url or "").lower()
    if is_gemini and model.startswith("models/"):
        return model[len("models/"):]
    return model


def _b64_to_bytes(value: str) -> Optional[bytes]:
    value = (value or "").strip()
    if not value:
        return None
    match = _DATA_URL_RE.match(value)
    if match:
        value = match.group(1)
    try:
        return base64.b64decode(value)
    except Exception:
        return None


def _diffusion_extra_body(size: str) -> Dict[str, Any]:
    """vLLM-Omni（Qwen-Image 等）chat 出图所需的 ``extra_body`` 生成参数。

    参数集与官方示例一致；尺寸解析自 ``size``（"1024x1024"），解析不了就不带
    尺寸（用模型默认）。
    """
    extra: Dict[str, Any] = {
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 50,
        "true_cfg_scale": 4.0,
        "seed": 42,
    }
    match = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(size or ""))
    if match:
        extra["width"] = int(match.group(1))
        extra["height"] = int(match.group(2))
    return extra


# ================================================================ 画面风格
# 角色是人设，画出来的图却常常是「真人照片」风格——和二次元角色完全不符。
# 生图前先读一遍角色设定，识别出角色属于二次元还是真实风格，把风格写进
# 绘图提示词；识别不出来就不加（保持模型自己的判断）。
#
# 全局兜底/强制开关：config.yaml 的 ``media.image_style``（可按机器人覆盖）
#   auto（默认）= 按角色自动识别；anime = 强制二次元；
#   realistic = 强制写实；custom = 用 ``media.image_style_custom`` 里
#   用户自己写的风格关键词；off = 不加风格词。

STYLE_ANIME = "anime"
STYLE_REALISTIC = "realistic"
STYLE_CUSTOM = "custom"

_ANIME_KEYWORDS = (
    "二次元", "动漫", "漫画", "日漫", "国漫", "轻小说", "acg", "萌", "萌娘",
    "傲娇", "病娇", "傲娇", "天然呆", "魔法少女", "魔法", "魔力", "魔导",
    "巫女", "术式", "异世界", "转生", "勇者", "魔王", "精灵", "兽耳",
    "猫耳", "兔耳", "狐耳", "龙角", "翅膀", "天使", "恶魔", "神明",
    "剑士", "法师", "圣骑士", "冒险者", "召唤", "契约", "轮回",
    "祭典", "樱花", "神社", "和服", "jk", "洛丽塔", "手办", "粘土人",
    "q版", "立绘", "卡面", "虚拟主播", "vtuber", "虚拟偶像", "偶像",
    "声优", "中二", "大小姐", "学姐", "学妹", "同桌", "放学后",
    "双马尾", "插画", "手绘", "赛璐璐", "像素", "游戏角色", "游戏cg",
    "修仙", "仙侠", "奇幻", "后宫",
)

_REAL_KEYWORDS = (
    "写实", "真实感", "真人", "照片", "摄影", "写真", "摄影作品",
    "胶片", "单反", "微单", "镜头", "人像摄影", "生活照", "纪实",
    "老照片", "黑白照片", "街拍", "都市", "职场", "商务", "西装",
    "通勤", "地铁", "旅拍", "风景照", "景点", "海边", "沙滩",
    "自然光", "逆光", "黄金时刻", "景深", "大光圈", "虚化",
    "85mm", "50mm", "35mm", "全画幅", "iso", "快门", "光圈",
    "曝光", "白平衡", "色温", "胶片感", "ccd", "手机拍照",
    "随手拍", "自拍", "合照", "婚纱照", "婚纱", "证件照",
    "模特", "超模", "走秀", "t台", "时尚", "穿搭", "高定",
    "杂志", "画报", "演员", "明星", "电影剧照", "电视剧",
)

_ANIME_CLAUSE = "，画面风格：二次元动漫风格，日系动漫插画，赛璐璐上色，线条干净"
_REAL_CLAUSE = "，画面风格：写实摄影风格，真实照片质感，自然光影，细节真实"


def detect_character_style(character: Optional[Dict[str, Any]]) -> str:
    """按角色设定识别画面风格：返回 ``anime`` / ``realistic`` / ``""``。

    把角色的名称 / 描述 / 性格 / 场景 / 系统指令拼起来打分：
    哪一类关键词命中多就算哪一类；两边都没命中或打平就返回 ``""``
    （不强加风格，交给模型）。
    """
    if not isinstance(character, dict) or not character:
        return ""
    text = " ".join(
        str(character.get(key) or "")
        for key in ("name", "description", "personality", "scenario", "system_prompt")
    ).lower()
    if not text.strip():
        return ""
    anime_score = sum(text.count(keyword) for keyword in _ANIME_KEYWORDS)
    real_score = sum(text.count(keyword) for keyword in _REAL_KEYWORDS)
    if anime_score > real_score:
        return STYLE_ANIME
    if real_score > anime_score:
        return STYLE_REALISTIC
    return ""


def style_clause(style: str, custom_keywords: str = "") -> str:
    """风格标识 → 追加到绘图描述末尾的风格短语（未知/空标识返回空串）。

    ``custom``：直接用用户自己写的风格关键词；没写关键词时返回空串
    （调用方会回落到自动识别）。
    """
    if style == STYLE_ANIME:
        return _ANIME_CLAUSE
    if style == STYLE_REALISTIC:
        return _REAL_CLAUSE
    if style == STYLE_CUSTOM:
        keywords = (custom_keywords or "").strip()
        if not keywords:
            return ""
        return "，画面风格：%s" % keywords
    return ""


def character_reference_clause(character: Optional[Dict[str, Any]]) -> str:
    """角色参考段：画面里要画**角色本人**时，按角色卡的描述与性格生成人物。

    返回追加到绘图描述末尾的条件句（画面无关该角色时不强行加入）；
    角色卡没有描述/性格时返回空串（没有可参考的信息就不加，避免稀释提示词）。
    """
    if not isinstance(character, dict) or not character:
        return ""
    name = str(character.get("name") or "").strip()
    description = " ".join(str(character.get("description") or "").split())
    personality = " ".join(str(character.get("personality") or "").split())
    if not description and not personality:
        return ""
    description = _truncate(description, 120)
    personality = _truncate(personality, 60)
    who = "「%s」" % name if name else "该角色"
    parts = []
    if description:
        parts.append(description)
    if personality:
        parts.append("性格：%s" % personality)
    return (
        "；角色参考：若画面中需要画出%s这个人物本身，其外貌、气质、穿着须符合——%s；"
        "若画面内容与该角色无关，则不要强行加入。" % (who, "；".join(parts))
    )


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def image_prompt_with_style(
    prompt: str,
    character: Optional[Dict[str, Any]],
    override: str = "auto",
    custom_keywords: str = "",
) -> Tuple[str, str]:
    """给绘图描述加上与角色匹配的风格，返回 ``(最终提示词, 生效的风格标识)``。

    ``override``：``auto`` 按角色自动识别（默认）；``anime`` / ``realistic``
    强制指定；``custom`` 用 ``custom_keywords`` 里用户自己写的风格关键词
    （关键词为空时回落自动识别）；``off`` 不加风格词。
    """
    prompt = (prompt or "").strip()
    style = ""
    if override in (STYLE_ANIME, STYLE_REALISTIC):
        style = override
    elif override == STYLE_CUSTOM:
        if (custom_keywords or "").strip():
            style = STYLE_CUSTOM
        else:  # 选了自定义但没填关键词：回落自动识别
            style = detect_character_style(character)
    elif override == "off":
        style = ""
    else:  # auto / 未知值
        style = detect_character_style(character)
    if style:
        prompt = prompt + style_clause(style, custom_keywords)
    return prompt, style


def _ext_from_mime(url_or_mime: str) -> str:
    """从 data URL / MIME 类型 / 文件后缀里猜图片扩展名。"""
    lower = str(url_or_mime or "").lower()
    if "png" in lower:
        return "png"
    if "jpeg" in lower or "jpg" in lower:
        return "jpg"
    if "webp" in lower:
        return "webp"
    if "gif" in lower:
        return "gif"
    return "png"


def _dashscope_native_url(base_url: str) -> Optional[str]:
    """阿里云百炼（DashScope）宿主时，派生原生同步生图端点。

    百炼的 OpenAI 兼容模式里 ``/images/generations`` 并非所有地域 / 域名都提供
    （旧公共域名上对 Qwen-Image 3.0 系列会 404）；原生协议
    ``{host}/api/v1/services/aigc/multimodal-generation/generation`` 则全量可用
    （开源组件 ArcReel 的 DashScope 图像后端即走此路径）。
    兼容两种 base 写法：``…/compatible-mode/v1`` 与 ``…/api/v1``。
    """
    try:
        from urllib.parse import urlparse

        parsed = urlparse((base_url or "").strip())
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host.endswith("aliyuncs.com") and "/compatible-mode/" not in (parsed.path or ""):
        return None
    path = parsed.path or ""
    for suffix in ("/compatible-mode/v1", "/api/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return "%s://%s%s/api/v1/services/aigc/multimodal-generation/generation" % (
        parsed.scheme or "https",
        parsed.netloc,
        path.rstrip("/"),
    )


def _first_image_from_dict(container: Dict[str, Any]) -> Optional[Tuple[Any, str]]:
    """从单个可能携带图片的对象里提取 ``(数据, 扩展名)``，提取不到返回 ``None``。

    数据为图片字节；是 URL 时为 ``("__url__", url)``，由调用方下载。
    data URL（``data:image/png;base64,...``）就地解码成字节，不走下载。
    """
    if not isinstance(container, dict):
        return None
    b64 = container.get("b64_json") or container.get("base64") or container.get("data")
    if isinstance(b64, str) and b64.strip():
        data = _b64_to_bytes(b64)
        if data:
            return data, "png"
    url_value = None
    image_url = container.get("image_url")
    if isinstance(image_url, dict):
        url_value = image_url.get("url")
    elif isinstance(image_url, str):
        url_value = image_url
    if not url_value:
        # DashScope 原生协议 / 部分兼容层用单数 "image" 键（值为 URL / data URL / b64）
        image_value = container.get("image")
        if isinstance(image_value, dict):
            image_value = image_value.get("url")
        if isinstance(image_value, str):
            url_value = image_value
        else:
            url_value = container.get("url") or container.get("urltest")
    if isinstance(url_value, str) and url_value.strip():
        value = url_value.strip()
        if value.lower().startswith("data:"):
            data = _b64_to_bytes(value)
            if data:
                return data, _ext_from_mime(value)
        return ("__url__", value)
    return None


def _extract_chat_image(payload: Any) -> Optional[Tuple[Any, str]]:
    """从 chat/completions 出图返回里找图片，兼容各家返回形状：

    * ``choices[].message.images[]``（OpenAI 风格：b64_json / image_url / url）
    * ``choices[].message.image`` / ``image_url``（单数键）
    * ``choices[].message.content`` 为 data URL 字符串，或内容数组里的图片部件
    * 顶层 ``data[]``（部分端点在 chat 接口直接返回 images/generations 形状）
    """
    if isinstance(payload, dict):
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for item in message.get("images") or []:
                found = _first_image_from_dict(item)
                if found:
                    return found
            for key in ("image", "image_url"):
                value = message.get(key)
                if isinstance(value, (dict, str)) and value:
                    container = value if isinstance(value, dict) else {"url": value}
                    found = _first_image_from_dict(container)
                    if found:
                        return found
            content = message.get("content")
            if isinstance(content, str):
                data = _b64_to_bytes(content)
                if data:
                    return data, "png"
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") not in ("image_url", "image", "input_image"):
                        continue
                    found = _first_image_from_dict(part)
                    if found:
                        return found
        for item in payload.get("data") or []:
            found = _first_image_from_dict(item)
            if found:
                return found
    return None


def builtin_test_image_data_url() -> str:
    """内置测试图：64x64 纯红 PNG 的 data URL。

    图像理解「测试线路」用它自检——独立于图像生成，端点不需要任何额外素材，
    模型应能答出「红色」。
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 0, 0)).save(buffer, format="PNG")
    return "data:image/png;base64,%s" % base64.b64encode(buffer.getvalue()).decode("ascii")


def _guess_mime(data: bytes, path: "str | Path" = "") -> str:
    """按文件头（magic bytes）识别图片类型，识别不了再按扩展名猜。

    本地存储的媒体文件名**没有点**（如 ``20261005_110802_dee5jpg``），
    ``Path.suffix`` 取不到扩展名，只看后缀会把 JPEG 标成 PNG——部分上游会拒收。
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    import mimetypes

    suffix = Path(str(path)).suffix if str(path) else ""
    if not suffix and str(path):
        # 无点文件名：结尾常带真实扩展名（..._dee5jpg）
        for ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
            if str(path).endswith(ext):
                suffix = "." + ext
                break
    return mimetypes.guess_type("x" + (suffix or ".png"))[0] or "image/png"


def image_data_url(path: "str | None", data: Optional[bytes] = None) -> str:
    """本地图片路径 → base64 data URL（多模态消息用），MIME 按真实内容识别。"""
    if data is None:
        if not path:
            raise ImageError("没有可发送的图片")
        data = Path(path).read_bytes()
    return "data:%s;base64,%s" % (_guess_mime(data, path or ""), base64.b64encode(data).decode("ascii"))


class ImageGenerator:
    """图像生成（/images/generations，自动降级 /chat/completions）。"""

    def __init__(self, spec: ProviderSpec, timeout: float = 120.0):
        self.spec = spec
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.spec.configured

    async def generate(self, prompt: str, size: str = "1024x1024") -> Tuple[bytes, str]:
        """返回 ``(图片字节, 扩展名)``。"""
        spec = self.spec
        if not spec.configured:
            raise ImageError("图像生成尚未配置（模型路由 → 图像生成）")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageError("绘图描述为空")

        forced = str(spec.extra.get("image_api") or "").strip().lower()
        order: List[str] = []
        if forced == "chat":
            order = ["chat"]
        elif forced == "images":
            order = ["images"]
        elif spec.engine == ENGINE_GEMINI:
            # Gemini 原生接口：原生优先，失败后兜底试 chat/modalities
            order = ["gemini", "chat"]
        elif _dashscope_native_url(spec.base_url):
            # 百炼宿主：兼容模式 images 路由缺失时先试原生协议，再降级 chat
            order = ["images", "dashscope", "chat"]
        else:
            order = ["images", "chat"]

        last_error = ""
        for mode in order:
            try:
                if mode == "images":
                    return await self._via_images(prompt, size)
                if mode == "gemini":
                    return await self._via_gemini_native(prompt)
                if mode == "dashscope":
                    return await self._via_dashscope_native(prompt, size)
                return await self._via_chat(prompt, size)
            except ImageError as exc:
                last_error = str(exc)
                log.info("图像生成 %s 方式失败，尝试下一种：%s", mode, last_error)
                continue
        raise ImageError("图像生成失败：%s" % last_error)

    async def _via_images(self, prompt: str, size: str) -> Tuple[bytes, str]:
        spec = self.spec
        url = _join_url(spec.base_url, "/images/generations")
        if not url:
            raise ImageError("图像生成未填写 Base URL")
        # 参数集从「全量」到「精简」逐级降级重试：
        # Gemini（nano banana / gemini-2.5-flash-image）的 OpenAI 兼容层对参数挑剔——
        # 部分版本直接拒绝 ``response_format``（400 UNEXPECTED_PARAMETER），
        # ``size`` 只接受特定取值（512x512 之类会报错），其余未知参数静默忽略。
        # OpenAI 官方端点三档都能吃，不受影响。
        base_body: Dict[str, Any] = {"model": _gemini_model_name(spec), "prompt": prompt}
        variants = [
            {**base_body, "n": 1, "response_format": "b64_json", "size": size} if size else {**base_body, "n": 1, "response_format": "b64_json"},
            {**base_body, "n": 1, "response_format": "b64_json"},
            dict(base_body),
        ]
        # 去掉连续重复的档位
        deduped: List[Dict[str, Any]] = []
        for body in variants:
            if body not in deduped:
                deduped.append(body)

        last_error = ""
        for body in deduped:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, headers=_headers(spec), json=body)
            except httpx.HTTPError as exc:
                raise ImageError("图像生成请求失败：%s" % exc) from exc
            if response.status_code >= 400:
                last_error = (
                    "图像生成失败（HTTP %d）：%s" % (response.status_code, response.text[:200])
                )
                if response.status_code == 400 and len(deduped) > 1:
                    log.info("图像生成端点拒绝了这组参数，降级参数重试：%s", last_error)
                    continue
                raise ImageError(last_error)
            try:
                payload = response.json()
            except Exception:
                raise ImageError("图像生成返回不是 JSON：%s" % response.text[:200])
            items = (payload or {}).get("data") or []
            if not items:
                raise ImageError("图像生成没有返回图片数据")
            item = items[0]
            b64 = item.get("b64_json")
            if b64:
                data = _b64_to_bytes(str(b64))
                if data:
                    return data, "png"
            url_value = item.get("url")
            if url_value:
                data = await self._download(str(url_value))
                if data:
                    return data, self._ext_from_url(str(url_value))
            raise ImageError("图像生成返回里没有可用的图片数据")
        raise ImageError(last_error)

    async def _via_dashscope_native(self, prompt: str, size: str) -> Tuple[bytes, str]:
        """百炼原生同步生图（DashScope 协议）。

        请求体：``{model, input: {messages: [{role, content: [{text}]}]},
        parameters: {size: "宽*高"}}``；响应：``output.choices[0].message.content``
        为部件数组，图片在部件的 ``image`` 键（公网 URL 或 data URL）。
        """
        spec = self.spec
        url = _dashscope_native_url(spec.base_url)
        if not url:
            raise ImageError("图像生成未填写 Base URL（百炼宿主）")
        if not spec.model:
            raise ImageError("图像生成未填写模型名")
        body: Dict[str, Any] = {
            "model": spec.model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        }
        match = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(size or ""))
        if match:
            body["parameters"] = {"size": "%s*%s" % (match.group(1), match.group(2))}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=_headers(spec), json=body)
        except httpx.HTTPError as exc:
            raise ImageError("图像生成请求失败：%s" % exc) from exc
        if response.status_code >= 400:
            raise ImageError("图像生成失败（HTTP %d）：%s" % (response.status_code, response.text[:200]))
        try:
            payload = response.json()
        except Exception:
            raise ImageError("图像生成返回不是 JSON：%s" % response.text[:200])
        inner = (payload or {}).get("output") or {}
        found = _extract_chat_image(inner) or _extract_chat_image(payload)
        if not found:
            raise ImageError(
                "图像生成返回里没有可用的图片数据（响应：%s）"
                % json.dumps(payload, ensure_ascii=False)[:400]
            )
        data_or_url, ext = found
        if data_or_url == "__url__":
            data = await self._download(ext)
            if data:
                return data, self._ext_from_url(ext)
        else:
            return data_or_url, ext
        raise ImageError("图像生成返回的图片链接下载失败")

    async def _via_gemini_native(self, prompt: str) -> Tuple[bytes, str]:
        """Gemini 原生接口：``POST /models/{model}:generateContent``（key 走 query）。

        官方文档的 OpenAI 兼容层只支持个别图像模型；新一代 Nano Banana 2 /
        2 Lite（gemini-3.1-flash-image / -lite-image）等走原生接口：
        ``generationConfig.responseModalities = ["TEXT", "IMAGE"]``，图片在
        ``candidates[0].content.parts[].inlineData``（base64，PNG 或 JPG）。
        """
        spec = self.spec
        base = (spec.base_url or "").strip().rstrip("/")
        if not base:
            raise ImageError("图像生成未填写 Base URL（Gemini 原生应为 …/v1beta）")
        if not spec.model:
            raise ImageError("图像生成未填写模型名")
        url = "%s/models/%s:generateContent" % (base, quote(_gemini_model_name(spec), safe=""))
        if spec.api_key:
            url += "?key=%s" % quote(spec.api_key, safe="")
        body: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        response: Optional[httpx.Response] = None
        # 503 = Google 高负载（官方原话 "Spikes in demand are usually temporary.
        # Please try again later"），短延迟重试一次再交给降级链路
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            except httpx.HTTPError as exc:
                raise ImageError("图像生成请求失败：%s" % exc) from exc
            if response.status_code in (500, 502, 503) and attempt == 1:
                log.info("图像生成上游繁忙（HTTP %d），3 秒后重试一次", response.status_code)
                await asyncio.sleep(3)
                continue
            break
        if response.status_code >= 400:
            raise ImageError(
                "图像生成失败（HTTP %d）：%s" % (response.status_code, response.text[:200])
            )
        try:
            payload = response.json()
        except Exception:
            raise ImageError("图像生成返回不是 JSON：%s" % response.text[:200])
        candidates = payload.get("candidates") or []
        parts: List[Any] = []
        for cand in candidates:
            if isinstance(cand, dict):
                content = cand.get("content") or {}
                parts = content.get("parts") or []
                break
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                data = _b64_to_bytes(str(inline["data"]))
                if data:
                    mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png").lower()
                    return data, ("jpg" if "jpeg" in mime else "png")
        raise ImageError("图像生成返回里没有可用的图片数据")

    async def _via_chat(self, prompt: str, size: str) -> Tuple[bytes, str]:
        """Gemini / OpenRouter / vLLM-Omni 一类只走 chat/completions 出图的端点。

        各兼容层对请求体挑剔程度不一，按顺序降级重试：

        * ``modalities`` 大小写各家不一：Gemini 兼容层现要求小写 ``["text", "image"]``，
          部分旧层 / 其他厂商要大写；
        * ``messages[].content`` 有的层只收**字符串**，有的层（vLLM-Omni /
          DeepSeek 风格的 Pydantic 校验）要求**多模态内容数组**
          ``[{"type": "text", "text": ...}]``（400 报
          "Input should be a valid list: messages[0].content"）——
          字符串是 OpenAI 官方格式，先试字符串，被报 content 格式错误时换数组；
        * vLLM-Omni（Qwen-Image 等扩散模型的 OpenAI 兼容服务）：200 但响应里
          没有图时，按官方示例补 ``extra_body`` 生成参数（``num_outputs_per_prompt``
          / ``height`` / ``width`` / ``num_inference_steps`` / ``true_cfg_scale`` /
          ``seed``）再试一次——不带这些参数时它会正常跑完推理却在响应里丢图。
        """
        spec = self.spec
        url = _join_url(spec.base_url, "/chat/completions")
        if not url:
            raise ImageError("图像生成未填写 Base URL")
        variants = [
            (prompt, modalities)
            for modalities in (["text", "image"], ["TEXT", "IMAGE"])
        ] + [
            ([{"type": "text", "text": prompt}], modalities)
            for modalities in (["text", "image"], ["TEXT", "IMAGE"])
        ]
        response = None
        ok_body: Optional[Dict[str, Any]] = None
        last_error = ""
        content_list_required = False
        for content, modalities in variants:
            if content_list_required and isinstance(content, str):
                continue  # 端点已明确要数组，其余字符串档不用试了
            body: Dict[str, Any] = {
                "model": _gemini_model_name(spec),
                "messages": [{"role": "user", "content": content}],
                "modalities": modalities,
            }
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, headers=_headers(spec), json=body)
            except httpx.HTTPError as exc:
                raise ImageError("图像生成请求失败：%s" % exc) from exc
            if response.status_code < 400:
                ok_body = body
                break
            last_error = (
                "图像生成失败（HTTP %d）：%s" % (response.status_code, response.text[:200])
            )
            low = response.text.lower()
            if response.status_code != 400:
                raise ImageError(last_error)
            if "modality" in low:
                log.info("chat 出图 modalities 取值不被接受，换下一组重试：%s", last_error)
                continue
            if "content" in low and ("list" in low or "array" in low):
                content_list_required = True
                log.info("chat 出图端点要求 content 为内容数组，换数组格式重试：%s", last_error)
                continue
            if "content" in low and "string" in low:
                # 端点要求字符串——字符串档已经先试过，数组档再试也没有意义
                raise ImageError(last_error)
            raise ImageError(last_error)
        if response is None or response.status_code >= 400:
            raise ImageError(last_error or "图像生成失败")
        try:
            payload = response.json()
        except Exception:
            raise ImageError("图像生成返回不是 JSON：%s" % response.text[:200])
        found = _extract_chat_image(payload)
        if found is None and ok_body is not None:
            # 200 但响应里没有图：vLLM-Omni 一类扩散服务需要 extra_body 生成参数，
            # 用上一次成功的请求体补上参数重试一次（官方示例的参数集）。
            retry_body = dict(ok_body)
            retry_body["extra_body"] = _diffusion_extra_body(size)
            log.info("chat 出图 200 但响应无图，按 vLLM-Omni 官方示例补 extra_body 重试")
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, headers=_headers(spec), json=retry_body)
            except httpx.HTTPError as exc:
                raise ImageError("图像生成请求失败：%s" % exc) from exc
            if response.status_code < 400:
                try:
                    payload = response.json()
                except Exception:
                    payload = {"raw": response.text[:200]}
                found = _extract_chat_image(payload)
            else:
                log.info("补 extra_body 重试仍被拒（HTTP %d），按原响应报错", response.status_code)
        if found:
            data_or_url, ext = found
            if data_or_url == "__url__":
                data = await self._download(ext)
                if data:
                    return data, self._ext_from_url(ext)
            else:
                return data_or_url, ext
        # 各家返回形状都没命中：把响应体带上，方便对照端点实际返回修解析
        raise ImageError(
            "图像生成返回里没有可用的图片数据（响应：%s）"
            % json.dumps(payload, ensure_ascii=False)[:400]
        )

    async def _download(self, url: str) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url)
            if response.status_code >= 400:
                return None
            return response.content
        except Exception:
            return None

    @staticmethod
    def _ext_from_url(url: str) -> str:
        lower = url.lower().split("?")[0]
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            if lower.endswith(ext):
                return ext.lstrip(".")
        return "png"


class Vision:
    """图像理解：多模态 chat/completions。"""

    def __init__(self, spec: ProviderSpec, timeout: float = 90.0):
        self.spec = spec
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.spec.configured

    async def describe(
        self,
        image_url: str,
        question: str = "请描述这张图片的内容",
        system: str = "",
    ) -> str:
        """让视觉模型看图并回答。``image_url`` 支持 data URL 或 http(s) URL。"""
        spec = self.spec
        if not spec.configured:
            raise ImageError("图像理解尚未配置（模型路由 → 图像理解）")
        url = _join_url(spec.base_url, "/chat/completions")
        if not url:
            raise ImageError("图像理解未填写 Base URL")

        content: List[Dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": question},
        ]
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        body = {"model": spec.model, "messages": messages, "max_tokens": 500}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=_headers(spec), json=body)
        except httpx.HTTPError as exc:
            raise ImageError("图像理解请求失败：%s" % exc) from exc
        if response.status_code >= 400:
            raise ImageError(
                "图像理解失败（HTTP %d）：%s" % (response.status_code, response.text[:200])
            )
        try:
            payload = response.json()
            message = (payload.get("choices") or [{}])[0].get("message") or {}
            return str(message.get("content") or "").strip()
        except Exception:
            raise ImageError("图像理解返回异常：%s" % response.text[:200])


__all__ = ["ImageError", "ImageGenerator", "Vision", "builtin_test_image_data_url", "image_data_url"]
