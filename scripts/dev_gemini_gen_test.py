# -*- coding: utf-8 -*-
"""实测：gemini-native 生图线路，验证 503 重试后能否出图。

用法：设置 GEMINI_API_KEY 环境变量（AQ. 前缀的 Gemini CLI Key 也可）后运行。
"""
import base64
import os
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

KEY = os.environ.get("GEMINI_API_KEY", "your-gemini-api-key")
MODEL = os.environ.get("GEMINI_TEST_MODEL", "gemini-3.1-flash-lite-image")
URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (MODEL, KEY)

if KEY.startswith("your-gemini"):
    print("请先设置 GEMINI_API_KEY 环境变量再运行本脚本。")
    sys.exit(1)
BODY = {
    "contents": [{"parts": [{"text": "深夜的窗边小桌，一盏暖黄的台灯，旁边放着一杯冒热气的茶，水彩风格"}]}],
    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
}

for attempt in (1, 2, 3):
    r = httpx.post(URL, json=BODY, headers={"Content-Type": "application/json"}, timeout=120)
    print("attempt %d: HTTP %d" % (attempt, r.status_code))
    if r.status_code >= 400:
        print(r.text[:300])
        if r.status_code in (500, 502, 503):
            import time

            time.sleep(4)
            continue
        break
    data = r.json()
    parts = []
    for cand in data.get("candidates") or []:
        content = (cand or {}).get("content") or {}
        parts = content.get("parts") or []
    got = False
    for part in parts:
        if isinstance(part, dict):
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                out = os.path.join(os.path.dirname(__file__), "gen_test_%d.png" % attempt)
                open(out, "wb").write(base64.b64decode(inline["data"]))
                print("出图成功！mimeType=%s bytes=%d 已存 %s" % (inline.get("mimeType"), len(inline["data"]), out))
                got = True
    if got:
        break
    print("无图片 parts:", str(parts)[:200])

print("DONE")
sys.exit(0)
