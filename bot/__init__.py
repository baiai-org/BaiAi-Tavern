"""BaiAi-Tavern Bot 进程（FastAPI + APScheduler）。

进程职责：

* 为每个「QQ 官方机器人」建立到 QQ 开放平台网关的 WebSocket 长连接，接收单聊/群聊消息；
* 调用 LLM 生成角色回复与主动消息；
* 用 APScheduler 调度定时 / 空闲 / 随机三种主动消息触发；
* 对外暴露本地 HTTP API（``/api/*``）与 WebSocket（``/ws/events``）供 GUI 使用。
"""

__version__ = "0.1"

__all__ = ["__version__"]
