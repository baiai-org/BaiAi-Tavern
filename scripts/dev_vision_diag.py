# -*- coding: utf-8 -*-
"""诊断 deepseek-flash 视觉请求返回空内容的原因。

用法：设置 DEEPSEEK_API_KEY 环境变量后运行（python scripts/dev_vision_diag.py）。
T3 / T4 需要一张真实 JPEG：把 IMG 改成本地图片路径即可（留档路径为开发时用的
data/media/inbox/ 下收到的 QQ 语音附件，仓库里不含该文件）。
"""
import base64
import json
import os
import struct
import sys
import zlib

import httpx

sys.stdout.reconfigure(encoding="utf-8")

KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-your-deepseek-api-key")
URL = "https://api.deepseek.com/v1/chat/completions"
IMG = os.path.join(os.path.dirname(__file__), "..", "data", "media", "inbox", "your_image.jpg")

if KEY.startswith("sk-your-"):
    print("请先设置 DEEPSEEK_API_KEY 环境变量再运行本脚本。")
    sys.exit(1)


def png_red_square(size=128):
    """生成一个纯红色 PNG（无依赖）。"""
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw_rows = b""
    for _ in range(size):
        raw_rows += b"\x00" + b"\xff\x00\x00" * size
    idat = zlib.compress(raw_rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def call(messages, label):
    body = {"model": "deepseek-flash", "messages": messages, "max_tokens": 200, "temperature": 0.3}
    r = httpx.post(URL, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=90)
    print("=" * 20, label, "HTTP", r.status_code)
    try:
        data = r.json()
    except Exception:
        print(r.text[:500])
        return
    if "choices" in data:
        ch = data["choices"][0]
        content = ch.get("message", {}).get("content")
        print("finish_reason=%s usage=%s" % (ch.get("finish_reason"), data.get("usage")))
        print("content=%r" % (content,))
    else:
        print(json.dumps(data, ensure_ascii=False)[:500])
    print()


# T1 纯文字（确认端点与 Key 正常）
call([{"role": "user", "content": "用一句话打个招呼。"}], "T1 纯文字")

# T2 红方块 PNG（正确 MIME），问颜色
red = base64.b64encode(png_red_square()).decode()
call(
    [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图片是什么颜色？"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + red}},
            ],
        }
    ],
    "T2 红方块PNG+正确MIME",
)

# T3 真实 JPEG（正确 MIME），重试一次
if not os.path.exists(IMG):
    print("T3/T4 跳过：%s 不存在（把 IMG 改成你自己的图片路径）" % IMG)
    sys.exit(0)
big = base64.b64encode(open(IMG, "rb").read()).decode()
call(
    [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片里有什么。"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + big}},
            ],
        }
    ],
    "T3 真实JPEG+正确MIME",
)

# T4 用户真实 JPEG（误标 PNG MIME）
call(
    [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片里有什么。"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + big}},
            ],
        }
    ],
    "T4 真实JPEG+误标PNG",
)

sys.exit(0)
