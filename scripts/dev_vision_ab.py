# -*- coding: utf-8 -*-
"""决定性对照：完全复刻 bot 视觉客户端参数（max_tokens=500/temp=0.85/top_p=1.0），
同一张真实图片，两种挂接位置，各跑 2 次看稳定性。

  B = 当前代码行为：图片挂在第一条历史 user 消息
  C = 修复后：图片挂在最后一条 user 消息（当前消息）

用法：设置 DEEPSEEK_API_KEY 环境变量，并把 IMG 改成你自己的 JPEG 图片路径。
"""
import base64
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
BIG = "data:image/jpeg;base64," + base64.b64encode(open(IMG, "rb").read()).decode()

SYSTEM = (
    "你正在扮演以下角色，请完全以该角色的身份、口吻和思维方式说话。\n\n"
    "【角色设定】\n姓名：小栖\n\n【行为规则】\n"
    "1. 始终保持角色身份，绝不能承认自己是 AI、语言模型或程序。\n"
    "2. 输出的是你在 QQ 上发给对方的消息本身，不要输出旁白、动作描写、括号说明、时间地点标注。\n"
    "3. 不要复述对方的话，不要加“角色名：”之类的前缀，不要使用引号包裹整句话。\n"
    "4. 语气自然口语化，像真实的人在聊天，可以有一点点情绪和个性。\n"
    "5. 单条消息尽量简短（不超过 120 字），一次只说一件事。"
)

HISTORY_MID = [
    {"role": "user", "content": "在吗"},
    {"role": "assistant", "content": "在的，怎么啦？"},
    {"role": "user", "content": "没事，就是有点困"},
    {"role": "assistant", "content": "困了就早点睡呀，别熬太晚。"},
    {"role": "user", "content": "你中午吃了什么"},
    {"role": "assistant", "content": "吃了一碗面，你还没吃吗？"},
]


def call(messages, label):
    body = {
        "model": "deepseek-flash",
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.85,
        "top_p": 1.0,
    }
    r = httpx.post(URL, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=120)
    data = r.json()
    ch = data["choices"][0]
    content = (ch.get("message") or {}).get("content") or ""
    print("[%s] HTTP %d finish=%s len=%d" % (label, r.status_code, ch.get("finish_reason"), len(content)))
    print("  %s" % content.replace("\n", " ⏎ "))
    return content


for i in (1, 2):
    # B：当前代码行为 —— 图片挂在第一条历史 user 消息
    msgs_b = [{"role": "system", "content": SYSTEM}]
    first_user = [
        {"type": "text", "text": "在吗"},
        {"type": "image_url", "image_url": {"url": BIG}},
    ]
    msgs_b.append({"role": "user", "content": first_user})
    rest = list(HISTORY_MID[1:])
    for item in rest:
        msgs_b.append(item)
    msgs_b.append({"role": "user", "content": "【图片】（对方发来了一条消息）"})
    call(msgs_b, "B-图片挂第一条历史 run%d" % i)

    # C：修复后 —— 图片挂在最后一条（当前）user 消息
    msgs_c = [{"role": "system", "content": SYSTEM}]
    msgs_c.extend(HISTORY_MID)
    msgs_c.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "【图片】（对方发来了一条消息）"},
                {"type": "image_url", "image_url": {"url": BIG}},
            ],
        }
    )
    call(msgs_c, "C-图片挂当前消息 run%d" % i)

print("DONE")
sys.exit(0)
