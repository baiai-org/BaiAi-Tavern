"""角色管理器：解析 + 落盘 + 数据库注册。"""

from .loader import (  # noqa: F401
    CharacterCard,
    CharacterCardError,
    build_card,
    decode_card_payload,
    load_card,
    load_card_bytes,
    normalize_card,
)
from .registry import CharacterRegistry  # noqa: F401

__all__ = [
    "CharacterCard",
    "CharacterCardError",
    "CharacterRegistry",
    "build_card",
    "decode_card_payload",
    "load_card",
    "load_card_bytes",
    "normalize_card",
]
