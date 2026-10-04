"""生成测试用角色卡（PNG tEXt / JSON / YAML）。"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Dict

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def tiny_png() -> bytes:
    """一张合法的 1x1 灰度 PNG（用于验证头像提取与 PNG 解析）。"""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00")
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def png_card_bytes(card: Dict[str, Any]) -> bytes:
    """把角色卡 JSON 塞进 PNG 的 tEXt(chara) 块。"""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00")
    payload = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"tEXt", b"chara\x00" + payload)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def card_v2(name: str, **overrides: Any) -> Dict[str, Any]:
    data = {
        "name": name,
        "description": "%s 是一个测试用角色，{{char}} 喜欢深夜聊天。" % name,
        "personality": "温柔、话不多，但很在意对方。",
        "scenario": "深夜的 QQ 私聊。",
        "first_mes": "在的，怎么啦？",
        "mes_example": "{{user}}：在吗？\n{{char}}：在呀，刚放下书。",
        "system_prompt": "说话简短自然，不要使用书面语。",
        "creator_notes": "测试角色",
        "tags": ["测试", "温柔"],
        "alternate_greetings": ["诶，你还没睡呀？"],
    }
    data.update(overrides)
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def write_png_card(path: Path, name: str, **overrides: Any) -> Path:
    path = Path(path)
    path.write_bytes(png_card_bytes(card_v2(name, **overrides)))
    return path


def write_json_card(path: Path, name: str, **overrides: Any) -> Path:
    path = Path(path)
    path.write_text(json.dumps(card_v2(name, **overrides), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_yaml_card(path: Path, name: str, **overrides: Any) -> Path:
    import yaml

    path = Path(path)
    path.write_text(
        yaml.safe_dump(card_v2(name, **overrides), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# ========================================================= 官方平台事件 =====
# 项目现在只有「QQ 官方机器人」这一种接入方式，事件是官方网关推送的
# ``{op, s, t, d}`` 结构，下面的辅助函数只负责拼出其中的 ``t`` 与 ``d``。
def official_c2c_event(
    text: str, openid: str = "mock-user-openid", message_id: str = "mock-msg-1"
) -> Dict[str, Any]:
    """构造一条官方单聊消息事件（C2C_MESSAGE_CREATE）。"""
    return {
        "type": "C2C_MESSAGE_CREATE",
        "data": {
            "id": message_id,
            "content": text,
            "timestamp": "1700000000",
            "author": {"user_openid": openid},
        },
    }


def official_group_at_event(
    text: str,
    group_openid: str = "mock-group-openid",
    member_openid: str = "mock-member-openid",
    message_id: str = "mock-msg-2",
    at_bot: bool = True,
) -> Dict[str, Any]:
    """构造一条官方群聊 @ 消息事件（GROUP_AT_MESSAGE_CREATE）。"""
    content = ("<@!1000000001> " + text) if at_bot else text
    return {
        "type": "GROUP_AT_MESSAGE_CREATE",
        "data": {
            "id": message_id,
            "content": content,
            "timestamp": "1700000000",
            "group_openid": group_openid,
            "author": {"member_openid": member_openid},
        },
    }


__all__ = [
    "card_v2",
    "official_c2c_event",
    "official_group_at_event",
    "png_card_bytes",
    "tiny_png",
    "write_json_card",
    "write_png_card",
    "write_yaml_card",
]
