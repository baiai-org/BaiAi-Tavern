"""QQ 官方机器人（QQ 开放平台）接入。

只有这一种 QQ 接入方式：

* :mod:`bot.qq_official.client`    —— 凭证（AppID/AppSecret → access_token）与 REST 接口
* :mod:`bot.qq_official.gateway`   —— WebSocket 网关（收消息）
* :mod:`bot.qq_official.receiver`  —— 事件 → 统一入站消息
* :mod:`bot.qq_official.messaging` —— 发送层（拆段、markdown、openid 目标）
"""

from .client import OfficialQQClient, OfficialQQError  # noqa: F401
from .gateway import INTENT_GROUP_AND_C2C, OfficialGateway  # noqa: F401
from .messaging import MODE_LABELS, MODE_OFFICIAL, Messaging, Peer  # noqa: F401
from .receiver import OfficialReceiver  # noqa: F401

__all__ = [
    "INTENT_GROUP_AND_C2C",
    "MODE_LABELS",
    "MODE_OFFICIAL",
    "Messaging",
    "OfficialGateway",
    "OfficialQQClient",
    "OfficialQQError",
    "OfficialReceiver",
    "Peer",
]
