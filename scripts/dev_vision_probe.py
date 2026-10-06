# -*- coding: utf-8 -*-
"""实测：deepseek-flash 视觉线路 + 一张真实 JPEG。

三种请求形态对照（要求模型描述图片）：
  A: 干净对话，图片在（唯一）user 消息，正确 MIME image/jpeg
  B: 有历史多轮，图片挂在第一条历史 user 消息，MIME 误标 image/png —— 精确复现当前代码
  C: 有历史多轮，图片挂在最后一条 user 消息，MIME 误标 image/png —— 只修挂接位置

用法：设置 DEEPSEEK_API_KEY 环境变量，并把 IMG 改成本地 JPEG 图片路径。
"""
import base64
import json
import os
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-your-deepseek-api-key")
URL = "https://api.deepseek.com/v1/chat/completions"
IMG = os.path.join(os.path.dirname(__file__), "..", "data", "media", "inbox", "your_image.jpg")

if KEY.startswith("sk-your-") or not os.path.exists(IMG):
    print("请先设置 DEEPSEEK_API_KEY，并把 IMG 改成本地 JPEG 图片路径。")
    sys.exit(1)

raw = open(IMG, "rb").read()
b64 = base64.b64encode(raw).decode()
URL_JPEG = "data:image/jpeg;base64," + b64
URL_PNG = "data:image/png;base64," + b64

results = {}


def ask(messages, label):
    body = {"model": "deepseek-flash", "messages": messages, "max_tokens": 200, "temperature": 0.3}
    r = httpx.post(URL, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=90)
    print("=" * 25, label, "HTTP", r.status_code)
    try:
        data = r.json()
        if "choices" in data:
            content = data["choices"][0]["message"]["content"]
            print("len=%d" % len(content or ""))
            print(content)
            return bool((content or "").strip())
        print(json.dumps(data, ensure_ascii=False)[:400])
    except Exception:
        print("非 JSON 响应:", r.text[:300])
    print()
    return r.status_code == 200


results["A"] = ask(
    [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片里有什么。"},
                {"type": "image_url", "image_url": {"url": URL_JPEG}},
            ],
        }
    ],
    "A 干净对话+当前消息+正确MIME",
)

results["B"] = ask(
    [
        {"role": "system", "content": "你是小栖，温柔体贴的女孩。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "在吗"},
                {"type": "image_url", "image_url": {"url": URL_PNG}},
            ],
        },
        {"role": "assistant", "content": "在的，怎么啦？"},
        {"role": "user", "content": "没事，就是有点困"},
        {"role": "assistant", "content": "困了就早点睡呀"},
        {"role": "user", "content": "【图片】（对方发来了一条消息）"},
    ],
    "B 图片挂第一条历史+误标MIME（当前代码）",
)

results["C"] = ask(
    [
        {"role": "system", "content": "你是小栖，温柔体贴的女孩。"},
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的，怎么啦？"},
        {"role": "user", "content": "没事，就是有点困"},
        {"role": "assistant", "content": "困了就早点睡呀"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "【图片】（对方发来了一条消息）"},
                {"type": "image_url", "image_url": {"url": URL_PNG}},
            ],
        },
    ],
    "C 图片挂最后user+误标MIME",
)

print("RESULT:", json.dumps(results))
sys.exit(0)
