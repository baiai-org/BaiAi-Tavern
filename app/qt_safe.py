"""把数据整理成 Qt 能安全承载的形式。

为什么需要这个
--------------
PySide6 把 ``Signal(dict)`` 的字典打包成 ``QVariantMap``，其中的整数必须落在
C ``int64`` 范围内；而 QQ 开放平台返回的机器人 ID 是 **20 位数字**
（例如 ``1020000000000000000``），转成 Python int 后就超出 int64 —— 于是每次状态
快照 emit（GUI 每 3 秒一次）都会抛一次 ``OverflowError: int too big to convert``，
堆栈里还没有 Python 帧，很难定位。

这里在 emit 之前把越界的整数转成字符串（它们本来就是标识符，界面上也是按文本显示），
非有限浮点同样处理，避免 ``nan``/``inf`` 流进界面。
"""

from __future__ import annotations

import math
from typing import Any

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

__all__ = ["INT64_MAX", "INT64_MIN", "qt_safe"]


def qt_safe(value: Any) -> Any:
    """递归把 ``value`` 转成 Qt 安全的形式（dict/list 原样保留结构）。"""
    if value is None or isinstance(value, (str, bytes, bool)):
        return value
    if isinstance(value, int):
        return value if INT64_MIN <= value <= INT64_MAX else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {(item if isinstance(item, str) else str(item)): qt_safe(sub) for item, sub in value.items()}
    if isinstance(value, (list, tuple)):
        return [qt_safe(item) for item in value]
    if isinstance(value, set):
        return [qt_safe(item) for item in value]
    return value
