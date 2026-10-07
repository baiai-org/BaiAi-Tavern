"""本地模拟服务：QQ 官方机器人开放平台 + OpenAI 兼容 LLM。

本程序只有一种 QQ 接入方式——**QQ 官方机器人**（AppID + AppSecret → access_token →
WebSocket 网关），因此这里复刻的就是官方平台的四件套：

* ``POST /app/getAppAccessToken`` —— 凭证接口（**失败时 HTTP 仍是 200，必须看 code**）
* ``GET  /users/@me`` / ``GET /gateway`` —— 机器人信息与网关地址
* ``POST /v2/users/{openid}/messages`` / ``POST /v2/groups/{group_openid}/messages``
  —— 记录机器人发出的消息（被动回复带 msg_id + msg_seq，主动消息不带）
* ``WS   /official-ws`` —— Hello → Identify → READY，并向机器人推送事件
* ``POST /v1/chat/completions`` —— 返回固定的 mock 回复（OpenAI 兼容）

既可以在测试进程内以独立进程方式启动（:class:`MockProcess`），也可以直接运行::

    python -m tests.mock_servers --port 3100

其它自检脚本依赖的公开 API
--------------------------
* :class:`MockProcess`：``.base_url`` / ``.start()`` / ``.stop()`` / ``.state()`` /
  ``.reset()`` / ``.official_sent()``
* :meth:`MockProcess.emit_c2c` / :meth:`MockProcess.emit_group` /
  :meth:`MockProcess.emit_friend_add` / :meth:`MockProcess.drop_gateway`
* 模块级：:func:`reset` / :data:`STATE` / ``OFFICIAL_APP_ID`` /
  ``OFFICIAL_APP_SECRET`` / ``OFFICIAL_TOKEN``
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="BaiAi-Tavern Mock", docs_url=None, redoc_url=None, openapi_url=None)

# 官方平台 mock 的凭据（与自检配置里写入的 AppID / AppSecret 保持一致）
OFFICIAL_APP_ID = "mock-app-id"
OFFICIAL_APP_SECRET = "mock-app-secret"
OFFICIAL_TOKEN = "mock-access-token"

#: 默认的私聊 / 群聊目标 openid（官方平台只按 openid 发送）
DEFAULT_OPENID = "mock-user-openid"
DEFAULT_GROUP_OPENID = "mock-group-openid"
DEFAULT_MEMBER_OPENID = "mock-member-openid"

STATE: Dict[str, Any] = {
    "official_sent": [],      # 官方接口发出的消息（c2c / group）
    "llm_requests": [],       # LLM 收到的请求
    "reply_text": "这是 mock 角色回复。",
    "proactive_text": "这是 mock 主动消息。",
    "vision_text": "我看你发的图了（mock 视觉回复）。",
    "last_llm_user_text": "",  # 最近一次 LLM 请求里最后一条 user 消息的文本
    "official_ws": set(),     # 已连接的官方网关 socket
    "official_seq": 0,        # 事件序号（官方网关的 s 字段）
    "official_intents": 0,    # 最近一次 Identify 上报的 intents
    "official_identify": 0,   # Identify 次数
    "official_resume": 0,     # Resume 次数
    "official_drops": 0,      # 被测试主动掐断的网关次数
    "token_calls": 0,         # 凭证接口调用次数
    "model_calls": 0,         # /v1/models 调用次数
    # ------------------------------------------------------------ V0.2 富媒体
    "media_uploads": [],      # /files 上传记录（base64 或合并）
    "media_chunk_puts": 0,    # 分片 PUT 次数
    "tts_calls": 0,           # /audio/speech 调用次数
    "image_gen_calls": 0,     # /images/generations 调用次数
    "vision_calls": 0,        # 带 image_url 的 chat/completions 次数
    "media_files_served": 0,  # 附件文件被下载次数
}


def reset(
    reply_text: Optional[str] = None,
    proactive_text: Optional[str] = None,
    vision_text: Optional[str] = None,
) -> None:
    """清空记录（网关连接保留）。

    注意：``official_seq`` 不清零——官方网关的事件序号（s）本来就单调递增，
    事件 id 也基于它生成；清零会让 mock 消息 id 跨 reset 复用。
    """
    STATE["official_sent"] = []
    STATE["llm_requests"] = []
    STATE["last_llm_user_text"] = ""
    STATE["official_identify"] = 0
    STATE["official_resume"] = 0
    STATE["official_drops"] = 0
    STATE["token_calls"] = 0
    STATE["model_calls"] = 0
    STATE["media_uploads"] = []
    STATE["media_chunk_puts"] = 0
    STATE["tts_calls"] = 0
    STATE["image_gen_calls"] = 0
    STATE["vision_calls"] = 0
    STATE["media_files_served"] = 0
    STATE["wav_variant_served"] = 0
    STATE["llm_last_model"] = ""
    STATE["llm_last_last_user"] = ""
    STATE["llm_last_messages"] = []
    STATE["resets"] = int(STATE.get("resets", 0)) + 1
    if reply_text is not None:
        STATE["reply_text"] = reply_text
    if proactive_text is not None:
        STATE["proactive_text"] = proactive_text
    if vision_text is not None:
        STATE["vision_text"] = vision_text


# ====================================================== QQ 官方机器人 mock ==
@app.post("/app/getAppAccessToken")
async def official_access_token(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """模拟官方凭证接口：失败时 HTTP 也是 200，靠 code 判断（与真实行为一致）。"""
    app_id = str(payload.get("appId") or "")
    secret = str(payload.get("clientSecret") or "")
    STATE["token_calls"] = int(STATE.get("token_calls", 0)) + 1
    if app_id != OFFICIAL_APP_ID:
        # 100007：AppID 无效 / 机器人状态异常
        return JSONResponse({"code": 100007, "message": "appid invalid"})
    if secret != OFFICIAL_APP_SECRET:
        # 100016：AppID 或 AppSecret 不正确
        return JSONResponse({"code": 100016, "message": "invalid appid or secret"})
    return JSONResponse({"access_token": OFFICIAL_TOKEN, "expires_in": "7200"})


def _check_official_token(authorization: str) -> bool:
    return authorization == "QQBot %s" % OFFICIAL_TOKEN


@app.get("/users/@me")
async def official_me(request: Request) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    return JSONResponse({"id": "1000000001", "username": "Mock官方机器人", "bot": True})


@app.get("/gateway")
async def official_gateway(request: Request) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    base = str(request.base_url).rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    return JSONResponse({"url": "%s/official-ws" % base})


@app.post("/v2/users/{openid}/messages")
async def official_send_c2c(
    openid: str, request: Request, payload: Dict[str, Any] = Body(default={})
) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    record = {
        "kind": "c2c",
        "openid": openid,
        "content": payload.get("content"),
        "msg_type": payload.get("msg_type", 0),
        "file_info": (payload.get("media") or {}).get("file_info"),
        "msg_id": payload.get("msg_id"),
        "msg_seq": payload.get("msg_seq"),
        "at": time.time(),
    }
    STATE["official_sent"].append(record)
    # 官方限制：同一 msg_id + msg_seq 不能重复回复
    for item in STATE["official_sent"][:-1]:
        if item.get("msg_id") and (item.get("msg_id"), item.get("msg_seq")) == (
            record["msg_id"],
            record["msg_seq"],
        ):
            return JSONResponse({"code": 40054, "message": "duplicate msg_seq"})
    return JSONResponse(
        {"id": "mock-msg-%d" % len(STATE["official_sent"]), "timestamp": int(time.time())}
    )


@app.post("/v2/groups/{group_openid}/messages")
async def official_send_group(
    group_openid: str, request: Request, payload: Dict[str, Any] = Body(default={})
) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    STATE["official_sent"].append(
        {
            "kind": "group",
            "group_openid": group_openid,
            "content": payload.get("content"),
            "msg_type": payload.get("msg_type", 0),
            "file_info": (payload.get("media") or {}).get("file_info"),
            "msg_id": payload.get("msg_id"),
            "msg_seq": payload.get("msg_seq"),
            "at": time.time(),
        }
    )
    return JSONResponse(
        {"id": "mock-gmsg-%d" % len(STATE["official_sent"]), "timestamp": int(time.time())}
    )


# ==================================================== V0.2 富媒体上传 mock ====
def _record_upload(kind: str, target: str, file_type: int, size: int, name: str) -> Dict[str, Any]:
    index = len(STATE["media_uploads"]) + 1
    record = {
        "kind": kind,
        "target": target,
        "file_type": int(file_type),
        "size": int(size),
        "name": name,
        "file_info": "mock-file-info-%d" % index,
        "at": time.time(),
    }
    STATE["media_uploads"].append(record)
    return record


@app.post("/v2/users/{openid}/files")
async def official_upload_c2c(openid: str, request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """base64 直传或分片合并（同一 URL，按 body 区分，与官方平台一致）。"""
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    upload_id = payload.get("upload_id")
    if upload_id:
        record = _record_upload("c2c", openid, int(payload.get("file_type") or 0), 0, "merge:%s" % upload_id)
    else:
        import base64

        data = base64.b64decode(str(payload.get("file_data") or ""))
        record = _record_upload("c2c", openid, int(payload.get("file_type") or 0), len(data), str(payload.get("file_name") or ""))
    return JSONResponse({"file_uuid": "mock-uuid-%d" % len(STATE["media_uploads"]), "file_info": record["file_info"], "ttl": 600})


@app.post("/v2/groups/{group_openid}/files")
async def official_upload_group(group_openid: str, request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    upload_id = payload.get("upload_id")
    if upload_id:
        record = _record_upload("group", group_openid, int(payload.get("file_type") or 0), 0, "merge:%s" % upload_id)
    else:
        import base64

        data = base64.b64decode(str(payload.get("file_data") or ""))
        record = _record_upload("group", group_openid, int(payload.get("file_type") or 0), len(data), str(payload.get("file_name") or ""))
    return JSONResponse({"file_uuid": "mock-uuid-%d" % len(STATE["media_uploads"]), "file_info": record["file_info"], "ttl": 600})


@app.post("/v2/users/{openid}/upload_prepare")
async def official_upload_prepare_c2c(openid: str, request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """分片上传第 1 步（mock：固定 2 片，预签名 URL 指向本服务）。"""
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    upload_id = "mock-upload-%d" % (len(STATE["media_uploads"]) + 1)
    file_size = int(str(payload.get("file_size") or 0))
    block_size = max(1024, (file_size + 1) // 2)
    parts = []
    for index in range(2):
        parts.append(
            {
                "index": index,
                "presigned_url": "http://%s/upload_chunk/%s/%d" % (request.url.netloc, upload_id, index),
                "block_size": block_size,
            }
        )
    return JSONResponse({"upload_id": upload_id, "block_size": block_size, "parts": parts})


@app.put("/upload_chunk/{upload_id}/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request) -> JSONResponse:
    body = await request.body()
    STATE["media_chunk_puts"] = int(STATE.get("media_chunk_puts", 0)) + 1
    return JSONResponse({"ok": True, "size": len(body)})


@app.post("/v2/users/{openid}/upload_part_finish")
async def official_upload_part_finish_c2c(openid: str, request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    if not _check_official_token(request.headers.get("authorization", "")):
        return JSONResponse({"code": 11244, "message": "token invalid"}, status_code=401)
    return JSONResponse({"code": 0, "message": "ok"})


@app.websocket("/official-ws")
async def official_ws(websocket: WebSocket) -> None:
    """模拟官方 WebSocket 网关：Hello / Identify / Resume / READY / 心跳。"""
    await websocket.accept()
    STATE["official_ws"].add(websocket)
    try:
        # 心跳间隔取 5 秒：既能让连接保活，又能在自检里覆盖心跳与 ACK 分支
        await websocket.send_json({"op": 10, "d": {"heartbeat_interval": 5000}})
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            op = int(payload.get("op") or 0)
            if op in (2, 6):  # Identify / Resume
                intents = (payload.get("d") or {}).get("intents")
                if op == 6:
                    STATE["official_resume"] = int(STATE.get("official_resume", 0)) + 1
                    await websocket.send_json({"op": 0, "s": 1, "t": "RESUMED", "d": {}})
                else:
                    STATE["official_intents"] = intents
                    STATE["official_identify"] = int(STATE.get("official_identify", 0)) + 1
                    await websocket.send_json(
                        {
                            "op": 0,
                            "s": 1,
                            "t": "READY",
                            "d": {
                                "version": 1,
                                "session_id": "mock-session",
                                "shard": [0, 1],
                                "user": {
                                    "id": "1000000001",
                                    "username": "Mock官方机器人",
                                    "bot": True,
                                },
                            },
                        }
                    )
            elif op == 1:  # 心跳
                await websocket.send_json({"op": 11, "d": None})
            else:  # 机器人上行（如 op=2 之后的业务帧）：回 ACK，防止对端缓冲写阻塞
                await websocket.send_json({"op": 3, "s": int(payload.get("s") or 0), "t": None, "d": None})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        STATE["official_ws"].discard(websocket)


async def _broadcast_official(event_type: str, data: Dict[str, Any]) -> int:
    STATE["official_seq"] = int(STATE.get("official_seq", 0)) + 1
    payload = {"op": 0, "s": STATE["official_seq"], "t": event_type, "d": data}
    sent = 0
    for socket in list(STATE["official_ws"]):
        try:
            await socket.send_json(payload)
            sent += 1
        except Exception:
            STATE["official_ws"].discard(socket)
    return sent


@app.post("/__control/emit_c2c")
async def control_emit_c2c(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """模拟「用户给机器人发单聊消息」（C2C_MESSAGE_CREATE），可带图片/语音附件。"""
    data = {
        "id": payload.get("id") or "mock-c2c-%d" % (STATE["official_seq"] + 1),
        "content": payload.get("content") or "你好呀",
        "timestamp": str(int(time.time())),
        "author": {"user_openid": payload.get("openid") or DEFAULT_OPENID},
    }
    attachments = payload.get("attachments")
    if attachments:
        data["attachments"] = attachments
    delivered = await _broadcast_official("C2C_MESSAGE_CREATE", data)
    return {"ok": bool(delivered), "delivered": delivered, "id": data["id"]}


@app.post("/__control/emit_group")
async def control_emit_group(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """模拟「群里 @ 了机器人」（GROUP_AT_MESSAGE_CREATE），可带附件。"""
    data = {
        "id": payload.get("id") or "mock-group-%d" % (STATE["official_seq"] + 1),
        "content": payload.get("content") or "群里在聊什么",
        "timestamp": str(int(time.time())),
        "group_openid": payload.get("group_openid") or DEFAULT_GROUP_OPENID,
        "author": {"member_openid": payload.get("member_openid") or DEFAULT_MEMBER_OPENID},
    }
    attachments = payload.get("attachments")
    if attachments:
        data["attachments"] = attachments
    delivered = await _broadcast_official("GROUP_AT_MESSAGE_CREATE", data)
    return {"ok": bool(delivered), "delivered": delivered, "id": data["id"]}


@app.post("/__control/emit_friend_add")
async def control_emit_friend_add(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """模拟「用户添加机器人」（FRIEND_ADD）。"""
    data = {
        "timestamp": int(time.time()),
        "openid": payload.get("openid") or DEFAULT_OPENID,
    }
    delivered = await _broadcast_official("FRIEND_ADD", data)
    return {"ok": bool(delivered), "delivered": delivered, "openid": data["openid"]}


@app.post("/__control/drop_gateway")
async def control_drop_gateway() -> Dict[str, Any]:
    """掐断所有官方网关连接（用于验证「网关断开后自动重连」）。"""
    closed = 0
    for socket in list(STATE["official_ws"]):
        try:
            await socket.close(code=1001)
            closed += 1
        except Exception:
            pass
        STATE["official_ws"].discard(socket)
    STATE["official_drops"] = int(STATE.get("official_drops", 0)) + 1
    return {"ok": True, "closed": closed, "drops": STATE["official_drops"]}


@app.post("/__control/reset")
async def control_reset(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    reset(
        payload.get("reply_text"),
        payload.get("proactive_text"),
        payload.get("vision_text"),
    )
    return {"ok": True, "reply_text": STATE["reply_text"]}


@app.get("/__control/state")
async def control_state() -> Dict[str, Any]:
    return {
        "official_sent": list(STATE["official_sent"]),
        "official_ws": len(STATE["official_ws"]),
        "official_seq": int(STATE.get("official_seq", 0)),
        "official_intents": int(STATE.get("official_intents", 0)),
        "official_identify": int(STATE.get("official_identify", 0)),
        "official_resume": int(STATE.get("official_resume", 0)),
        "official_drops": int(STATE.get("official_drops", 0)),
        "resets": int(STATE.get("resets", 0)),
        "token_calls": int(STATE.get("token_calls", 0)),
        "llm_calls": len(STATE["llm_requests"]),
        "llm_requests": list(STATE["llm_requests"]),
        "last_llm_user_text": str(STATE.get("last_llm_user_text", "")),
        "llm_last_model": str(STATE.get("llm_last_model", "")),
        "llm_last_last_user": str(STATE.get("llm_last_last_user", "")),
        "llm_last_messages": list(STATE.get("llm_last_messages") or []),
        "model_calls": int(STATE.get("model_calls", 0)),
        "reply_text": STATE["reply_text"],
        "media_uploads": list(STATE["media_uploads"]),
        "media_chunk_puts": int(STATE.get("media_chunk_puts", 0)),
        "tts_calls": int(STATE.get("tts_calls", 0)),
        "image_gen_calls": int(STATE.get("image_gen_calls", 0)),
        "vision_calls": int(STATE.get("vision_calls", 0)),
        "vision_image_in_last_user": bool(STATE.get("vision_image_in_last_user", False)),
        "media_files_served": int(STATE.get("media_files_served", 0)),
        "wav_variant_served": int(STATE.get("wav_variant_served", 0)),
    }


# ======================================================= V0.2 附件文件 mock ==
# 1x1 蓝色 PNG（真实 PNG 头 + IHDR/IDAT/IEND，Pillow 可直接解码）
_MOCK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x9a\x19\xa2\x00\x00\x00\x00IEND\xaeB\x60\x82"
)

_MOCK_SILK_CACHE: Optional[bytes] = None


def _mock_silk() -> bytes:
    """生成一小段真实 silk（0.5 秒 16k 正弦）：pilk 编码，bot 侧可解码。"""
    global _MOCK_SILK_CACHE
    if _MOCK_SILK_CACHE is not None:
        return _MOCK_SILK_CACHE
    import io
    import math
    import struct
    import wave

    rate = 16000
    frames = io.BytesIO()
    w = wave.open(frames, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(struct.pack("<%dh" % int(rate * 0.5), *[int(6000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(rate * 0.5))]))
    w.close()
    wav_bytes = frames.getvalue()

    import tempfile

    from pilk import SilkEncoder

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav, tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as tmp_silk:
        tmp_wav.write(wav_bytes)
        wav_path, silk_path = tmp_wav.name, tmp_silk.name
    try:
        encoder = SilkEncoder(pcm_rate=16000, silk_rate=16000, max_rate=16000)
        encoder.encode(wav_path, silk_path)
        with open(silk_path, "rb") as f:
            _MOCK_SILK_CACHE = f.read()
    finally:
        for path in (wav_path, silk_path):
            try:
                os.unlink(path)
            except Exception:
                pass
    return _MOCK_SILK_CACHE


@app.get("/media/test.png")
async def serve_test_png() -> "Response":
    from fastapi.responses import Response

    STATE["media_files_served"] = int(STATE.get("media_files_served", 0)) + 1
    return Response(_MOCK_PNG, media_type="image/png", headers={"content-disposition": 'inline; filename="test.png"'})


@app.get("/media/test.silk")
async def serve_test_silk() -> "Response":
    from fastapi.responses import Response

    STATE["media_files_served"] = int(STATE.get("media_files_served", 0)) + 1
    return Response(_mock_silk(), media_type="audio/silk", headers={"content-disposition": 'inline; filename="test.silk"'})


@app.get("/media/wav_variant.bin")
async def serve_wav_variant() -> "Response":
    """模拟平台转好的 voice_wav_url（WAV 字节），单独计数以验证下载优先级。"""
    from fastapi import Response as _Resp

    STATE["wav_variant_served"] = int(STATE.get("wav_variant_served", 0)) + 1
    return _Resp(b"RIFF-fake-wav-bytes", media_type="audio/wav", headers={"content-disposition": 'inline; filename="wav_variant.bin"'})


# ============================================================== mock LLM ====
@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """模拟 OpenAI 兼容的 /models：用于验证「获取模型列表」功能。"""
    STATE["model_calls"] = int(STATE.get("model_calls", 0)) + 1
    ids = [
        "mock-model",
        "mock-model-mini",
        "mock-model-pro",
        "text-embedding-3-small",  # 非对话模型，应被前端过滤掉
        "whisper-1",               # 非对话模型，应被前端过滤掉
    ]
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "owned_by": "mock"} for model_id in ids
            ],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    STATE["llm_requests"].append(payload)
    STATE["llm_last_model"] = str(payload.get("model") or "")
    STATE["llm_last_last_user"] = ""
    reply = str(STATE["reply_text"])
    # 主动消息的提示词里带「本次任务 / 主动」，据此返回不同内容，
    # 便于自检区分「被动回复」与「主动消息」两条链路。
    system = ""
    messages = payload.get("messages") or []
    if messages and str(messages[0].get("role")) == "system":
        system = str(messages[0].get("content") or "")
    # 记录最后一条 user 消息的文本（多模态时取 text 部分），供断言"转写进了上下文"
    _last_user_text = ""
    for _message in reversed(messages):
        if _message.get("role") != "user":
            continue
        _content = _message.get("content")
        if isinstance(_content, str):
            _last_user_text = _content
        elif isinstance(_content, list):
            _last_user_text = " ".join(
                str(part.get("text") or "") for part in _content if isinstance(part, dict)
            )
        break
    STATE["last_llm_user_text"] = _last_user_text
    STATE["llm_last_last_user"] = _last_user_text[:200]
    STATE["llm_last_messages"] = messages
    if "主动" in system and "本次任务" in system:
        reply = str(STATE.get("proactive_text") or "这是 mock 主动消息。")
    # 多模态：请求里带 image_url 时走视觉回复（V0.2）
    # 同时记录图片挂在第几条 user 消息上（应为最后一条，即当前消息）
    has_image = False
    image_in_last_user = False
    user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    has_image = True
    if has_image and user_indices:
        image_idx = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance((messages[i].get("content") or ""), list)
                and any(isinstance(p, dict) and p.get("type") == "image_url" for p in messages[i]["content"])
            ),
            -1,
        )
        image_in_last_user = image_idx == user_indices[-1]
    if has_image:
        STATE["vision_calls"] = int(STATE.get("vision_calls", 0)) + 1
        STATE["vision_image_in_last_user"] = bool(image_in_last_user)
        reply = str(STATE.get("vision_text") or "我看你发的图了（mock 视觉回复）。")
    return JSONResponse(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        }
    )


# ====================================================== V0.2 语音 / 生图 mock ==
# 最小可播放 MP3（ID3 + 一个 MP3 frame 头）：bot 只负责透传上传，不校验解码
_MOCK_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 417


@app.post("/v1/audio/speech")
async def audio_speech(request: Request) -> "Response":
    from fastapi.responses import Response

    STATE["tts_calls"] = int(STATE.get("tts_calls", 0)) + 1
    return Response(_MOCK_MP3, media_type="audio/mpeg")


# 百炼（DashScope）原生 TTS mock：Qwen3-TTS / CosyVoice 的返回是 JSON（output.audio），
# 不是 OpenAI 兼容的裸音频流。model 以 urltest 开头时走 audio.url 分支，其余走 Base64 data 分支。
@app.post("/api/v1/services/aigc/multimodal-generation/generation")
@app.post("/api/v1/services/audio/tts/SpeechSynthesizer")
async def dashscope_tts(request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    import base64

    STATE["tts_dashscope_calls"] = int(STATE.get("tts_dashscope_calls", 0)) + 1
    STATE["tts_dashscope_last_path"] = request.url.path
    model = str(payload.get("model") or "")
    if model.startswith("urltest"):
        audio = {"url": "%s/v1/mock-dashscope-audio" % request.base_url.rstrip("/"), "data": ""}
    else:
        audio = {"url": "", "data": base64.b64encode(_MOCK_MP3).decode("ascii")}
    return JSONResponse(
        {
            "status_code": 200,
            "request_id": "mock-dashscope",
            "output": {"audio": audio},
            "usage": {"characters": 10},
        }
    )


@app.get("/v1/mock-dashscope-audio")
async def mock_dashscope_audio() -> "Response":
    from fastapi.responses import Response

    return Response(_MOCK_MP3, media_type="audio/wav")


@app.post("/v1/images/generations")
async def images_generations(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    import base64

    STATE["image_gen_calls"] = int(STATE.get("image_gen_calls", 0)) + 1
    STATE["image_last_prompt"] = json.dumps((payload.get("messages") or [{}])[-1].get("content", "") if payload.get("messages") else payload.get("prompt") or "", ensure_ascii=False)[:200]
    return JSONResponse(
        {
            "created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(_MOCK_PNG).decode("ascii"), "revised_prompt": "mock image"}],
        }
    )


# ============================================================== 进程封装 ====
class MockProcess:
    """以独立进程启动 mock 官方平台 + mock LLM。

    独立进程更贴近真实部署（官方平台本来就在远端），同时也避免在测试主线程里
    混用 asyncio 事件循环带来的 Windows IOCP 问题。

    Windows 下子进程放进带 KILL_ON_JOB_CLOSE 的 Job Object：测试进程无论正常退出、
    异常还是 Qt 析构 abort（0xC0000409），Job 句柄随进程关闭都会自动杀掉 mock，
    不会留孤儿进程（历史上每次崩溃退出泄漏一个，积累几十个）。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3100):
        self.host = host
        self.port = port
        self.process = None
        self.output: List[str] = []
        self._client = None
        self._job_handle = None

    @property
    def base_url(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    @property
    def client(self):
        """复用连接的控制通道客户端（避免大量短连接造成 TIME_WAIT）。"""
        import httpx

        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=10.0)
        return self._client

    def start(self, timeout: float = 25.0) -> "MockProcess":
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.mock_servers", "--host", self.host, "--port", str(self.port)],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self._attach_kill_on_close_job()

        def _reader() -> None:
            try:
                for line in self.process.stdout:  # type: ignore[union-attr]
                    self.output.append(line.rstrip())
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                time.sleep(0.5)
                if self.process.poll() is not None:
                    raise RuntimeError(
                        "mock 服务进程已退出：%s" % "\n".join(self.output[-10:])
                    )
            if _http_ready(self.host, self.port):
                return self
            time.sleep(0.2)
        raise RuntimeError("mock 服务进程启动超时：%s" % "\n".join(self.output[-10:]))

    def _attach_kill_on_close_job(self) -> None:
        """Windows：把 mock 子进程放进 KILL_ON_JOB_CLOSE 的 Job（失败不致命）。"""
        if os.name != "nt" or self.process is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", ctypes.c_byte * 48),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            if not kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(job)
                return
            if not kernel32.AssignProcessToJobObject(job, self.process._handle):
                kernel32.CloseHandle(job)
                return
            self._job_handle = job
        except Exception:
            self._job_handle = None

    def _release_job(self) -> None:
        # 必须在子进程确认退出后再关闭 Job 句柄，否则 KILL_ON_JOB_CLOSE 会误杀
        try:
            if self._job_handle is not None:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._job_handle)
                self._job_handle = None
        except Exception:
            self._job_handle = None

    def state(self) -> Dict[str, Any]:
        response = self.client.get("/__control/state")
        response.raise_for_status()
        return response.json()

    def reset(
        self,
        reply_text: Optional[str] = None,
        proactive_text: Optional[str] = None,
        vision_text: Optional[str] = None,
    ) -> None:
        self.client.post(
            "/__control/reset",
            json={
                "reply_text": reply_text,
                "proactive_text": proactive_text,
                "vision_text": vision_text,
            },
        )

    def official_sent(self) -> List[Dict[str, Any]]:
        """官方接口发出的消息（c2c / group）。"""
        return list(self.state().get("official_sent") or [])

    def media_uploads(self) -> List[Dict[str, Any]]:
        """官方 /files 上传记录（base64 直传或分片合并）。"""
        return list(self.state().get("media_uploads") or [])

    def attachment_url(self, filename: str) -> str:
        """测试附件（test.png / test.silk）的可直接 GET 的 URL。"""
        return "%s/media/%s" % (self.base_url, filename)

    def emit_c2c(self, content: str, openid: str = DEFAULT_OPENID, **extra: Any) -> Dict[str, Any]:
        """向机器人推送一条单聊消息。"""
        payload = {"content": content, "openid": openid}
        payload.update(extra)
        response = self.client.post("/__control/emit_c2c", json=payload)
        response.raise_for_status()
        return response.json()

    def emit_group(
        self,
        content: str,
        group_openid: str = DEFAULT_GROUP_OPENID,
        member_openid: str = DEFAULT_MEMBER_OPENID,
        **extra: Any
    ) -> Dict[str, Any]:
        """向机器人推送一条群聊 @ 消息。"""
        payload = {
            "content": content,
            "group_openid": group_openid,
            "member_openid": member_openid,
        }
        payload.update(extra)
        response = self.client.post("/__control/emit_group", json=payload)
        response.raise_for_status()
        return response.json()

    def emit_friend_add(self, openid: str = DEFAULT_OPENID) -> Dict[str, Any]:
        """向机器人推送「用户添加机器人」事件。"""
        response = self.client.post("/__control/emit_friend_add", json={"openid": openid})
        response.raise_for_status()
        return response.json()

    def drop_gateway(self) -> Dict[str, Any]:
        """掐断官方网关连接，用于验证断线重连。"""
        response = self.client.post("/__control/drop_gateway")
        response.raise_for_status()
        return response.json()

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except Exception:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except Exception:
                    pass
        self._release_job()


def _http_ready(host: str, port: int, timeout: float = 2.0) -> bool:
    """就绪探测：发一个真实的 HTTP 请求。

    注意不要用「TCP 连接后立刻关闭」的方式探测：在 Windows 上，如果客户端在服务端
    完成 AcceptEx 之前就中断连接，会让 asyncio proactor 收到 WinError 64，
    而 CPython 的处理是「记录错误并 close() 监听套接字」——监听会因此彻底失效。
    """
    import httpx

    try:
        response = httpx.get("http://%s:%d/__control/state" % (host, port), timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 mock QQ 官方机器人平台 + mock LLM 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3100)
    args = parser.parse_args()

    import uvicorn

    base = "http://%s:%d" % (args.host, args.port)
    print("mock 服务已启动：%s" % base)
    print("  · 官方平台 REST : POST %s/app/getAppAccessToken、/v2/users/{openid}/messages" % base)
    print("  · 官方平台 网关 : WS   %s/official-ws" % base.replace("http://", "ws://"))
    print("  · 事件注入      : POST %s/__control/emit_c2c 等" % base)
    print("  · LLM 兼容接口  : POST %s/v1/chat/completions" % base)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
