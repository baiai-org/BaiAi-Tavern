"""Bot 进程入口：FastAPI + APScheduler（QQ 官方机器人通道）。

启动方式（开发）::

    python -m bot.main

打包后为 ``bot.exe``，由 GUI 通过 subprocess 启动。

进程职责
--------
* 用 FastAPI 暴露本机控制接口（``/api/*``）与事件推送（``/ws/events``），GUI 只通过它读写；
* 为每个「官方机器人」建立一条到 QQ 开放平台网关的长连接（WebSocket）；
* 跑主动消息调度器（定时 / 空闲 / 随机）。

只有一种 QQ 接入方式——**QQ 官方机器人**（AppID + AppSecret → access_token → 网关）。
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Any, List, Optional

from common.logging_setup import get_logger, setup_logging
from common.paths import ensure_dirs

from . import __version__
from .config import bot_config
from .runtime import Runtime

log = get_logger("bot.main")


def build_app(runtime: Runtime) -> Any:
    """构建控制接口应用（FastAPI）。"""
    from fastapi import FastAPI

    from . import api

    config = runtime.config
    api.set_runtime(runtime)

    application = FastAPI(
        title="BaiAi-Tavern Bot",
        description="QQ 官方机器人接入 + 角色对话 + 主动消息调度",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(api.router)
    api.register_websocket(application)

    @application.on_event("startup")
    async def _on_startup() -> None:  # pragma: no cover - 启动路径
        await runtime.setup()
        api.set_runtime(runtime)

        # 角色池为空时自动导入内置角色，保证首次运行就有角色可用
        if bool(config.get("characters.import_builtin", True)):
            try:
                outcome = await runtime.registry.ensure_builtin()  # type: ignore[union-attr]
                if outcome:
                    runtime.publish({"type": "characters_changed"})
            except Exception as exc:
                log.warning("导入内置角色失败：%s", exc)

        if bool(config.get("proactive.enabled", True)):
            runtime.scheduler.start()

        # 每个官方机器人一条网关长连接
        started = await runtime.start_gateway()
        if not started:
            log.warning(
                "官方机器人网关未启动：请在「系统设置 → QQ」或「机器人」页面填写 AppID / AppSecret"
            )

        log.info("=" * 72)
        log.info("BaiAi-Tavern Bot v%s 已启动", __version__)
        log.info("机器人数量   : %d", len(runtime.bots))
        for bot in runtime.bots:
            log.info(
                "  · %-10s %s%s%s",
                bot.name,
                bot.mode_label,
                "（已停用）" if not bot.enabled else "",
                ("，绑定角色 %s" % bot.character_name) if bot.character_name else "，未绑定角色（按上次/随机选择）",
            )
        log.info("控制接口     : http://%s:%d/api/status", runtime.host, runtime.port)
        for bot in runtime.bots:
            log.info(
                "官方机器人   : 「%s」AppID=%s（网关推送，无需上报地址）",
                bot.name,
                str(bot.spec.official("app_id", "") or "未配置"),
            )
        log.info("状态推送     : ws://%s:%d/ws/events", runtime.host, runtime.port)
        log.info("日志文件     : %s", (runtime.log_path or ""))
        log.info("=" * 72)

    @application.on_event("shutdown")
    async def _on_shutdown() -> None:  # pragma: no cover - 关闭路径
        log.info("Bot 进程正在退出…")
        await runtime.teardown()

    return application


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BaiAi-Tavern Bot 进程")
    parser.add_argument("--host", help="控制接口监听地址（覆盖配置文件）")
    parser.add_argument("--port", type=int, help="控制接口端口（覆盖配置文件）")
    parser.add_argument("--no-console", action="store_true", help="不输出日志到控制台")
    args = parser.parse_args(argv)

    ensure_dirs()
    config = bot_config()
    config.ensure_file()
    config.load(force=True)

    setup_logging(
        name="bot",
        level=str(config.get("logging.level", "INFO") or "INFO"),
        console=(not args.no_console) and bool(config.get("logging.console", True)),
        max_bytes=int(config.get("logging.max_bytes", 2097152) or 2097152),
        backup_count=int(config.get("logging.backup_count", 3) or 3),
    )
    log.info("BaiAi-Tavern Bot v%s 启动中…（Python %s）", __version__, sys.version.split()[0])

    # 命令行覆盖写回内存配置（不落盘）
    if args.host:
        config.set("api.host", args.host)
    if args.port:
        config.set("api.port", int(args.port))

    from . import api

    runtime = Runtime(config)
    api.set_shutdown_handler(lambda: signal.raise_signal(signal.SIGINT))
    application = build_app(runtime)

    import uvicorn

    try:
        # log_config=None：不使用 uvicorn 自带的日志配置，日志统一走本项目的
        # 文件/控制台处理器（否则 bot.log 里会丢掉 uvicorn 的启动与报错信息）
        # timeout_graceful_shutdown：关闭时若仍有后台任务未结束（如 WS 处理器泄漏），
        # 限时强制取消，避免进程卡死在「Waiting for background tasks to complete」
        uvicorn.run(
            application,
            host=runtime.host,
            port=runtime.port,
            log_config=None,
            timeout_graceful_shutdown=8,
            access_log=bool(config.get("logging.access_log", False)),
        )
    except KeyboardInterrupt:  # pragma: no cover
        log.info("收到中断信号，Bot 退出")
    finally:
        try:
            import asyncio

            loop = asyncio.new_event_loop()
            loop.run_until_complete(runtime.teardown())
            loop.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
