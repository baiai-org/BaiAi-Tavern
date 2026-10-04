"""测试与自检脚本（QQ 官方机器人通道）。

* ``tests/mock_servers.py`` —— 模拟 QQ 官方机器人开放平台（凭证 / REST / 网关）
  与 OpenAI 兼容的 mock LLM
* ``tests/card_factory.py`` —— 生成用于测试的 PNG / JSON / YAML 角色卡与官方平台事件
* ``tests/smoke_test.py``  —— 端到端自检（不依赖真实 QQ 与真实 LLM）
* ``tests/official_smoke.py`` —— 官方通道专项自检（错误码 / 重连 / 重启记忆等）
* ``tests/scheduler_live.py`` —— 定时触发的“真实到点”慢速自检
"""
