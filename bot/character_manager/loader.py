"""SillyTavern 角色卡解析器。

支持格式
--------
* ``PNG``：从 ``tEXt`` / ``zTXt`` / ``iTXt`` 块中读取 ``chara``（V2）或 ``ccv3``（V3）
  关键字，内容为 base64 编码的 JSON。
* ``JSON``：V1 扁平结构、``chara_card_v2`` / ``chara_card_v3`` 结构均可。
* ``YAML``：内容与 JSON 一致，只是序列化格式不同。

解析结果统一归一化到 :class:`CharacterCard`，缺失字段填空字符串，
保证下游（提示词构建、数据库写入）不需要再做防御性判断。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_KEYS = ("ccv3", "chara")
MAX_CARD_BYTES = 32 * 1024 * 1024

_NAME_RE = re.compile(r"[\\/:*?\"<>|\s]+")


class CharacterCardError(ValueError):
    """角色卡格式错误。"""


@dataclass
class CharacterCard:
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    creator_notes: str = ""
    tags: List[str] = field(default_factory=list)
    alternate_greetings: List[str] = field(default_factory=list)
    tts_voice: str = ""
    spec: str = ""
    source_path: str = ""
    avatar_bytes: Optional[bytes] = None
    avatar_suffix: str = ".png"
    raw: Dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- 输出
    def to_db_fields(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "personality": self.personality,
            "scenario": self.scenario,
            "first_mes": self.first_mes,
            "mes_example": self.mes_example,
            "system_prompt": self.system_prompt,
            "creator_notes": self.creator_notes,
            "tags": list(self.tags),
            "tts_voice": self.tts_voice,
            "card_spec": self.spec,
            "source_path": self.source_path,
        }

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        data = self.to_db_fields()
        data["alternate_greetings"] = list(self.alternate_greetings)
        data["has_avatar"] = self.avatar_bytes is not None
        data["name"] = self.name
        if include_raw:
            data["raw"] = self.raw
        return data

    def summary(self) -> str:
        parts = [
            "名称=%s" % self.name,
            "格式=%s" % (self.spec or "unknown"),
            "描述=%d 字" % len(self.description or ""),
            "性格=%d 字" % len(self.personality or ""),
        ]
        return ", ".join(parts)

    #: 核心人设字段（卡片本身没写时，界面上对应输入框就是空的）
    CORE_FIELD_LABELS = (
        ("description", "描述"),
        ("personality", "性格"),
        ("scenario", "场景"),
        ("mes_example", "示例对话"),
        ("system_prompt", "系统指令"),
    )

    def missing_core_fields(self) -> List[str]:
        """卡片本身留空的核心字段中文名。

        Chub 等平台导出的卡片常常只写「描述 + 开场白 + 作者备注」，
        其余字段为空——这是卡片内容问题而非解析丢失，导入后提示用户，
        避免误以为程序把字段弄丢了。
        """
        missing = []
        for key, label in self.CORE_FIELD_LABELS:
            if not str(getattr(self, key) or "").strip():
                missing.append(label)
        return missing


# ============================================================== PNG 解析 =====
def _safe_name(value: str, fallback: str = "character") -> str:
    cleaned = _NAME_RE.sub("_", (value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned[:48] or fallback


def decode_card_payload(text: str) -> Dict[str, Any]:
    """把 tEXt 里的角色卡文本解码为字典。

    兼容：标准 base64 / URL-safe base64（``-`` ``_`` 替代 ``+`` ``/``，
    部分工具写成 urlsafe 后标准解码会静默错位）/ 个别工具直接写明文 JSON。
    """
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        raise CharacterCardError("角色卡内容为空")
    candidates: List[bytes] = []
    padding = "=" * ((-len(compact)) % 4)
    for decoder in (
        lambda s: base64.b64decode(s + padding, validate=True),
        lambda s: base64.urlsafe_b64decode(s + padding, ),
        lambda s: base64.b64decode(s + padding),
    ):
        try:
            candidates.append(decoder(compact))
        except (binascii.Error, ValueError):
            continue
    for raw in candidates:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    # 兜底：个别工具把明文 JSON 直接写进 tEXt。先试原文（保留字符串内空白），
    # 再去空白兜底（紧凑 JSON 也合法）。
    for source in ((text or "").strip(), compact):
        if not source:
            continue
        try:
            data = json.loads(source)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise CharacterCardError("角色卡内容无法解码（base64 与 JSON 均失败）")


def _decode_text_payload(raw: bytes) -> str:
    """tEXt / zTXt 规范上是 latin-1，但不少工具把 UTF-8 字节直接写进去；
    先按 UTF-8 试，失败再退回 latin-1，两种写法都能读。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _parse_png_text_chunk(chunk_type: bytes, payload: bytes) -> Optional[Tuple[str, str]]:
    try:
        if chunk_type == b"tEXt":
            keyword, _, text = payload.partition(b"\x00")
            return keyword.decode("latin-1", "replace"), _decode_text_payload(text)
        if chunk_type == b"zTXt":
            keyword, _, rest = payload.partition(b"\x00")
            if len(rest) < 1 or rest[0] != 0:
                return None
            return (
                keyword.decode("latin-1", "replace"),
                _decode_text_payload(zlib.decompress(rest[1:])),
            )
        if chunk_type == b"iTXt":
            keyword, _, rest = payload.partition(b"\x00")
            if len(rest) < 2:
                return None
            compressed, method, rest = rest[0], rest[1], rest[2:]
            if method != 0:
                return None
            _, _, rest = rest.partition(b"\x00")  # language tag
            _, _, text = rest.partition(b"\x00")  # translated keyword
            if compressed:
                text = zlib.decompress(text)
            return keyword.decode("latin-1", "replace"), text.decode("utf-8", "replace")
    except Exception:
        return None
    return None


def extract_png_metadata(data: bytes) -> Dict[str, str]:
    """遍历 PNG 块，返回所有文本关键字 -> 文本内容。"""
    if not data.startswith(PNG_SIGNATURE):
        raise CharacterCardError("不是有效的 PNG 文件")
    metadata: Dict[str, str] = {}
    offset = len(PNG_SIGNATURE)
    total = len(data)
    while offset + 12 <= total:
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end > total or length < 0:
            break
        payload = data[start:end]
        if chunk_type in (b"tEXt", b"zTXt", b"iTXt"):
            parsed = _parse_png_text_chunk(chunk_type, payload)
            if parsed:
                metadata[parsed[0]] = parsed[1]
        elif chunk_type == b"IEND":
            break
        offset = end + 4  # 跳过 CRC
    return metadata


def _pillow_metadata(data: bytes) -> Dict[str, str]:
    """Pillow 兜底：部分写入工具只在 tag 中出现 chara。"""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            info = dict(getattr(image, "info", {}) or {})
            return {str(k): str(v) for k, v in info.items() if isinstance(v, str)}
    except Exception:
        return {}


def parse_png(data: bytes) -> Dict[str, Any]:
    metadata = extract_png_metadata(data)
    for key in PNG_TEXT_KEYS:
        if key in metadata:
            return decode_card_payload(metadata[key])
    lower_map = {str(k).lower(): v for k, v in metadata.items()}
    for key in PNG_TEXT_KEYS:
        if key in lower_map:
            return decode_card_payload(lower_map[key])
    fallback = _pillow_metadata(data)
    for key in PNG_TEXT_KEYS:
        if key in fallback:
            return decode_card_payload(fallback[key])
    raise CharacterCardError(
        "该 PNG 中没有找到 chara / ccv3 元数据，不是 SillyTavern 角色卡"
    )


# =========================================================== JSON / YAML =====
def parse_json_text(text: str) -> Dict[str, Any]:
    cleaned = (text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise CharacterCardError("文件内容为空")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CharacterCardError("JSON 解析失败: %s" % exc) from exc
    if not isinstance(data, dict):
        raise CharacterCardError("角色卡内容必须是 JSON 对象")
    return data


def parse_yaml_text(text: str) -> Dict[str, Any]:
    if yaml is None:  # pragma: no cover
        raise CharacterCardError("缺少 PyYAML 依赖，无法解析 YAML 角色卡")
    cleaned = (text or "").lstrip("\ufeff")
    try:
        data = yaml.safe_load(cleaned)
    except Exception as exc:
        raise CharacterCardError("YAML 解析失败: %s" % exc) from exc
    if not isinstance(data, dict):
        raise CharacterCardError("角色卡内容必须是键值映射")
    return data


# ============================================================== 归一化 =======
def _first_text(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _normalize_tags(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _split_spec(data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    spec = str(data.get("spec") or data.get("spec_version") or "").strip()
    inner = data.get("data")
    if isinstance(inner, dict) and inner:
        return inner, spec or "chara_card_v2"
    return data, spec or "chara_card_v1"


def normalize_card(
    data: Dict[str, Any],
    fallback_name: str = "character",
    source_path: str = "",
) -> CharacterCard:
    payload, spec = _split_spec(data)
    if not isinstance(payload, dict):
        raise CharacterCardError("角色卡数据结构无法识别")

    name = _first_text(payload, "name", "char_name", "character_name") or fallback_name
    card = CharacterCard(
        name=name.strip()[:60],
        description=_first_text(payload, "description", "char_persona", "desc"),
        personality=_first_text(payload, "personality", "char_personality"),
        scenario=_first_text(payload, "scenario", "world_scenario"),
        first_mes=_first_text(payload, "first_mes", "first_message", "greeting"),
        mes_example=_first_text(payload, "mes_example", "example_dialogue", "example_messages"),
        system_prompt=_first_text(
            payload, "system_prompt", "post_history_instructions", "char_system_prompt"
        ),
        creator_notes=_first_text(payload, "creator_notes", "creatorcomment", "creator_notes_memo"),
        tags=_normalize_tags(payload.get("tags")),
        tts_voice=_first_text(payload, "tts_voice"),
        spec=spec,
        source_path=source_path,
        raw=data,
    )

    greetings = payload.get("alternate_greetings")
    if isinstance(greetings, (list, tuple)):
        card.alternate_greetings = [str(item) for item in greetings if str(item).strip()]

    # V1 卡片常把性格塞在 description 中，保留原样即可（提示词会全量带上）
    if not card.description and card.personality:
        card.description = card.personality

    return card


_MAX_AVATAR_BYTES = 8 * 1024 * 1024


def _image_magic_suffix(data: bytes) -> Optional[str]:
    """按文件头识别图片格式，返回扩展名；不是图片返回 None。"""
    if not data:
        return None
    if data.startswith(PNG_SIGNATURE):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def _download_image(url: str) -> Optional[bytes]:
    """下载角色卡里内嵌的远程头像 URL（Chub 等平台的卡片常用）。"""
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BaiAi-Tavern/0.2"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(_MAX_AVATAR_BYTES + 1)
    except Exception:
        return None
    if len(data) > _MAX_AVATAR_BYTES:
        return None
    return data if _image_magic_suffix(data) else None


def _b64_to_image(text: str) -> Optional[Tuple[bytes, str]]:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return None
    for decoder in (
        lambda s: base64.b64decode(s + "=" * ((-len(s)) % 4), validate=True),
        lambda s: base64.urlsafe_b64decode(s + "=" * ((-len(s)) % 4)),
        lambda s: base64.b64decode(s + "=" * ((-len(s)) % 4)),
    ):
        try:
            raw = decoder(compact)
        except (binascii.Error, ValueError):
            continue
        suffix = _image_magic_suffix(raw)
        if suffix:
            return raw, suffix
    return None


def _extract_avatar(payload: Dict[str, Any], data: Dict[str, Any]) -> Tuple[Optional[bytes], str]:
    """卡片里的头像：远程 URL（Chub 等）/ data URL / base64 / V3 assets。

    只有能通过图片魔数验证的字节才会被采用——早期版本会把 URL 字符串
    直接当 base64 解码，得到几十字节垃圾当成 PNG 保存（用户实测坏头像）。
    """
    candidates: List[str] = []
    inner = data.get("data")
    for source in (payload, data, inner if isinstance(inner, dict) else {}):
        if isinstance(source, dict):
            for key in ("avatar", "avatar_base64", "image"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
    # V3：assets 数组里的 icon / avatar（data URL 或 https URL）
    for source in (payload, inner if isinstance(inner, dict) else {}):
        assets = (source or {}).get("assets") if isinstance(source, dict) else None
        if isinstance(assets, list):
            for item in assets:
                if isinstance(item, dict) and str(item.get("type") or "") in ("icon", "avatar"):
                    uri = item.get("uri")
                    if isinstance(uri, str) and uri.strip():
                        candidates.append(uri.strip())

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.startswith(("http://", "https://")):
            raw = _download_image(candidate)
            if raw:
                return raw, _image_magic_suffix(raw) or ".png"
            continue
        if candidate.startswith("data:"):
            header, _, payload_text = candidate.partition(",")
            if "base64" not in header:
                continue
            decoded = _b64_to_image(payload_text)
            if decoded:
                return decoded
            continue
        decoded = _b64_to_image(candidate)
        if decoded:
            return decoded
    return None, ".png"


# ============================================================== 入口 =========
def build_card(data: Dict[str, Any], fallback_name: str = "character", source_path: str = "") -> CharacterCard:
    card = normalize_card(data, fallback_name=fallback_name, source_path=source_path)
    payload, _ = _split_spec(data)
    avatar, suffix = _extract_avatar(payload, data)
    card.avatar_bytes = avatar
    card.avatar_suffix = suffix
    return card


def guess_format(filename: str = "", data: Optional[bytes] = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".png", ".apng"):
        return "png"
    if suffix in (".json",):
        return "json"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if data:
        if data.startswith(PNG_SIGNATURE):
            return "png"
        head = data[:512].lstrip(b"\xef\xbb\xbf \r\n\t")
        if head.startswith(b"{") or head.startswith(b"["):
            return "json"
        if b":" in head:
            return "yaml"
    return ""


def load_card_bytes(filename: str, data: bytes) -> CharacterCard:
    """从内存字节流解析角色卡（GUI 上传走这条路径）。"""
    if not data:
        raise CharacterCardError("文件内容为空")
    if len(data) > MAX_CARD_BYTES:
        raise CharacterCardError("角色卡文件过大（>32MB）")
    fmt = guess_format(filename, data)
    fallback_name = _safe_name(Path(filename or "character").stem)
    if fmt == "png":
        card = build_card(parse_png(data), fallback_name=fallback_name)
        card.avatar_bytes = data
        card.avatar_suffix = ".png"
        return card
    if fmt == "json":
        return build_card(parse_json_text(data.decode("utf-8", errors="replace")), fallback_name=fallback_name)
    if fmt == "yaml":
        return build_card(parse_yaml_text(data.decode("utf-8", errors="replace")), fallback_name=fallback_name)
    # 未知扩展名：依次尝试
    errors = []
    for parser in (
        lambda: build_card(parse_png(data), fallback_name=fallback_name),
        lambda: build_card(parse_json_text(data.decode("utf-8", errors="replace")), fallback_name=fallback_name),
        lambda: build_card(parse_yaml_text(data.decode("utf-8", errors="replace")), fallback_name=fallback_name),
    ):
        try:
            return parser()
        except CharacterCardError as exc:
            errors.append(str(exc))
    raise CharacterCardError("无法识别的角色卡格式：%s" % (errors[-1] if errors else "未知"))


def load_card(path: Path) -> CharacterCard:
    """从文件解析角色卡。"""
    path = Path(path)
    if not path.exists():
        raise CharacterCardError("文件不存在: %s" % path)
    data = path.read_bytes()
    card = load_card_bytes(path.name, data)
    card.source_path = str(path)
    return card
