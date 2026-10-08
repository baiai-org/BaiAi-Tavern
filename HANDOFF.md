# BaiAi-Tavern 开发交接说明

> 面向接手的开发者 / 本地模型。**先读第 0 节，再读第 5、7、10 节**（不变量、硬约束、历史坑）。
> 文档与代码同步演进：改动架构或约定时请一并更新本文件。

---

## 0. 30 秒速览

| 问题 | 答案 |
|---|---|
| 这是什么 | Windows 桌面应用：让多个 AI 角色通过 **QQ 官方机器人** 主动给你发消息、并回复你的消息 |
| 运行形态 | 两个进程：**GUI 进程**（PySide6，含托盘）+ **Bot 进程**（FastAPI + uvicorn + APScheduler），通过本机 HTTP/WS 通信 |
| 怎么跑 | `scripts\start.bat`（开发模式，自动建 `.venv`）；或 `python -m app.main` / `python -m bot.main` |
| 怎么验证 | `python -m tests.smoke_test` 等 8 套自检，开发环境 **733 项**（打包产物齐备时 **774 项**）；`python -m pyflakes app bot common installer scripts tests` 必须干净 |
| 关键硬约束 | ① 代码保持 **Python 3.9 兼容** ② 自检必须全绿 ③ 任何"外部数据 → Qt"的数值都要过 `app/qt_safe.py` ④ 富媒体失败必须降级回纯文字 |
| 当前版本 | V0.2.2 开发中（分支 `v0.2.2`，基于 `543adc6`）：8 项修复 + 对话分级管理 + 按月/按天检索 + 语音存档；**尚未发布**，`main` / tag / Release 仍是 V0.2.1 |
| 最大的坑 | 见第 10 节，尤其 **Qt `Signal(dict)` + 超 int64 整数**、**跑自检别带 `BAIAI_DATA_DIR` 环境变量**、**PowerShell 批量改源码** |

---

## 1. 项目定位与当前状态

**做什么**：导入 SillyTavern 角色卡（PNG / JSON / YAML）→ 绑定到 QQ 官方机器人 → 角色在你说话时用对应人格回复，并在**定时 / 空闲 / 随机**三种策略下主动找你说话。所有参数都在 `config.yaml`，全部能通过界面改。

**当前状态（V0.2，多模态）**

- 在 V0.1「文字陪伴」之上，V0.2 让角色能**看、能发图、能听语音、能回语音**：
  * **模型路由（不同能力走不同 API）**：新增「模型路由」页面，三个能力槽位独立配置
    Base URL / API Key / 模型——图像理解 / 图像生成 / 文字转语音。
    每个槽位带**服务商预设**（`app/provider_presets.py`，选中自动填 Base URL + 候选模型；
    主流服务商全覆盖：主模型 34 家见 `app/llm_presets.py`，看图 13 / 生图 14 /
    文字转语音 7 家）与
    **「获取模型列表」**（从上游 `/models` 拉真实清单，点选即填；401/403 时自动换 `?key=` 重试），
    交互同「系统设置 → LLM」；
    **「测试线路」按表单当前值测试**（`/api/providers/test` 带 `values`，未保存也能测）。
    **主模型（文字对话）统一在「系统设置 → LLM」段配置**，模型路由页不重复
    （`load_slot("chat")` 以 `llm` 段为准，仅 llm 段未配置时回退 `providers.chat` 兼容老配置）。
    **语音转文字不占槽位**：QQ 官方平台随语音消息事件直接推送参考转写
    （`MessageAttachment.asr_refer_text`），零配置、不下载音频；平台没给时提示"角色暂时没听清"。
    （V0.2 开发中曾做独立的 asr 槽位——本地 SenseVoice / 远程 `/audio/transcriptions`，
    后确认真机零配置可听后整体移除；旧配置 `providers.asr` 段启动时自动清理。）
    **图像生成有两种引擎**：`openai`（OpenAI 兼容 `/images/generations`，对参数挑剔的端点
    自动降级参数重试：400 → 去掉 `size` → 再去掉 `response_format`，仍不行再改走 chat 出图）
    或 `gemini-native`（Gemini 原生 `/models/{model}:generateContent`，支持全部 Gemini 图像模型，
    含新一代 nano banana 2 系列——兼容层只支持个别模型，详见第 10 节坑 29）。
  * **理解图片 + 发图**：QQ 发来的图片（单聊 / 群聊）经视觉线路理解后回复；
    回复或主动消息末尾写 `[IMG] 描述`（角色自己决定）即自动生图发过去。
  * **双向语音**：用户发语音，QQ 官方平台随消息推送的参考转写直接给角色听（零配置）；
    角色按概率把整条回复改成语音发回（TTS，三种引擎：edge-tts / OpenAI 兼容 /
    **阿里云百炼原生**——Qwen3-TTS / CosyVoice / Qwen-Audio 三族，端点按模型名自动路由、
    音色按族内置候选，详见第 10 节坑 37；
    选 `qwen3-tts-instruct-flash` 自动启用**指令风格**、选 `qwen-audio-3.1-tts-flash`
    自动启用**情感与拟声标签**：内置 SKILL（`bot/media/instruct.py`）让主模型按百炼
    官方格式/标签表结合角色人设与语境调教语音表现力，详见坑 38）。
  * **每个角色独立音色与头像**：角色管理列表的「音色」按钮（`CharacterVoiceDialog`）可为单个角色
    指定 `tts_voice` + 角色级音色调节（`tts_rate` / `tts_pitch` / `tts_volume` / `tts_speed`，
    覆盖「模型路由」的全局调节，留空跟随全局，合成见 `MediaHub._tts_style(character)`）；
    试听走 `/api/providers/test`（带 `values` 把当前调节叠上去）。点角色头像可自选图片替换
    （`PUT /api/characters/{id}/avatar` → `registry.update_avatar`，文件存 `data/characters/avatars/`）；
    导入角色卡自带人物图像时（PNG 卡整图 / JSON-YAML 的 base64 `avatar` 字段）自动用作头像。
  * **富媒体行为可调**：语音回复概率 / 单条语音字数上限 / 临时文件保留天数，都在「模型路由」页，保存即热生效。
  * **自动更新（V0.2）**：启动后后台检查 GitHub Releases 是否有新版（6 小时限流），
    发现新版弹轻量提醒，四出口：**立即更新 / 跳过此版本 / 不再提示 / 稍后再说**。
    更新 = 下载 Release 里的安装包（`%TEMP%\baiai-update\`，进度条）→ 有 `SHA256SUMS.txt`
    时校验 → 静默装到当前安装目录（`installer.common` 会自动关掉旧版本进程）→ 自动启动新版。
    安装 / 更新 / 卸载 / 关于合一界面：主窗口左下角「安装与更新」（`app/lifecycle.py`），
    逻辑在 `app/updater.py`（不依赖 Qt，自检直接调用）。
    策略存 `config.yaml` 的 `app.update_check_enabled` / `app.update_skipped_version` /
    `app.update_last_check`。**发布新版时 Release 附件命名必须保持 `BaiAi-Tavern*.exe`
    （+ `SHA256SUMS.txt`），自动更新靠这个规则挑安装包。**
- 架构上：Bot 侧新增 `bot/media/` 包（`store` 落地 / `voice` TTS / `images` 生图+理解 / `hub` 收发编排），
  `runtime.media` 持有 `MediaHub`；官方通道 `client.py` 增加富媒体上传（`msg_type=7` + `file_info`）与分片上传。
  数据库升到 `SCHEMA_VERSION=6`（`characters.tts_voice` + 角色级音色调节 `tts_rate/tts_pitch/tts_volume/tts_speed`、
  `messages.kind` / `messages.media_path`，旧库自动 `ALTER`）。
  **V0.2.2**：数据库升到 `SCHEMA_VERSION=7`（`messages.summarized` + `memory_summaries` 表）；
  新增 `bot/memory/summarizer.py`（后台压缩器：7 天前对话分批压缩成摘要，每 60 秒一轮）、
  `app/parallel_download.py`（多线程分段下载，最多 16 段）；主动消息调度改为**每机器人独立**
  任务（`bot/scheduler/proactive.py` 的 `_install_jobs` 按 `enabled_bots()` 展开）；
  对话页（`app/pages/conversations.py`）加月/日筛选 + 关键词搜索 + 图片查看/语音播放
  （QMediaPlayer，silk 走系统播放器）。
- **降级不变量**：任何媒体能力失败（未配置线路 / 调用出错 / 上传失败）都必须优雅退回纯文字，绝不把聊天打断；
  `send_outgoing` 里若文字 + 媒体都没发出去，会把正文再按纯文字发一遍保底。
- 接入方式**只有 QQ 官方机器人**（AppID / AppSecret）。历史上支持过 NapCat / OneBot，**已完整移除**：相关代码、界面、下载器、安装包内容都删了，老配置里的遗留键会在 `ConfigManager.load()` 时自动清理并写出 `config.yaml.bak`。
- 多机器人 + 多角色：每个机器人一套官方凭据，绑定一个角色，互不串台。
- 安装包：**单个 EXE**（安装 / 重新安装 / 卸载合一），按用户安装到 `%LOCALAPPDATA%\Programs\BaiAi-Tavern`，不需要管理员权限；卸载默认保留 `data/`（配置 + 聊天记录）。
- 已开源公开：<https://github.com/baiai-org/BaiAi-Tavern>（Apache-2.0）。`main` 已是 V0.2（tag `v0.2`），Releases 已有 v0.1 历史版本，V0.2 的 Release 资产（安装包 + SHA256SUMS.txt）上传后自动更新即可生效。

---

## 2. 仓库与发布信息

| 项目 | 值 |
|---|---|
| 仓库 | <https://github.com/baiai-org/BaiAi-Tavern>（public，默认分支 `main`） |
| 协议 | Apache-2.0（`LICENSE`，第三方清单见 `NOTICE`） |
| CI | `.github/workflows/ci.yml`（Windows runner：pyflakes + 6 套自检；`workflow_dispatch` 时额外打包并跑 frozen 自检） |
| 发布 | `main` = V0.2.1（tag `v0.2.1`）；线上 Releases：`v0.2.1`（当前）/ `v0.2` / `v0.1`（历史），均含安装包 + `SHA256SUMS.txt`。V0.2.2 待发布（分支 `v0.2.2`） |
| 产物 | `dist\BaiAi-Tavern V0.2.2.exe`（安装包，2026-10-08 重打包含 3 项新修复）、无空格发布副本 `dist\BaiAi-Tavern-V0.2.2.exe` + `dist\SHA256SUMS.txt`、`dist\BaiAi-Tavern\`（绿色版）、`dist\BaiAi-Tavern.exe`、`dist\bot.exe` |
| 图标 | 全部由 `scripts/make_icons.py` + `app/uikit.py` 绘制，`resources/icons/*.ico` 是产物 |

**发新版本流程**

```bat
:: 1) 改版本号：scripts\make_version_info.py 里的 3 个 target（gui / bot / installer）
:: 2) 打包
scripts\build.bat            :: GUI + Bot（含版本资源）
scripts\build_installer.bat  :: 组装 build\payload → dist\BaiAi-Tavern V0.x.exe（带自动重试）
:: 3) 全量自检（见第 9 节），必须全绿
:: 4) 提交 + 打标签 + 建 Release，把 dist\BaiAi-Tavern V0.x.exe 作为附件上传
```

> Release 附件名**不要带空格**：GitHub 会把空格替换成点（`BaiAi-Tavern V0.2.exe` → `BaiAi-Tavern.V0.2.exe`），
> 所以对外统一用 `BaiAi-Tavern-V0.2.exe`，本地 `dist\` 里的文件名可保持带空格。
> 上传 Release 需要 token 具备 `Contents: write`（classic token 需 `repo` + `workflow`）；
> **不要把 token 写进任何文件**，用完立即 Revoke。
>
> **自动更新依赖 Release 附件**：客户端（`app/updater.py`）按「`BaiAi-Tavern` 开头 + `.exe` 结尾 +
> 不是主程序名 + 优先带版本号」挑安装包，并会下载 `SHA256SUMS.txt` 做 SHA256 校验。
> 发版时**必须**同时上传安装包与 `SHA256SUMS.txt`，命名保持 `BaiAi-Tavern-V0.x.exe`，
> 否则老版本用户「立即更新」会提示找不到安装包（不致命，可手动下载）。

---

## 3. 运行架构

```
┌──────────────────────── GUI 进程（app/）────────────────────────┐
│ app/main.py          单实例锁、日志、主题、异常钩子、--uninstall  │
│ app/main_window.py   主窗口 + 侧边栏 + 8 个页面 + 托盘            │
│ app/context.py       AppContext：配置、API 客户端、Bot 进程管理、  │
│                      3 秒状态轮询（Poller）、事件 WS 客户端        │
│ app/bot_process.py   以子进程方式拉起/结束 bot.exe（或 bot.main）  │
└───────────────┬───────────────────────────────┬─────────────────┘
        HTTP(127.0.0.1:8765)              WS /ws/events
                │                               │
┌───────────────▼───────────────────────────────▼─────────────────┐
│ Bot 进程（bot/）：FastAPI + uvicorn + APScheduler                │
│ bot/main.py      build_app() + uvicorn.run(log_config=None)      │
│ bot/api.py       REST 路由（见下）                                │
│ bot/runtime.py   Runtime：多机器人、配置热重载、事件总线、快照      │
│ bot/qq_official/ 官方通道：client(REST) gateway(WS) messaging     │
│                  receiver(事件分发)                                │
│ bot/chat_router.py  收到消息 → 选角色 → 提示词 → LLM → 分段回复    │
│ bot/scheduler/    proactive(主动消息) + triggers(定时/空闲/随机)   │
│ bot/media/        富媒体中枢 store/voice/images/hub(runtime.media)   │
│ bot/ai_engine/    engine(编排) + prompt_builder(提示词) + llm_client│
│ bot/memory/       短期(上下文) + 长期(记忆/向量化前的加权条目)      │
│ bot/database/     aiosqlite：models(建表) + crud                  │
│ bot/character_manager/  loader(角色卡解析) + registry(注册表)      │
└──────────────────────────────────────────────────────────────────┘
```

**数据流（关键三条）**

1. **状态**：GUI 每 3 秒 `GET /api/status` → `Context._on_status()` → `qt_safe(snapshot)` → `status_updated` 信号 → 主窗口 + 每个页面。
2. **事件**：Bot 的 `Runtime.publish()` → WS `/ws/events` → GUI `EventStream` 线程 → `event_received` → 主窗口（托盘通知）+ 页面刷新。
3. **配置**：GUI 写 `data/config.yaml`（`ConfigManager`，带 `revision` 自增）→ `POST /api/config/reload` 或页面保存时触发 → Bot 的 `Runtime.sync_config()` 比对 `config.revision` 与 `_applied_revision` 后才应用（**注意：读接口也必须触发同步，否则会"饥饿"**）。

**Bot 侧 REST 路由（`bot/api.py`，前缀 `/api`）**

```
GET  /health  /status  /stats/today  /logs  /config  /proactive/status  /proactive/logs
POST /bot/start  /bot/stop  /bot/restart  /shutdown  /config/reload  /llm/test
GET/POST/PUT/DELETE /bots  /bots/{id}  /bots/{id}/test  /bots/{id}/reconnect  /bots/{id}/forget-openid
GET  /qq/status      POST /qq/test  /qq/reconnect  /qq/forget-openid
GET/POST/PUT/DELETE /characters  /characters/{id}  /characters/import  /characters/import-builtin
                    /characters/import-path  /characters/scan  /characters/{id}/avatar
GET  /conversations  /conversations/{id}/messages  /conversations/{id}/memories
POST /conversations/{id}/memories   DELETE /memories/{memory_id}
POST /proactive/trigger
GET  /providers            POST /providers/test（V0.2 模型路由：槽位状态 / 单槽位测试）
GET  /media/voices  /media/file  /media/inbox（V0.2 富媒体：音色 / 本地媒体 / 收件箱）
```

鉴权：仅当 `api.token` 非空时校验请求头 `X-Tavern-Token`（或 `?token=`）。**只监听 `127.0.0.1`**；
若把 `api.host` 改成 `0.0.0.0`，必须同时设置 `api.token`（见 `SECURITY.md`）。

---

## 4. 目录与文件地图（改哪里去哪个文件）

| 目录 | 内容 | 常见改动入口 |
|---|---|---|
| `app/` | GUI 进程（34 个文件，含包初始化） | 界面/交互 |
| `app/pages/*.py` | 8 个页面：仪表盘 / 机器人 / 角色管理 / **模型路由(V0.2)** / 主动消息 / 对话查看 / 系统设置 / 日志 | 加功能优先看 `pages/base.py`（`Page` 基类：`build/refresh/on_status/on_event/reload_if_loaded`） |
| `app/widgets/*.py` | 可复用控件：`qq_form`（官方凭据表单）、`llm_form`、`character_card`、`fields`、`status_indicator`、**`provider_form`(V0.2 槽位表单，含服务商预设/获取模型列表/测试线路/ASR 下载进度条)** | 表单字段 |
| `app/llm_presets.py` / `app/provider_presets.py` | LLM 服务商预设（主模型，**34 家**：国内直连 / 国际 / 聚合平台 / 自建网关 / 本地）/ 模型路由各槽位服务商预设（**看图 13 / 生图 14 / ASR 8 / TTS 7 家**，选中自动填 Base URL + 候选模型，可带 `engine` 字段联动引擎下拉） | 加服务商 |
| `app/onboarding.py` | 6 步配置引导（步骤编号连续，`EXPECTED_HEADS` 与自检绑定） | 引导流程 |
| `app/uikit.py` / `app/icons.py` | 手绘图标/箭头/开关（**不用 emoji 字形**） | 任何视觉元素 |
| `app/qt_safe.py` | 把外部数据转成 Qt 安全形式（超 int64 整数 → 字符串） | **新增"外部数据进信号"时必须过这里** |
| `bot/` | Bot 进程（含包初始化） | 业务逻辑 |
| `bot/runtime.py` | 多机器人装配、配置热重载、事件总线、`snapshot()`；`self.media = MediaHub(self)` | 加新的状态字段 |
| `bot/qq_official/*.py` | 官方通道：`client`(REST/凭证/**富媒体上传**) `gateway`(WS/心跳/重连) `messaging`(发送/探测) `receiver`(事件分发) | 官方协议相关 |
| `bot/media/*.py` | **V0.2 富媒体**：`store`(data/media 落地) `voice`(TTS 客户端) `images`(生图/理解) `instruct`(TTS 指令风格 SKILL，主模型按百炼官方格式生成指令) `hub`(`MediaHub` 收发编排) | 看/发图/语音 |
| `bot/scheduler/proactive.py` | 主动消息调度（APScheduler 任务、配额、免打扰、`trigger_once`） | 触发策略 |
| `bot/ai_engine/*.py` | `engine`(编排，**带 vision 图像理解**) `prompt_builder`(提示词) `llm_client`(OpenAI 兼容) | 提示词/模型调用 |
| `common/` | 双进程共用：`config`(ConfigManager) `paths` `bots`(BotSpec) `text`(分段/清洗) `async_utils` `logging_setup` `utils` **`providers`(V0.2 槽位规格)** | 配置/路径 |
| `installer/common.py` | **安装/卸载全部逻辑**（复制、注册表、快捷方式、自删除、进程回收） | 安装行为 |
| `installer/installer_main.py` | 安装向导（安装 / 重装 / 卸载，`--silent` 等） | 向导 UI |
| `tests/` | 8 套自检 + mock（见第 9 节） | 加断言 |
| `scripts/` | `start.bat`(开发启动) `build.bat` `build_installer.bat` `make_icons.py` `make_version_info.py` `gui_entry.py` `bot_entry.py` | 打包 |
| `resources/` | 内置角色卡 3 张、图标 ico/png、`styles/dark.qss` | 资源 |

**配置系统要点**

- 结构（`common/config.py` 的 `DEFAULTS`）：`app` / `llm` / `qq` / `proactive` / `memory` / `database` / `characters` / `api` / `logging` / **`providers`(V0.2)** / **`media`(V0.2)**。
- **`providers`（V0.2 模型路由）**：槽位 `chat` / `vision` / `image` / `tts`，每个是
  `engine` / `base_url` / `api_key` / `model` /（tts 另有 `voice`）。
  **主模型统一读 `llm` 段**：`load_slot("chat")` 以 `llm` 段为准，仅当 `llm` 段未配置时才回退
  `providers.chat`（兼容早期在路由页填过主模型的老配置）——模型路由页面已不显示 chat 槽位，
  界面上主模型只在「系统设置 → LLM」改。
  **引擎取值**：`openai`（默认，vision/image 远程）/ `edge-tts`（仅 tts，在线免费无需 Key）/
  `dashscope`（仅 tts，阿里云百炼原生接口：Qwen3-TTS 与 CosyVoice，端点按模型名路由，
  Base URL 带不带 `/compatible-mode/v1` 都行）/
  `gemini-native`（仅 image，Gemini 原生接口）。
  `ProviderSpec.configured`：edge-tts 恒为 True；openai 需 base_url（本地 127.0.0.1 不强制 Key，远程必须有 Key）。
  语音转文字不占槽位（官方平台参考转写）；旧配置遗留的 `providers.asr` 段由 `strip_legacy_keys` 自动清理。
- **`media`（V0.2 富媒体行为）**：`enabled` / `voice_reply_probability`(0–1 语音回复概率) / `allow_image`(是否允许生图) /
  `image_marker`(默认 `[IMG]`) / `voice_max_chars`(单条语音字数上限，超长拆分) / `temp_days`(临时文件保留天数)。
- `qq` 下：`id name enabled character_id character_name user_nickname reply_enabled group_reply_enabled official{app_id,app_secret,...}`；**多机器人是 `qq` 下的列表式条目**（`raw_bot_entries()` / `common/bots.py`）。
- 值支持 `${ENV_VAR}` 展开。
- 路径覆盖（自检靠它隔离）：`BAIAI_HOME`、`BAIAI_DATA_DIR`（兼容旧名 `QQAI_HOME` / `QQAI_DATA_DIR`）；卸载注册表键可用 `BAIAI_UNINSTALL_KEY` 覆盖。
- 数据目录优先级：`BAIAI_DATA_DIR` → exe/项目目录下 `data/`（可写时）→ `%APPDATA%\BaiAi-Tavern\data`（旧目录 `QQ-AI-Tavern` 存在则沿用）。
- 富媒体临时文件落在 `data/media/inbox`（用户发来的）与 `data/media/outbox`（角色发出的），按 `temp_days` 定期清理。
- 遗留键清理：`LEGACY_QQ_KEYS` / `LEGACY_SECTIONS` / `LEGACY_APP_KEYS` + `strip_legacy_keys()`，会写回并留 `config.yaml.bak`。

---

## 5. 核心不变量（改动时不要破坏）

1. **只支持 QQ 官方机器人**：`common/bots.py` 里 `MODE_OFFICIAL` 是唯一模式，`normalize_mode()` 永远返回 official。不要重新引入 NapCat/OneBot。
2. **ID 一律按字符串处理**：官方平台的机器人 ID / openid 是 20 位数字或长串，**任何 ID 都不要转 int 后再送进 Qt / JS**。
3. **所有"外部数据 → Qt"的值都要清洗**：`app/qt_safe.qt_safe()` 在 `Context` emit 前统一处理；新加的信号如果是 `Signal(dict)` 要么改成 `Signal(object)`，要么保证值已清洗。
4. **页面级异常不能击穿界面**：`Page._status_guard()` 捕获所有异常并记日志（带页面名）。新页面直接继承 `Page` 即可获得这个保护。
5. **卸载必须先删注册表项**，再安排延迟删除自己（否则"应用列表里还留着"）。
6. **安装前必须结束目标目录里正在运行的进程**（`installer.common.stop_running_app()`），否则 `bot.exe: [Errno 13] Permission denied`。
7. **卸载默认保留 `data/` 与 `config.yaml`**，只有 `--remove-data` 才删。
8. **配置热重载靠 `revision` 比对**：读接口也要调用 `Runtime.sync_config()`，否则改配置后长时间不生效。
9. **`.bat` 必须 CRLF**，`.py/.md/.yml/.yaml/.qss/.spec` 必须 LF（`.gitattributes` 已固定）。
10. **中文用户可见文案 + `%` 格式化**；界面元素不要用 emoji/符号字形（系统缺字形会显示成方块），图标用 `app/uikit.py` 画。
11. **不要把真实凭据提交进仓库**：`data/`、`config.yaml`、`*.log`、`dist/`、`build/` 都在 `.gitignore` 里，别绕过它。
12. **富媒体必须优雅降级（V0.2）**：任何媒体能力失败（线路未配置 / 调用出错 / 下载或解码失败 / 上传失败）
    都要退回纯文字并记日志，绝不把聊天打断。`chat_router` 的 `inbound_media()` 异常 → 按纯文字继续；
    `hub.compose()` 异常 → 按纯文字继续；`send_outgoing()` 里若文字 + 媒体都没发出去，把 `out.body` 再按纯文字发一遍保底。
    语音回复时**整条**变语音（`send_text=False`），不要文字 + 语音各发一遍。
    语音理解零配置：直接用官方平台随消息推送的 `asr_refer_text` 参考转写（不下载音频），
    平台没给时记 errors（"角色暂时没听清"），按纯文字继续。
13. **`[IMG]` 生图约定（V0.2）**：角色回复或主动消息末尾单独一行 `[IMG] 描述`（`media.image_marker`），
    最多一个；命中即调图像生成线路发图。`parse_image_prompt()` 解析、`_truncate_keep_marker()` 保证截断不丢标记。
14. **主模型只认 `llm` 段（V0.2 整合）**：`load_slot("chat")` 以 `llm` 段为准（`providers.chat` 仅作旧配置兜底）；
    **不要在别处直接读 `llm` 段当主模型**——一律走 `common/providers.py::load_slot("chat")`。
15. **测试线路必须按「表单当前值」测试（V0.2）**：GUI「模型路由」的测试按钮把 `form.values()` 随
    `/api/providers/test` 一起提交（`bot/api.py::_SlotOverrideConfig` 把表单值叠在已保存配置上，
    空字符串字段按「没填」处理）。**不要改回只读已保存配置**——用户改完还没点「保存」就能测，
    否则 local 引擎这种「什么都没填」的线路永远报「这条线路还没填完整」。

---

## 6. 环境与常用命令

```bat
:: 环境：Windows 10/11 + Python 3.9.13 虚拟环境（代码保持 3.9 兼容，实际目标 3.10+）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 开发启动（GUI 会自动拉起 Bot 进程）
scripts\start.bat
:: 或分开跑
.venv\Scripts\python.exe -m app.main
.venv\Scripts\python.exe -m bot.main

:: 静态检查（必须 0 输出）
.venv\Scripts\python.exe -m pyflakes app bot common installer tests scripts

:: 打包
scripts\build.bat            :: dist\BaiAi-Tavern.exe + dist\bot.exe（带版本资源）
scripts\build_installer.bat  :: dist\BaiAi-Tavern V0.2.2.exe（组装 payload → 单文件安装包）
```

> ⚠️ PATH 上的 `python` 可能是 Windows Store 版 3.9，**永远用 `.venv\Scripts\python.exe`**。
> ⚠️ 跑自检时把输出重定向到文件再读（例如 `*> $env:TEMP\x.log`），不要用管道接 `Select-Object`。

---

## 7. 开发约定（硬约束）

- **Python 3.9 兼容**：`from __future__ import annotations`；不用 `X | Y` 类型写法；不用 3.10+ 的 API
  （例如 `Path.write_text(newline=...)` 是 3.10 才有的，3.9 会 `TypeError` —— 用 `path.open("w", newline="\n")`）。
- **没有 pytest**：所有自检都是"可执行的检查器"，通过 `python -m tests.<名字>` 运行，
  逐条打印 `[PASS]` / `[FAIL]`，末尾给"N 项通过，M 项失败"，失败则退出码非 0。
- **发现真实缺陷时先修产品、再补断言**；新增/修改功能要同步补断言。
- **字符串格式化用 `%`**；注释与用户可见文案用中文。
- **副作用必须可回收**：窗口/定时器/信号/外部进程都要能随页面销毁清理（页面基类已给出模式）。
- **日志**：GUI → `data/logs/gui.log`，Bot → `data/logs/bot.log`（滚动）；异常钩子会打印完整堆栈。
- **不要用 PowerShell here-string / `Replace` 批量改 Python 源码**（历史上多次把 `\n` 字面写进源码、把行拼坏）：
  改用 `write` 工具，或写一个临时 Python 脚本改完即删（见第 10 节）。

---

## 8. 关键实现参考（按需求找代码）

| 需求 | 从哪里下手 |
|---|---|
| 加一个配置项 | `common/config.py::DEFAULTS` → 对应页面 `values()` / `set_values()` → `config.example.yaml` → 自检 |
| 加一个状态字段（界面要显示） | `bot/runtime.py::snapshot()` → `app/pages/<页面>.py::on_status()` → 自检断言字段存在 |
| 加一个 Bot 接口 | `bot/api.py`（`APIRouter`）→ `app/api_client.py` 加方法 → 页面调用 |
| 改提示词 / 人设拼接 | `bot/ai_engine/prompt_builder.py`（+ `resources/styles/dark.qss` 无关） |
| 改主动消息策略 | `bot/scheduler/triggers.py`（判定）+ `bot/scheduler/proactive.py`（调度/配额）+ `app/pages/proactive.py`（界面） |
| 改官方协议 | `bot/qq_official/client.py`（REST/凭证）、`gateway.py`（WS/心跳/重连）、`messaging.py`（发送/探测） |
| 改角色卡解析 | `bot/character_manager/loader.py`（PNG 内嵌 JSON / JSON / YAML，含大小与字段限制） |
| 改安装/卸载行为 | `installer/common.py`（复制、注册表、快捷方式、延迟批处理自删除、进程回收） |
| 加自检断言 | 对应 `tests/*.py`；mock 能力见 `tests/mock_servers.py`、`tests/card_factory.py` |

---

## 9. 自检体系（8 套，开发环境 733 项 / 打包产物齐备 774 项）

| 命令 | 项数 | 覆盖 |
|---|---|---|
| `python -m tests.smoke_test` | 290 | 单元（含**模型槽位 / 主模型整合 / asr 槽位移除与旧配置清理 / Gemini 生图参数降级 / Gemini 原生接口 / chat modalities 大小写兜底 / chat 出图 content 数组格式兜底（端点要求 messages[].content 为内容数组时自动换格式）/ chat 出图多种返回形状兜底（顶层 data[]、非标准 b64 键、data URL 就地解码）/ vLLM-Omni（Qwen-Image）200 无图时按官方示例补 extra_body 重试 / 百炼兼容模式 images 404 时走原生协议 multimodal-generation（content 部件 image 键 + URL 下载）/ 局域网私网地址（10.x / 192.168 / 172.16-31 / .local）识别为本地不强制 Key / 回复链路局域网端点 Key 留空判定（与界面提示一致；SDK 空 Key 自动补占位）/ 推理模型空正文重试自动翻倍长度（上限 4096）/ 未配置提示按字段精确列缺失项 / 认不出图片数据时报错带响应体 / 图像理解内置红色测试图（测试线路独立于图像生成）/ 角色卡 Chub 风格（avatar 远程 URL 下载 + 图片魔数验证 / 非图片头像不留垃圾字节 / 非标准 extensions 不破坏解析 / PNG chara 的 URL-safe base64 与明文 JSON 兜底 / tEXt 块 UTF-8 容错 / 报告卡片本身未写的核心字段——Chub 卡常只写描述+开场白，其余字段空属卡片内容问题）/ 提示词卫生（Chub 整页 HTML 版 creator_notes 不进提示词、短纯文本保留 / 发送前清理 Markdown 图片链接）/ 测试线路表单值 / 服务商预设全覆盖 / 入站语音平台参考转写（零配置零下载）/ 角色级音色调节覆盖全局 / 头像上传与旧文件清理 / [IMG] 句中识别 / 生图风格按角色人设自动匹配（二次元关键词→动漫风、写实关键词→写实风、无特征不强加、全局开关覆盖）/ TTS 风格参数与试听文案池 / 默认语音概率 5%（V0.2.2 起）/ 视觉图片挂当前 user 消息与 MIME 按文件头识别 / 百炼 TTS 引擎（端点按模型路由 / Base URL 归一 / Base64 与 audio.url 两种返回 / 业务错误码透传 / 411 三族音色不混用提示 / qwen-audio 全量官方参数 rate/pitch/volume/format/sample_rate/language_hints/instruction 与 GUI 值映射）/ TTS 调教 SKILL（instruct 门控 / 官方格式提示词 / 指令解析 / 主模型生成与空内容重试 / instructions + optimize_instructions + language_type 请求体 / Qwen-Audio 标签门控、官方语义与官方示例提示词、标签逐句覆盖、标签+指令双输出解析、标签校验与近似拼写归一、编造中文标签剥除、本地兜底、思考类模型不限制思考长度 4096）/ 全量群消息 @ 判定单元（`<@AppID>` 占位 / `mentions[].id` / @ 别的机器人 / 无 @ / @ 事件恒真 / **V0.2.2：is_you 标记 / user_openid 字段匹配 / 多身份集合 / 状态化去重（先让路后到 @ 仍处理、已回复不重发、@ 先到去重后到全量）** / **按机器人生效段 effective_section（全局兜底 + 键级覆盖）**）/ TTS 缺省引擎 dashscope 且引擎列表首位**）+ 端到端（mock 官方平台与 mock LLM） |
| `python -m tests.official_smoke` | 82 | 官方通道：凭证 / 网关 / 单聊 / 群聊 / 主动消息 / 重连 / 错误码 + **V0.2 富媒体段（TTS 语音回复 / 视觉理解且图片挂当前 user 消息 / [IMG] 生图 / 语音参考转写进模型上下文且音频被下载存档（V0.2.2：优先 voice_wav_url））** + **全量群消息段（GROUP_MESSAGE_CREATE：@ 自己才回 / @ 别的机器人让路 / 普通消息按开关 / 同一 msg_id 重复推送只回一次 / V0.2.2 新版格式无 @ 占位 + is_you 仍回复 / 新版 @ 别的机器人让路 / 同 msg_id 全量先到让路后 @ 事件仍回复、@ 先到后全量重推不重复）** |
| `python -m tests.multibot_smoke` | 44 | 两个官方机器人 + 两个角色互不串台 + **多机器人群：@ 谁谁回答（同一条全量群消息推到两个平台，只有被 @ 的回复；普通群消息都回；V0.2.2 新版 is_you 格式同样 @ 谁谁回答）** |
| `python -m tests.onboarding_smoke` | 88 | 6 步配置引导（含步骤标题 `EXPECTED_HEADS`；含**汇总页隐藏「取消引导」/ LLM 密钥留空也能「完成」不 KeyError**） |
| `python -m tests.gui_smoke` | 159 | 界面集成（offscreen）：**八页**裁切体检、**全页面宽度守卫（逐页断言滚动内容宽度 ≤ 视口，防「长单行文本撑宽页面、右侧被裁」回归）**、官方表单、超大 ID 回归、**模型路由（预设/获取模型列表/获取模型列表联动刷新音色/测试线路按表单值/视觉测试线路校验内置红色测试图/测试完成后按钮保持可用防焦点串段/生图双引擎与 Gemini 原生预设自动切引擎/TTS 音色调节字段与输出格式/TTS 三引擎切换与百炼音色清单/全量音色清单加载/角色音色试听入口）**、**「消息设置」页（V0.2.2：富媒体行为从模型路由搬来；顶部设置范围下拉全局/按机器人；按机器人保存只写覆盖段）**、**机器人页生图风格按机器人设置（auto/anime/realistic/off）**、**对话页媒体显示（图片消息渲染缩略图 / 语音消息渲染播放徽标 + 转写文字 / 选中后按钮可用 / V0.2.2：真实鼠标点击缩略图即打开、点击徽标即播放）**、**角色音色对话框（回显 + 角色级调节输出）/ 角色卡音色按钮与可点击头像 / 角色编辑滚动区 / 编辑对话框 {{char}}/{{user}} 占位符说明 / HTML 版补充设定（Chub 展示页）不影响对话的说明**、**安装与更新一体窗口（区块/控件/新版检测/跳过版本/不再提示/启动提醒四出口，mock GitHub API）** |
| `python -m tests.scheduler_live` | 14 | 定时触发"真实到点"慢速自检 |
| `python -m tests.frozen_smoke` | 0 / 34 | **打包产物**（无产物时自动跳过 0 项；产物齐备时 34 项，`--force` 强制） |
| `python -m tests.installer_smoke` | 56 / 63 | **真实安装包**安装/卸载 + 主程序自卸载 + 旧版本运行时升级 + **更新系统（版本比较/附件挑选/SHA256/下载进度/启动限流/静默拉起，本地 HTTP 模拟 GitHub，无外网依赖）** + **V0.2.2 多线程分段下载（大文件 Range 分段与完整覆盖、plan_ranges 切分）**（无产物 56 项；有产物 63 项） |

- `tests/mock_servers.py`：mock LLM + mock QQ 官方平台（`/app/getAppAccessToken`、`/users/@me`、`/gateway`、
  `/v2/users/{openid}/messages`、`/v2/groups/{group_openid}/messages`、WS `/official-ws`、控制面 `/__control/*`）；
  **V0.2 新增富媒体 mock**：`POST /v2/users/{openid}/files`（`file_info` 富媒体上传）、`/v1/audio/speech`（TTS 返回 MP3）、
  `/api/v1/services/aigc/multimodal-generation/generation` 与 `/api/v1/services/audio/tts/SpeechSynthesizer`
  （百炼原生 TTS mock，JSON 返回 `output.audio`，model 以 `urltest` 开头走 audio.url 分支）、
  `/v1/images/generations`（生图返回 b64）、`/media/test.png` / `/media/test.silk`（仿真语音附件；
  V0.2.2 起产品代码会**下载音频存档**——优先 `voice_wav_url`（`/media/wav_variant.bin`），
  没有时存 silk 原件；mock 记录 `media_files_served` / `wav_variant_served` 供断言）、
  `plan_ranges` 分段下载断言用 `/media/large.bin`（4MB 随机数据，mock 的 Range GET 会 206
  且记录每个 `range_requests`）；mock 记录 `last_llm_user_text`（最近一次 LLM 请求的最后一条
  user 消息文本），
  用于断言"平台参考转写进了模型上下文"。
  控制面 `emit_c2c` / `emit_group` 支持 `attachments` 列表（含 `asr_refer_text`），`reset()` 支持 `vision_text`。
  **V0.2.2：`emit_group` 新增 `mention_marker`（是否带 `<@AppID>` 前缀，模拟平台新格式时传 False）、
  `mention_is_you`（True/False → mention 条目带 `is_you` 标记）、`mention_fields`（dict 合并进
  mention 条目，可覆盖 `id/user_openid/union_openid` 等字段模拟不匹配的身份格式）；
  mention 条目现固定带 `user_openid=mention_appid` 等身份字段（与真实平台 User 对象一致）——
  要模拟「全量推送但 @ 的不是自己」必须用 `mention_fields` 把身份字段改得不匹配
  （见坑 48 与 official/multibot 的新格式用例）**。
  辅助：`MockProcess`、`reset()`、`emit_c2c()`、`emit_group()`、`official_sent()`、
  常量 `OFFICIAL_APP_ID/SECRET/TOKEN`、`DEFAULT_OPENID`。
- `tests/card_factory.py`：生成测试用 PNG / JSON / YAML 角色卡。
- 自检统一用 `BAIAI_DATA_DIR` / `QQAI_DATA_DIR` 指向临时目录，互不污染。
- **跑自检的 shell 里不要残留 `BAIAI_DATA_DIR` 环境变量**：`common/paths.py` 里它优先级高于 `QQAI_DATA_DIR`，
  子进程 Bot 会用它覆盖测试设的临时数据目录，导致"Bot 进程已启动"健康检查连错端口而误报失败。
- **`tests.mock_servers.MockProcess` 的孤儿进程防护**：Qt 界面 teardown 偶发 abort
  （退出码 `-1073740791` / 0xC0000409，无害但会跳过 Python finally 的尾部语句），
  历史上每次崩溃退出泄漏一个 mock 子进程（曾积累 23 个）。现在两层防护：
  ① Windows 下 mock 子进程放进带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，
  父进程无论怎么死，Job 句柄关闭即杀子进程；② `gui_smoke` 的 finally 把 mock/bot 清理
  提到 Qt 析构之前。跑完自检可用
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -like '*tests.mock_servers*' }`
  确认无残留；有残留说明 Job Object 未生效（手动 kill 后查 `_attach_kill_on_close_job`）。
  gui_smoke 已加 stdout 行缓冲：即使 abort，「N 项通过」摘要也不会丢在块缓冲里。
- **`installer_smoke` 成品阶段必须剥离 `BAIAI_PAYLOAD`**：源码模式阶段把
  `BAIAI_PAYLOAD`（cmd.exe 冒充的小体积假 payload，见 `make_payload`）设进了全局环境变量。
  `installer.common.payload_dir()` 里该环境变量**优先于安装包内嵌 payload**——一旦泄漏给
  成品安装器子进程，装出来的"主程序"就是几百 KB 的 cmd.exe 副本，它的
  `--uninstall --silent` 什么也不做、直接退出码 0，卸载逻辑根本没被验证，
  曾让「自卸载后注册表项已清理 / 自己被删掉」两条误报失败。成品阶段的三个子进程
  （安装 / gui_alive / 主程序卸载）现在都显式传 `clean_env`（剔除 BAIAI_PAYLOAD），
  且「主程序与 Bot 都在」断言加了体积下限（>1MB）防回归。失败时设
  `BAIAI_KEEP_TESTDIR=1` 可保留临时目录，读 `real-install\data\logs\gui.log` 定位。
- **Windows 注册表「陈旧键缓存」怪癖**（本机 Win11 26200 实测）：一个进程反复
  创建/删除同一个 HKCU 键后，**其它进程重新创建的同名键，该进程（及其子进程树）
  的 winreg 会一直看不到**（OpenKey/DeleteKeyEx 报 WinError 2），而**新启动的
  reg.exe 子进程能看到**。这就是 `read_uninstall_entry` 要 winreg + reg.exe 双读、
  `remove_uninstall_entry` 要 winreg + reg.exe 双删的原因——别把 reg.exe 兜底当成
  冗余删掉。
- CI（`.github/workflows/ci.yml`）跑 pyflakes + 6 套（不含 frozen/installer，那两套只在 `workflow_dispatch` 时跑）。

---

## 10. 历史坑清单（真实踩过，改动前务必看）

> 这一节比前面的架构更有价值：每条都是花过时间的。

1. **`OverflowError: int too big to convert`（内测用户报的崩溃）**
   官方平台返回的机器人 ID 是 **20 位数字**，`int()` 后超出 int64。PySide6 把 `Signal(dict)` 的字典打包成
   `QVariantMap` 时，**每个越界整数都抛一次**，表现为"每 3 秒一条错误、堆栈还是空的"。
   修法（三层）：ID 按字符串输出 / `status_updated`、`event_received` 改 `Signal(object)` / emit 前 `qt_safe()`。
   回归断言在 `tests/gui_smoke.py`（真的 emit 一个 20 位 ID 的快照）。
2. **空堆栈 = 绑定层抛的**：日志里只有一行 `XxxError: ...` 没有 Python 帧，说明异常出在 shiboken/Qt
   的参数转换阶段，不是我们代码里的 `int()` —— 优先怀疑"值交给 Qt 时的类型/范围"。
3. **PowerShell 批量改源码 = 灾难**：`@"..."@` here-string 与 `-replace` 多次把字面 `\n` 写进 Python 源码、
   把多行拼成一行。**改源码一律用 `write` 工具或临时 Python 脚本**（改完删掉）。
4. **3 秒轮询会覆盖操作反馈**：点"测试连接"后结果被下一轮状态刷新冲掉。修法：`qq_form` 里
   `STICKY_SECONDS`/`_sticky_until` + `_show_result()`；注意 `_set_status(text, level=...)` 的**签名要保持**，
   自检里用 spy 包了它。
5. **热重载"饥饿"**：读接口消费了 `reload_if_changed()` 却不应用配置，导致改配置迟迟不生效。
   修法：`Runtime.sync_config()` 比对 `config.revision` 与 `_applied_revision`。
6. **`_dump_yaml(self.path, cleaned)` 参数个数错** → 遗留配置清理静默失效，改成 `_write_yaml(...)`。
7. **卸载后"应用列表里还有"**：自删除分支提前 `return` 跳过了 `remove_uninstall_entry`。
   修法：**注册表项最先删**，再安排延迟批处理删文件。
8. **`bot.exe: [Errno 13] Permission denied`**：正在运行的旧版本锁住自己的 exe。
   修法：安装前 `stop_running_app()`（只结束目标目录内的进程）。
9. **卸载时误杀自己的父进程**：`--uninstall` 由 PyInstaller 引导器启动，父进程也是自己人。
   修法：`current_process_tree_pids()` 排除自身与父进程。
10. **延迟清理批处理不执行**：`cmd /c` 的引号被吞。修法：生成 `.bat` 文件后用 `cmd.exe /c <bat>` 执行，
    内含等待进程退出 + `rd` + `reg delete` + 自删除。
11. **COM 建快捷方式失败**（目标 PE 无效）：退化为自己写 `.lnk` 字节（`build_lnk_bytes`/`write_lnk`），
    并用 `lnk_target()` 回读校验。
12. **注册表读写的视图差异**：`winreg` 与 `reg.exe` 看到的结果不一致时，用 `reg_exe_read` / `reg_exe_delete` 兜底。
13. **PyInstaller `update_exe_pe_checksum` PermissionError**：刚生成的 82MB exe 被杀软短暂锁定。
    修法：删掉旧 exe + 重试（`scripts\build_installer.bat` 内置自动重试）。
14. **CI 上路径断言误报**：GitHub Windows runner 的 `%TEMP%` 是 **8.3 短名**（`C:\Users\RUNNER~1\...`），
    而程序 `data_dir()` 做了 `resolve()` 得到长名。修法：测试里创建时就 `resolve()`，比较时两边都 `resolve()`。
15. **引导步骤编号断层**（第 4 步直接跳到第 6 步）：已改成 1–6 连续，`EXPECTED_HEADS` 与自检同步。
16. **官方机器人"连不上"其实是配置问题**：老 `config.yaml` 里留着 `mode: napcat` / 空的 AppID。
    现在会自动迁移并给出"尚未填写 AppID / AppSecret"的明确提示。
17. **换行符**：`.bat` 被写成 LF 会导致 cmd 解析错乱；改回来时记得 CRLF（`.gitattributes` 已固定，但脚本编辑仍可能破坏）。
18. **Python 3.9 的 API 差异**：`Path.write_text(newline=...)`、`str.removeprefix` 等 3.10+ 特性不要用。
19. **`ProviderSpec.available` 不存在（V0.2 富媒体回归）**：`common/providers.py` 的规格对象只有 `configured` 属性，
    但 `images.py` / `voice.py` 里多处写成 `if not spec.available:` → 运行时 `AttributeError`。
    表现很隐蔽：官方 e2e 里 `[IMG]` 生图和 silk ASR 都"静默降级成纯文字"（被 `chat_router` 的宽 except 吃掉），
    但 `bot.log` 里有 `'ProviderSpec' object has no attribute 'available'`。**改富媒体前先确认用的是 `spec.configured`。**
20. **mock 官方平台 `reset()` 曾把 `official_seq` 清零（V0.2）**：事件 id 基于 `official_seq` 生成，清零后
    跨 `reset()` 会**复用同一个 msg_id**，而接收端按 msg_id 维护 `msg_seq` 计数 → 富媒体段的 `msg_seq` 断言
    （期望 `[1,2]`）拿到 `[3,4]` 而失败。**官方事件序号本来就单调递增，`reset()` 不应清它**（现已保留）。
21. **安装包自检的"假 payload"用 `cmd.exe` 冒充 bot.exe（V0.2）**：`cmd.exe` 的 stdin 是 `DEVNULL`（EOF）时
    读完就退出，于是"运行中的程序会锁住自己的 exe"这条前提不成立。修法：`Popen(..., stdin=subprocess.PIPE)`
    保持一条打开的管道让 cmd 一直等输入（真 bot.exe 不读 stdin，不受影响）。
22. **跑自检的 shell 别带 `BAIAI_DATA_DIR`（V0.2 踩过）**：`common/paths.py` 里它优先级高于 `QQAI_DATA_DIR`，
    会覆盖测试设的临时数据目录 → 子进程 Bot 健康检查连错端口，"Bot 进程已启动"误报失败。详见第 9 节。
23. **（已移除）本地 ASR 模型下载（V0.2 开发中做后又删）**：asr 槽位（本地 SenseVoice / sherpa-onnx，
    首次识别自动下载约 230MB 模型 + 进度条）已随「听语音零配置化」整体移除——真机验证过
    QQ 官方平台随语音消息直接推 `asr_refer_text` 参考转写，用户确认后决定只留这条路径。
    遗留知识：`sherpa_onnx` 是延迟 import（没装也能启动）、非流式解码是 CPU 同步调用要
    `asyncio.to_thread`、本地引擎只收 16-bit PCM wav。若将来要恢复「可配线路」，这些结论都还有效。
24. **主模型整合的方向（V0.2）**：`load_slot("chat")` 是 **`llm` 段优先、`providers.chat` 兜底**，
    不是反过来。模型路由页面已不显示 chat 槽位（`models.py` 跳过 `SLOT_CHAT`），`_collect()` 保存时
    也不写 `providers.chat`（`deep_merge` 会保留磁盘上已有的旧值）。改动主模型读写路径时别破坏这个方向。
25. **Gemini（nano banana）的 OpenAI 兼容生图接口对参数挑剔（V0.2，用户踩过）**：
    `bot/media/images.py::_via_images` 的 body 会触发 400——部分兼容层版本直接拒绝 `response_format`，
    `size` 只接受特定取值（我们测试线路用的 512x512 就不行）。处理：**400 时自动降级参数重试**
    （全量 → 去掉 `size` → 去掉 `response_format`），解析兼容 `b64_json` 与 `url` 两种返回；
    再不行就落到 `_via_chat`（chat/completions + `modalities` 出图）。
    **别把 body 改回「一次发全量参数」的单发逻辑**；新增生图端点适配时保持这个重试链。
    预设里 Gemini 的 Base URL 是 `https://generativelanguage.googleapis.com/v1beta/openai`（带 `/openai` 段），
    模型名 `gemini-2.5-flash-image`。
26. **「测试线路」按表单当前值测试（V0.2）**：`/api/providers/test` 接受 `values`（表单当前值），
    `bot/api.py::_SlotOverrideConfig` 把它叠在已保存配置上——**空字符串字段按「没填」处理**
    （用户在界面上清掉了旧值，测试就该当没填）。修过一次：ASR 选 local 后点测试报「这条线路还没填完整」，
    就是因为只读了已保存配置（磁盘上还是 openai + 空 URL）。GUI 侧 `ProviderSlotForm.test()` 必须带上
    `values=self.values()`。
27. **PySide6 的 QComboBox 没有 `itemTexts()`（V0.2 踩过）**：Qt C++ 的 QComboBox 无此方法（那是
    QCompleter 的习惯），PySide6 一样没有。`provider_form.py` / `characters.py` 里判断音色是否在候选里，
    用 `[combo.itemText(i) for i in range(combo.count())]`。这 bug 之前被页面初始化的 try/except 吞掉
    （日志「页面初始化失败」），显式调 `refresh()` 就炸——看到这种日志别当小事。
28. **服务商预设的维护规则（V0.2）**：预设文件是 `app/llm_presets.py`（主模型，34 家）与
    `app/provider_presets.py`（三槽位：看图 13 / 生图 14 / TTS 7；asr 槽位的 8 家预设已随槽位移除）。规则：
    - `Provider.name` **是稳定 ID**——onboarding 与 gui 自检按名称引用（如 DeepSeek 预设名），改名会破坏自检；
    - 预设的 `models` 只是**候选/兜底**（服务商模型名经常变），真实清单靠「获取模型列表」从上游 `/models` 拉，
      所以新增服务商时模型名不确定就留空元组 + 在 `note` 里说明（如火山方舟要填 ep- 接入点 ID）；
    - 同一槽位内 `base_url` 尽量不重复（`by_base_url` 反查取第一个命中），跨槽位可以重复
      （按槽位 + URL 精确匹配，不会串）。已知例外：vision 槽位有两条硅基流动预设
      （DeepSeek-VL2 专用 + 其他 VL 模型），反查会优先显示前者，功能无碍；
    - 预设可带 `engine` 字段（如 Gemini 原生生图预设 = `gemini-native`），选中时表单自动切引擎下拉；
    - `labels()` 末位固定是「自定义 / 其他」，自检依赖它。
29. **Gemini 生图接口的实测结论（V0.2，用户真实 Key 实测 + 官方文档核对）**：
    - **OpenAI 兼容层（`…/v1beta/openai`）的 `/images/generations` 只支持个别图像模型**——
      官方文档（ai.google.dev/gemini-api/docs/openai）点名只有 `gemini-2.5-flash-image` 与
      `gemini-3-pro-image-preview`；`prompt/model/n/size/response_format` 都接受，未知参数静默忽略，
      `extra_body` 可传 `aspect_ratio` / `generation_config` / `safety_settings`。
      其他模型（如 nano banana 2 lite = `gemini-3.1-flash-lite-image`）走兼容层会 **404**（不是 Key 问题）。
    - **新一代 Nano Banana 2 系列走原生接口**（`gemini-native` 引擎）：
      `POST …/v1beta/models/{model}:generateContent?key={key}`，body 带
      `generationConfig.responseModalities=["TEXT","IMAGE"]`，图片在
      `candidates[0].content.parts[].inlineData`（base64，**PNG 或 JPG 都可能**，按 mimeType 定扩展名）。
      注意：原生接口只认 **`?key=` 查询参数**，`Authorization: Bearer` 会 401（Gemini CLI 的 `AQ.` 前缀 Key 同理）。
    - **兼容层的 chat 出图（`/chat/completions` + `modalities`）现在要求小写** `["text","image"]`
      （大写会 400 "Invalid modality type"）；且目前对图像模型有服务端 bug——
      `gemini-2.5-flash-image` 直接 400「chat.completions 不支持」，3.x 模型生成出 **JPEG** 后
      兼容层崩在 "Unhandled generated data mime type: image/jpeg"。
      所以程序策略：`gemini-native` 引擎原生优先、chat/modalities 兜底（modalities 小写→大写自动重试）；
      openai 引擎走 `/images/generations`（参数降级重试）→ chat 兜底。
    - 实测产物留档：`extras/samples/gemini_native_2.5_flash_image_实测.png`、
      `extras/samples/gemini_兼容层_3pro_image_preview_实测.png`（生图实测输出样例）。
30. **QQ 官方附件消息的语音字段（C2C_MESSAGE_CREATE，官方文档核对过）**：
    平台 `MessageAttachment` 对语音消息有三个关键字段：
    - `asr_refer_text`：QQ/腾讯内置 ASR 的参考转写，免费——**这就是产品用的转写**
      （零配置、不下载音频，直接进模型上下文）；平台某次没给时记 errors 提示"角色暂时没听清"；
    - `voice_wav_url`：平台已把 SILK 转成 WAV 的文件 URL——现在不下载（留字段备将来可配线路用）；
    - 新版 schema **没有 `aes_key`**（旧文档提过，现文档已无）；`filename` 可能无扩展名，
      分类要按 `content_type`（`voice` / `audio/silk`）兜底。
    实现见 `bot/media/hub.py::_process_voice_attachment`（参考转写单一路径），单元 + e2e 都有覆盖
    （official_smoke 断言参考转写进了 `last_llm_user_text` 且未下载任何音频）。
31. **TTS 风格参数与音色清单（V0.2，官方文档核对过）**：
    - edge-tts 走微软 Edge TTS 原生 `rate` / `pitch` / `volume`（如 "+10%" / "+5Hz" / "-10%"）；
      OpenAI 兼容 `/audio/speech` 官方支持 `speed`（0.25~4.0，不支持的兼容服务商会忽略）。
      配置写在 `providers.tts` 的 `rate/pitch/volume/speed` 键，`load_slot` 读进 `spec.extra`，
      `MediaHub.compose` 与 `/api/providers/test` 回读后传给 `TTS.synthesize`；
    - GUI「模型路由 → 文字转语音」有「音色调节」行（edge-tts：语速 -50%~+100% / 音调 ±50Hz /
      音量 ±50%~+100%；OpenAI：语速倍率 0.5~2.0），0 值不写配置（保持服务商默认）；
      「角色管理」对话框每角色可单独设音色 + 试听，试听文案每次从
      `bot/media/voice.py::PREVIEW_TEXTS`（12 句）随机取一句并回显；
    - 音色清单：静态兜底 14 个中文音色（微软当前官方清单，`provider_form.EDGE_TTS_ZH_VOICES`，
      旧的 3 个已下线音色 Xiaochen/Xiaohan/Xiaochuan 已移除）；启动后 `/api/media/voices`
      经 `edge_tts.list_voices()` 拉全量（322 个，中文在前）合并进下拉框，拉不到保留静态清单；
    - 语音回复默认概率 30% → **10%**（`DEFAULTS["media"]` / `config.example.yaml` / models.py 三处同步）。
32. **视觉（看图）链路的实测结论（V0.2 用户真机反馈"角色看不见图"，用真实 Key 实测定位）**：
    三个叠加的原因，缺一不可——
    - **图片必须挂在最后一条（当前）user 消息上**。`AIEngine._with_image` 原先挂在
      历史第一条 user 消息上；deepseek-flash 实测：挂早期历史时模型回复"图没加载出来，
      我这看不到"，挂当前消息则能准确描述图片内容（同一张图 A/B 对照 2 轮全对）。
      官方文档只说"图片只能在 user 消息"，没说必须哪条——这是模型行为，不是文档承诺。
    - **推理类模型看图会先"想"再答**：deepseek-flash 对复杂照片（~180KB 照片）在
      max_tokens=200 时把 200 token 全耗在 `reasoning_tokens` 上，返回
      `finish_reason=length` + **content 为空**。视觉线路的 `max_tokens` 现在至少给 1024
      （`_vision_client`），正文才不会空。
    - **MIME 必须按文件头识别**：`media/store.py` 落盘文件名没有点
      （`20261005_110802_dee5jpg`），`Path.suffix` 取不到扩展名，原先一律标 `image/png`
      把 JPEG 送上去。`image_data_url` 现在按 magic bytes 识别
      （JPEG FFD8 / PNG 89504E47 / GIF / WEBP），识别不了才按扩展名猜。
    - 生图侧同批修复：gemini-native 遇 503（Google 高负载，官方原话"稍后重试"）
      会 3 秒后重试一次再降级；用户 11:09 的失败 = 503 高负载 + 降级到兼容层 chat
      撞上已知 JPEG bug（坑 29）。
    实测脚本留档：`scripts/dev_vision_probe.py` / `dev_vision_diag.py` / `dev_vision_ab.py` /
    `dev_gemini_gen_test.py`（Key 已改为环境变量 `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`，
    图片路径改为 `data/media/inbox/your_image.jpg` 占位，仓库里不含真实 Key 与图片）。
33. **音色清单只在表单构建时拉一次会"永久 14 个"（V0.2 用户反馈"模型路由音色太少"）**：
    模型路由页在窗口初始化时构建表单，若当时 Bot 还没就绪，`/api/media/voices` 拉取失败、
    下拉框只剩 14 个静态中文音色，之后也不再重试（角色管理侧每次打开对话框都拉，所以那边全）。
    修法：`ModelsPage.refresh()` 每次都调 `form_tts._refresh_voice_list()`，`on_status` 里
    Bot 上线后再补救重试一次。**凡是"启动时拉一次"的清单数据都要考虑 Bot 就绪时序。**
34. **角色级音色调节的合成路径（V0.2）**：`MediaHub._tts_style(character)` 先取角色
    `tts_rate/tts_pitch/tts_volume/tts_speed`（非空才用），留空回退 `providers.tts` 的全局值；
    试听走 `/api/providers/test` 的 `values`（`_SlotOverrideConfig` 叠到 `providers.tts` 节点，
    `load_slot` 读进 `spec.extra`）。**别在合成时只读全局**，否则角色级设置静默失效。
35. **头像的存储与迁移（V0.2）**：头像文件存 `data/characters/avatars/{id}{ext}`，
    DB 只存相对路径 `avatar_path`；`registry.update_avatar` 换不同扩展名时会清理旧文件。
    PNG 卡导入时整图就是头像（`loader` 把 PNG 字节直接放进 `avatar_bytes`），
    JSON/YAML 卡读 base64 `avatar` 字段。`avatar_path` 可能是绝对路径（测试/tempdir），
    `registry.abs_path` 两种都认。
36. **自动更新的时序与限制（V0.2）**：
    - **安装程序会主动杀掉安装目录里正在运行的旧版本**（`ic.install` →
      `stop_running_app`），所以 GUI 的更新流程是「拉起新安装包（DETACHED）→ 自己尽快退出」，
      不要指望安装完成后旧进程还活着去做收尾。`launch_after=True`（不带 `--no-run`）
      时安装程序装完会自己启动新版。
    - GitHub 未登录 API 限速 60 次/小时/IP：启动检查有 6 小时限流
      （`app.update_last_check`），手动检查不限流但别加轮询。
    - 源码模式（`python -m app.main`）没有「安装目录」概念：`install_dir_for_self()`
      回退到注册表 InstallLocation / 默认目录，界面上「重装 / 更新」装的是那个目录，
      界面上会明确显示「源码运行」。
    - 版本比较按数字段（`parse_version`）：`0.10 > 0.9`；tag 与 `V0.2` 这种显示格式
      都能解析，解析不出来就按「不算新版」处理（宁可不更新也不误更新）。
    - 单实例锁：新版启动时旧版必须已退出（QSharedMemory），安装程序的 `stop_running_app`
      保证了这一点，别在 GUI 里加「新旧并存」的逻辑。
37. **阿里云百炼（DashScope）TTS 不是 OpenAI 兼容协议（V0.2，真实 Key 实测过）**：
    - 百炼的 `/compatible-mode/v1` 只兼容 chat / embeddings / `/models` 等接口，
      **TTS 走 `/audio/speech` 会 404**（标准域名与 MaaS 工作空间域名都 404，已实测）。
    - 非实时 TTS 走 DashScope 原生端点，且**按模型系列分家、不能混用**：
      Qwen-TTS（`qwen3-tts-*`）→ `/api/v1/services/aigc/multimodal-generation/generation`；
      CosyVoice / Qwen-Audio-TTS → `/api/v1/services/audio/tts/SpeechSynthesizer`
      （文档：help.aliyun.com/zh/model-studio/non-realtime-tts-user-guide）。
      `bot/media/voice.py` 的 `_dashscope_path()` 按模型名前缀路由，`_dashscope_root()`
      把 Base URL 剥成服务根（用户填 compatible-mode 地址也能直接用）。
    - 请求体 `{"model":..., "input":{"text":..., "voice":...}}`，`voice` 必填。
      **三族音色不能跨模型混用**（官方文档原话：混用时返回 `InvalidParameter`，
      例如 `[cosyvoice:]Engine error [411]: TTS speak operation failed`，已实测）：
      Qwen-TTS（`qwen3-tts-*`）用 Cherry / Ethan 等英文名；CosyVoice（`cosyvoice-*`）
      用 longanyang 等（**v1 / v2 音色名不通用**：cosyvoice-v2 配 longxiaochun 会 400
      「Engine return error code: 418」，v3-flash 配 longanyang 正常）；
      Qwen-Audio-TTS（`qwen-audio-*`）用 yuxiaoyun_v3.1 等（官方「Qwen-Audio-TTS
      音色列表」，含支持方言/多语种的 longanhuan_v3.1 等 4 个 + 精品中文/英文音色）。
      音色候选在 `bot/media/voice.py` 的 QWEN_TTS_VOICES / COSYVOICE_VOICES /
      QWEN_AUDIO_VOICES 三个常量（`dashscope_voices()` 汇总进 GUI 下拉）；
      `_dashscope()` 遇到 411 / 418 时会在报错里附上三族正确音色示例。
      合成时 `load_slot` 给 dashscope 引擎默认 Cherry。
    - 返回是 JSON：`output.audio.data`（Base64）或 `output.audio.url`（24 小时临时链接，
      要自己下载）；业务错误在 HTTP 400 的 body 里（`code` / `message`），
      `_dashscope()` 会把两者拼进 VoiceError。
    - MaaS 工作空间地址格式 `https://ws-{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
      可直接当 Base URL 用；`GET {base}/models` 在该域名下可用（「获取模型列表」不用改），
      但返回的 262 个模型**不含 cosyvoice-\* 和 qwen-audio 非实时 TTS 系列**（实测
      qwen-audio-3.1-tts-flash 不在列表里，端点却完全可用）——属百炼上游列表行为，
      所以预设的候选模型里把 qwen-audio-3.1-tts-flash 也列上了。
    - Qwen-TTS 非实时接口**没有 speed 等参数**，GUI 对 dashscope 引擎隐藏两组音色调节
      （显示说明标签，不报错）；Qwen3-TTS-Instruct 系列支持自然语言指令控制、
      Qwen-Audio-TTS 系列支持情感/富语言标签（均已接，见坑 38）。
    - 已用真实工作空间 Key 实测：`qwen3-tts-flash` + Cherry、`cosyvoice-v3-flash` +
      longanyang、`qwen-audio-3.1-tts-flash` + yuxiaoyun_v3.1 均 200 返回 WAV；
      qwen-audio + Cherry 复现 411。
    - 试听/测试线路的预览音频：`bot/api.py::_b64_preview` 原截断 120KB（约 2 秒），
      用户反馈「只能听到 2 秒」——GUI 与 Bot 同机走 127.0.0.1，已改为给完整音频
      （2MB 上限防极端长文本）。
38. **TTS 指令风格 SKILL（V0.2，`bot/media/instruct.py`）**：
    - 背景：`qwen3-tts-flash` 直出音色语调偏平。百炼的 `qwen3-tts-instruct-flash`
      支持自然语言指令控制表现力（`input.instructions`，≤1600 Token，仅中英文，
      可配 `input.optimize_instructions=true` 服务端再做语义优化），官方文档给出
      「如何编写高质量的声音描述」规范——本 SKILL 就是把这套规范内置进提示词。
    - 链路：`MediaHub.compose`（或「测试线路」）发现 tts 模型命中
      `is_tts_instruct_model()`（模型名含 `instruct` 且不含 `realtime`）→
      `generate_tts_instruction()` 用**主模型**（`runtime.engine.llm`）按 SKILL 提示词
      （五原则：具体/多维/客观/原创/简洁；维度：语速/音调/情感/语气节奏/场景；
      官方示例原文）+ 角色人设（description/personality 截 400 字）+ 待合成文本
      生成一句指令 → `parse_tts_instruction()` 清洗（围栏/引号/「指令是：」前导语/
      多行标签行，截 300 字）→ `TTS.synthesize(instruction=...)` 把
      `instructions` + `optimize_instructions=true` 放进请求体。
    - 指令里**不指定性别/年龄/声线**（音色由 voice 参数固定），只描述「怎么读」；
      情感必须贴合语境（提示词里已约束：安慰温柔、调侃俏皮、提问上扬……）。
    - 降级：主模型未配置 / 超时（30s）/ 报错 / 解析为空 → 返回空串，合成照常不带
      指令。每条语音分片各生成一次指令（分片文本不同，语境不同）。
    - **思考类主模型（deepseek-flash 等）的坑（真实用户日志发现）**：模型先输出
      `reasoning_content` 再给正文，`max_tokens=120` 会被 reasoning 吃光
      （`finish_reason=length` + `content=""`，偶发约 1/3）。对策：指令生成
      `max_tokens=512` + 空内容时外层再重试一次（`generate_tts_instruction`）；
      「测试线路」主模型探活同理（`LLMClient.test_connection` max_tokens 16→128）。
    - **解析器的引号坑**：指令正文里可能带强调引号（「记得」「别跑太快」），
      「取引号内容」只在**整段首尾成对**时剥外层引号；悬空引号只在**不平衡**时
      去掉（否则会把正文末尾的强调引号截掉）。
    - 附带改进：`TTS._dashscope` 对 qwen3-tts*/qwen-tts* 模型自动带
      `language_type`（中文/英文自动判定，`_detect_language`，拿不准不给走 Auto）——
      官方明确「指定语种能显著提升合成质量」。
    - **CosyVoice 的指令参数名不同**：是 `instruction`（单数）；系统音色的 v3-flash/
      v3-plus 要求「固定格式和内容」（见 CosyVoice 音色列表页），v3.5 复刻/设计音色
      可任意指令——目前 SKILL 只接 Qwen3-TTS-Instruct 系列，CosyVoice 指令留待扩展。
    - Qwen-Audio-TTS（qwen-audio-3.x）的**情感与富语言标签**已接入（见下方「Qwen-Audio 标签」
      小节）；其 `instruction` 参数（单数，任意指令，官方文档确认
      qwen-audio-3.1-tts-flash / 3.0-tts-plus / 3.0-tts-flash 支持，系统音色与复刻音色均可，
      实测 200）暂未与标签同开——标签已覆盖情感/语速/拟声，先只用一套机制，效果不够再叠加。
    - 已用真实工作空间 Key 实测：instruct 模型带指令 200 返回 WAV，且带/不带指令
      音频时长不同（指令真实生效）；qwen-audio 带 `[whispers]` / `[giggles]` /
      `[serious]+[excited]` 切换 / `instruction` 参数均 200，音频时长随之变化。
    - **Qwen-Audio 情感与富语言标签（V0.2 第二轮，用户反馈「qwen-audio 调教效果不行」
      后按官方文档补齐）**：`is_qwen_audio_tts_model()` 门控（`qwen-audio*` 非实时、
      排除 asr）→ `generate_qwen_audio_tags()` 用主模型按 `build_qwen_audio_tag_messages()`
      的 SKILL 提示词（内置官方全量标签表：23 控制类 + 7 富语言类，含场景→标签映射
      「撒娇→[mischievously]/[whispers]、安慰→[empathetic]…」）把标签嵌入文本
      → `parse_tagged_text()` 清洗（围栏 / 「好的，改写后的文本是：」类短前导语+冒号
      截断 / 引号 / 多行）+ `_validate_tags()` 只保留官方表内英文标签（编造的 `[happy]`
      会被删掉防念出来；中文方括号 [哈哈] 视为正文保留）→ 带标签文本直接作为
      `text` 参数发送（`MediaHub.compose` 与「测试线路」均已接入，日志/提示分别显示
      「情感标签」）。
    - **第三轮加固（用户反馈「标签有时生效有时不生效 + 效果还不够」，按官方
      non-realtime-qwen-audio-tts-http-api 全量参数表补齐）**：
      * **双机制叠加**：SKILL 输出升级为严格两行（`文本：` 带标签文本 + `指令：`
        一句整体语气指令，语速/音调/情绪底色 40 字内），`parse_tagged_output()`
        拆两块（行优先找最后一行「指令：」，找不到按行内标记拆）；指令经 qwen-audio
        的 `instruction` 参数（单数）发送，与标签同时生效。
      * **标签逐句覆盖**：提示词要求控制标签放第一句开头、**每个句子开头都要放**
        （可沿用同一标签）——官方说明控制标签「作用于其后文本，直到下一个控制标签
        或长句自动切分」，长句切分会打断标签效果，逐句覆盖后整段都带情感。
      * **本地兜底 `local_qwen_audio_tags()`**：主模型 3 次尝试（max_tokens=512，
        思考类模型空内容重试）仍失败时，按高置信线索给标签（哈哈→[giggles]、
        委屈/难过→[sad]、快一点+！→[very fast]、哼/才不是→[mischievously]、
        别怕/有我→[empathetic]、疑问→[curious]）+ 固定中性指令——保证**每次**
        合成都带调教参数；bot.log 每分片一行「语音调教（角色）：文本 | 指令」。
      * **官方全量可调参数**（`TTS._dashscope_qwen_audio_params` 做 GUI 值映射，
        只对 `qwen-audio*` 模型发送）：`rate` 0.5~2.0（优先 OpenAI 式 speed 倍率，
        否则 edge 式「+10%」换算）、`pitch` 0.5~2.0（edge「+5Hz」按 1+Hz/200 近似）、
        `volume` 0~100（edge 百分比相对默认 50）、`language_hints`（中文→["zh"]、
        英文→["en"]，自动判定）、`format=wav` + `sample_rate=24000`（显式）。
        GUI：模型路由页选 qwen-audio 模型时，音色调节行的语速/音调/音量/语速倍率
        自动显示（`_sync_engine_display` 按模型前缀判断，模型框改模型会重同步）；
        角色音色对话框的调节本来就在，自动生效。
      * 实测（真实 Key + 真实主模型 deepseek-flash）：「快一点！要迟到了！路上别光顾着
        看手机。」→ `[very fast]快一点！[very fast]要迟到了！[serious]路上别光顾着看手机。`
        + 指令「语速偏快，音调稍高，催促中带着关心的叮嘱」——句内情感切换 + 逐句覆盖
        均按预期；请求体带 instruction/rate/format/sample_rate/language_hints 全部 200。
    - **第四轮（用户反馈「语气指令已生效、情感标签仍有概率不生效，建议不限制思考长度」+
      贴出官方标签表与示例要求「认真重做」）**：
      * **max_tokens 512 → 4096**（指令与标签两个生成函数都改）：实测 deepseek-flash
        在 4096 下思考完整 `finish=stop` 稳定输出；512 时 reasoning 吃光预算会返回空
        （finish_reason=length）——这就是「有时不生效」的主要来源之一。4096 只是上限，
        模型正常停止不额外计费。
      * **实测发现的第二个来源：模型自创中文标签**。4096 探针里 deepseek-flash 会输出
        `[温柔催促]` `[着急担忧]` 这类中文方括号标签，旧 `_validate_tags` 把中文方括号
        当正文保留 → 这些词会被 TTS 念出来 / 情感没打上。现在：提示词明确「标签是英文
        单词、逐字照抄、不许自创、不许用中文标签」+ 校验器升级——
        `_TAG_ALIASES` 近似拼写归一（laugh→laughing、happy→excited、Whispers→whispers
        等 10 个）、**输出里已出现官方标签时中文方括号视为编造剥除**（完全没有官方标签
        时 [哈哈] 仍按正文保留）、剥除打 warning 日志可查。
      * **SKILL v3 按官方原文重建**（`build_qwen_audio_tag_messages`）：官方语义原样写入
        （控制标签「作用于其后的所有文本，直到遇到下一个控制类标签，或因句子较长被
        自动切分为止」；富语言标签「在当前位置插入一段拟声效果，不影响前后文本的情感
        风格」）、官方两个使用示例原样内置；30 个标签逐个配场景精细映射（每个官方
        标签都有使用出口，要求「选最贴合的那一个」）；
      * **强制标签覆盖**：控制标签必须放文本最开头（不许裸开头）；多句文本每句开头都放
        （情感相同重复同一标签、变化换标签，防长句自动切分打断）；逗号中间不放控制标签；
        富语言标签 0~2 个放对应词语位置；**平铺直叙也必须放中性底色标签**（[serious] /
        [tired]）——不再有「不打标签」的合法出口，配合本地兜底（也无线索时给 [serious]）
        实现「每次都有标签 + 指令」。
      * 实测：5 句样张 100% 主模型产出官方标签 0 兜底——傲娇「哼，才不是为你，只是顺路
        而已。」→ `[mischievously]` + 「语气傲娇，嘴硬里藏着关心」；平铺直叙「好的，明天
        九点开会。」→ `[serious]` + 中性指令；催促句内 [very fast]→[serious] 切换。
    - 官方标签语义/用法见 non-realtime-tts-user-guide「情感与富语言标签」一节；
      参数范围（rate/pitch/volume/language_hints/seed/hot_fix 等）见
      non-realtime-qwen-audio-tts-http-api（`seed` 固定随机种子、`hot_fix` 多音字/
      替换——未接入，需要时再加）。
    - **TTS 推荐默认（第五轮，用户要求「百炼设为默认并标注推荐」）**：
      `SLOT_ENGINES[SLOT_TTS]` 重排为 `[dashscope, edge-tts, openai]`（首位=界面默认
      选中），`SLOT_DEFAULT_ENGINE[SLOT_TTS]`、`common/config.py` DEFAULTS 的 tts 段、
      `config.example.yaml` 全部改为 dashscope + qwen-audio-3.1-tts-flash +
      yuxiaoyun_v3.1 + 百炼地址；`_engine_label` 对 tts 的 dashscope 标注「（推荐）」；
      `ProviderSlotForm.set_values` 对**完全未配置**的 tts（base/model/voice 全空且
      引擎为空或 dashscope）自动填推荐组合（用户只需粘贴 Key）；
      `load_slot` 的 dashscope 无音色回退从 Cherry 改为 yuxiaoyun_v3.1（与推荐模型
      同系列，Cherry 是 qwen3-tts 系，混用会 411）；模型占位文案改 qwen-audio-
      3.1-tts-flash；预设名标注「（推荐）」。
      gui_smoke 引擎切换测试改用 `SLOT_ENGINES[SLOT_TTS].index(...)` 取索引（不写死
      位置）；「切回 edge-tts 后等全量音色」处加了**去重补刷**：TaskRunner 按
      `media_voices_<form>` key 去重，切引擎瞬间上一轮刷新还在跑时本轮刷新会被跳过，
      等空闲后 `count<=100` 再手动 `_refresh_voice_list()` 一次（实测抓到的时序坑）。

39. **页面宽度守卫：任何「不换行的长单行文本」都会把滚动页撑宽裁掉右侧（V0.2，用户反馈
    「点 TTS 测试线路后页面突然左移、显示不全」定位）**：
    - 机制：`QScrollArea.setWidgetResizable(True)` 时，Qt 把滚动内容宽度取
      `max(视口宽, 内容.minimumSizeHint 宽)`。内容最小宽度 = 布局里各子控件最小宽度之和，
      **QLabel 最小宽度 = 整句文本宽度（不换行时）、QComboBox 最小宽度 = 当前项文本宽度**。
      所以一句 1474px 的提示标签（TTS 百炼参数说明）能把模型路由页撑到 2553px
      （视口只有 898px）：右侧控件被裁、无横向滚动条（AlwaysOff），引擎切换/刷新引发
      显隐变化时用户看到的就是「页面突然左移偏移」。
    - 定位方法（可复用）：offscreen 起窗口 → 逐页 `show_page` + `ensure_loaded` →
      对页面里每个 QScrollArea 比较 `scroll.widget().width()` 与 `viewport().width()`，
      超出的页再用 minW 递归遍历（`minimumSizeHint().width() > 阈值` 逐层下钻）找元凶控件。
      临时探针已删，需要时照此重写（模式见本条与 gui_smoke 的宽度守卫）。
    - 修复两层：
      * **结构性（根本）**：所有可滚动页面的内容容器
        （`app/pages/base.py` 的 `container`、`app/pages/bots.py` 的 `form_host`、
        `app/pages/characters.py` 的 `form_container`、`app/onboarding.py` 各步骤页）
        设 `QSizePolicy.Policy.Ignored`（宽向）+ `setMinimumWidth(0)`——内容允许收缩到
        比子控件最小宽度更窄，长文本只在自己内部裁行/截断，不再顶宽整页。
        **PySide6 注意：`Qt.SizePolicy` 不存在，要用 `QSizePolicy.Policy.Ignored`**；
        且 Ignored 对 `QScrollArea` 的 `minimumSizeHint` 传播有效（Qt 布局的
        effectiveMinimumSize 认 size policy）。
      * **具体控件**（缩小最小宽度，观感更好）：`provider_form.py` 百炼提示标签
        单独一行 + `setWordWrap(True)`（调教行拆成内层 HBox + 外层 VBox）、
        各 spin `setMinimumWidth(72)`、dashscope 引擎标签缩短为「阿里云百炼 DashScope（推荐）」；
        `settings.py` 生成参数行拆两行（max_tokens/temperature 一行、top_p 一行）+
        路径标签 `setWordWrap(True)`；`qq_form.py` `in_target_openid` 320→230px。
    - 防回归：`tests/gui_smoke.py` 开头新增**全页面宽度守卫**（逐页切页 + 检查所有
      QScrollArea 的 `widget().width() <= viewport().width()+2`）。
      **注意该守卫切页会触发 `_on_nav_changed → ensure_loaded`**：对话查看页若在此时
      首次加载（库里还没消息），表格会自动选中当时第 0 行（空会话角色）——
      所以对话章节必须「循环 `refresh()` 直到表格第 0 行 `message_count >= 2`」
      （refresh 按 `load_conversations` key 去重，旧任务在跑时新刷新被静默跳过，
      重试是必须的），再 `clearSelection() + selectRow(0)`（不清空则对同一行是无效操作）。
    - 顺带修的产品缺陷：`conversations.py` 的 `refresh()._ok` 重新填充表格
      （`setRowCount`）会清掉现有选中，而旧逻辑只在 `current_character_id is None` 时
      自动选——用户点「刷新」后消息面板会停在旧角色上。现在 `_ok` 记住 prev_id，
      填充后按该角色**跟回选中行**（角色已不存在则回落到第 0 行；列表清空时
      `current_character_id` 置 None）。
40. **自检 149 项全过、进程却以退出码 1 结束（V0.2 发布后 CI 首次红，根因两层）**：
    - **第一层（bot 侧）**：`/ws/events` 处理器原来只 `await queue.get()` 从不断言对端
      存活，uvicorn 关闭时该后台任务永远阻塞 → bot 卡死在
      「Waiting for background tasks to complete」，不跑 lifespan shutdown（bot.log 停在
      `Shutting down`，没有「Bot 进程正在退出…」）。修法：处理器里加一个
      `_watch_disconnect` 任务并发 `await websocket.receive()`（对端断开时
      `WebSocketDisconnect` 被消费掉），主循环用 `asyncio.wait({get_task, watcher})`
      两边都等；另外 `uvicorn.run(timeout_graceful_shutdown=8)` 兜底——**任何新的
      后台任务/长连接都要问一句「服务端 shutdown 时它怎么结束」**。
    - **第二层（GUI 侧，真正的非 0 退出码来源）**：bot 慢退（20 秒）期间 EventStream
      处于重连退避（`msleep` 最长 15 秒），`stop()` 原来只 `wait(3000)` → 超时后
      `self.event_stream = None` 释放最后一个引用 → **QThread C++ 对象在线程还跑着时
      被析构** → Qt 打印 `QThread: Destroyed while thread '' is still running` 并
      `__fastfail`（0xC0000409，本地实测退出码 -1073740791，GitHub Actions 报 exit code 1）。
      修法：`EventStream.stop()` 循环 `wait(500)` 直到 `isRunning()==False`（上限 20 秒），
      退避上限 15s→5s；`TaskRunner.shutdown()` 的 `waitForDone` 3s→10s（池线程同类风险）。
    - **防回归**：gui_smoke 把 bot 的 `/api/shutdown` 收尾从 `finally` 静默等待挪进
      检查正文——bot 15 秒未退出直接 `[FAIL] Bot 进程在 /api/shutdown 后 15 秒内退出
      （无泄漏的后台任务）`，不再无声强杀。`finally` 里保留 `bot_stopped` 守卫的兜底
      清理（try 中途异常时仍能杀掉 bot）。
    - **诊断路径备忘**：这类「全过但非 0 退出」先看**汇总行之后**的 stderr——
      failfast 前 Qt 会打 `QThread: Destroyed while ...`；CI 日志用 API 拉
      （`GET /actions/jobs/{id}/logs` 302 重定向时要**去掉 Authorization 头**，
      否则 blob 存储 403）。
41. **APScheduler 任务「到点静默不触发」（V0.2.2 多机器人重构引入，scheduler_live 才抓出来）**：
    - **症状**：任务注册成功、`next_run_time` 正确、调度器 running，到点后 `next_run`
      直接跳到**下一个周期**（看起来像"执行完了"），但任务函数一行日志都没打。
    - **根因**：给任务带参数时用了 `(lambda bot_id: self._job_x(bot_id), {"bot_id": ...})`
      这种"同步 lambda 包装协程"的写法。APScheduler 用
      `iscoroutinefunction_partial(job.func)` 判定：lambda 是**普通同步函数** → 丢进
      线程池执行 → lambda 调用 async 方法返回的**协程对象没人 await**，被 GC 静默丢弃
      （`run_job` 只看到 lambda 正常返回）。next_run 照常推进，所以状态接口看起来一切正常。
    - **修法**：直接传**协程函数本体**（bound method 会被正确识别）+ 单独的
      `kwargs` 字典——`ProactiveScheduler._add` 约定 `func` 可以是
      `(协程函数, kwargs 字典)` 元组，但元组里的函数本身必须是协程函数（已写进 docstring）。
    - **排查工具**：`scheduler_live` 等待期间轮询 `/api/proactive/status` 打印各任务
      `next_run`——next_run 提前跳到下一周期 = 任务"执行"过但协程没跑。
42. **`ORDER BY id ASC LIMIT n` 会截掉最新消息（V0.2.2 上下文窗口查询引入）**：
    - **症状**：对话历史一长（> 窗口 limit），模型"看不到当前这条消息"——刚 append
      的 user 消息不在传给 LLM 的上下文里，但数据库里明明有。
    - **根因**：`crud.messages_in_window`（以及 `search_messages`）按 `id ASC` 取 limit 条，
      窗口内消息超过 limit 时取到的是**最旧**的 n 条，最新的被挤出。
    - **修法**：一律 `ORDER BY id DESC LIMIT n` 取最新再 `rows.reverse()` 反成升序
      （`recent_messages` / `recent_summaries` 本来就是对的）。**凡是"取最近 N 条"的
      查询都要先 DESC 再反序，禁止 ASC + LIMIT。**
43. **`Accept-Ranges` 头判定写成 `"bytes=" in value`（V0.2.2 多线程下载引入）**：
      HTTP 标准里 `Accept-Ranges` 的取值就是 `bytes`（**不带等号**，等号是 Range
      **请求头**里的格式），写成 `"bytes="` 永远判 False → 所有下载悄悄退化成整文件
      单连接。判定用 `"bytes" in value.lower()`。installer_smoke 的大文件分段断言
      （>=2 个 Range 请求且覆盖完整）就是防这个的。
44. **定时触发 jitter 写错单位（900 秒 ≠ 90 秒）**：CronTrigger 的 `jitter` 单位是
      秒；多机器人重构时把 90 写成 900（迟到最多 15 分钟），`scheduler_live` 的
      210 秒等待窗口抓不到。改配置数值前先确认单位与测试等待窗口的匹配。
45. **多机器人群里「@ 一个、所有机器人都答」（V0.2.2 全量群消息引入）**：
    - **症状**：用户开了官方平台的「接收所有消息」（全量模式）后，同一个群里的多个
      机器人都会收到群里**每条**消息（`GROUP_MESSAGE_CREATE`），之前只要 content 里
      出现任何 `<@...>` 占位（哪怕是 @ 的另一个机器人）就被当成 @ 了自己 → 集体抢答。
    - **平台事实**（bot.q.qq.com 文档 + 真实网关抓包）：全量事件 content 保留
      `<@机器人标识>` 标记；`mentions` 是「消息中 @ 的用户列表」（User 对象，`id` /
      `bot: bool`），@ 机器人事件本身不含机器人自身；官方文档明确「相同 msg_id 可能
      重复推送，开发者需结合 msg_id 做去重」（@ 事件与全量事件可能同时收到同一条，
      不去重会回两遍）。
    - **修法**：`receiver.parse_event` 对全量事件精确匹配 `<@!?(AppID|user_id)>` 占位
      与 `mentions[].id`（本机器人全部已知身份，`_bot_identities`），新增
      `any_mentioned` 标志；`_should_reply` 里「没 @ 自己但 @ 了别人」直接让路
      （debug 日志：群消息 @ 了别人（不是本机器人），忽略）；`OfficialReceiver.handle`
      按 msg_id 做 5 分钟 TTL 去重。mock 侧：`emit_group` 支持 `event_type` /
      `mention_appid`（模拟全量模式 + 被 @ 的 AppID），`MockProcess(app_id=...)` 让
      两个 mock 平台代表两个不同机器人应用（token 端点按实例 AppID 校验）。
    - **真机注意**：真机上 @ 占位里到底是 AppID 还是 user_id 以实际抓包为准，
      代码两种身份都匹配，若还有出入看 `receiver` 的 debug 日志。
46. **`QLabel` 没有 `setIcon`（V0.2.2 对话页媒体徽标引入）**：`QIcon` 的 set 方法只在
    `QAbstractButton` 系（QPushButton/QToolButton）上有；给 QLabel 挂图标要
    `label.setPixmap(icon.pixmap(QSize(w, h)))`。当时语音徽标用 `badge.setIcon(...)`
    在运行到该消息才炸（slot 异常被 gui_smoke 的「未处理异常」巡检抓到），且炸在
    `_load_messages._ok` 的填充循环中间 → 表格留下**有行没 item** 的中间态
    （`item(r, 0)` 为 None 的崩溃就是它）。教训：表格填充循环里任何一步抛异常都会
    留下半填充状态，`_ok` 里的 widget 构造代码要按「可能抛」对待；gui_smoke 的
    「界面槽函数没有未处理异常」检查对这类运行期错误是唯一保险。
47. **mock 的 test.png IDAT 块 CRC 是错的**：手写的 1x1 PNG 字节里 IDAT 的 CRC 校验
    不对（PIL 能容忍、Qt 会报 `libpng error: IDAT: incorrect data check` 并可能解出
    空 pixmap）。已用 `zlib.crc32` 重新生成常量并验证。凡是往 mock 里塞二进制附件，
    都要用 PIL / Qt 双端验一遍解码。
48. **群 @ 彻底不回复（V0.2.2 真机回归，双层根因）**：官方平台 2026-09 起群 @
    消息的 content **已去除 @ 前缀**，@ 判定只剩 `mentions` 字段（User 对象，含
    社区确认的 `is_you` 标记）。坑 45 修好的匹配只认 `<@标识>` 占位与
    `mentions[].id` 一个字段 → 新格式下全量事件识别不出「@ 的是自己」被让路；
    而 `handle()` 的去重发生在 `_should_reply` **之前**，让路也把 msg_id 标成
    已处理 → 后到的 `GROUP_AT_MESSAGE_CREATE`（恒为 @ 自己）被去重吃掉 →
    **这条消息永远没人回**。修法两条：a) 去重改状态机（`_check_duplicate` 返回
    前先记录 `"replied"/"skipped"`，`_mark_replied` 在真正开始回复时覆盖）——
    已回复的 msg_id 不再回第二遍，让路过的 msg_id 允许后续 @ 事件处理；
    b) `_mentions_self` 同时匹配 `is_you` 与 `id/user_openid/union_openid/
    member_openid` 多个字段（平台字段格式可能因版本而异，多字段冗余匹配最稳），
    并保留旧 `<@标识>` 占位路径；全量消息 @ 了机器人但没匹配上时打 WARNING
    带 mention 明细（真机排查用）。教训：**平台改格式时「识别不出」和
    「去重」两个机制叠加会把消息无声吞掉**，凡「先判定后去重」的顺序，去重
    必须区分「处理过」与「让路过」。
49. **QTableWidget 的 setCellWidget 单元格吃掉鼠标点击**：媒体单元格是
    `QWidget` 容器 + 子 `QLabel`（缩略图/徽标），子控件把 mouse 事件全部
    消费，表格的 `itemClicked` 永不触发 → 「缩略图点了没反应」（gui_smoke
    此前只测了程序化 `selectRow`，没测真实点击，所以没抓到）。修法：容器
    自建 `_MediaCellWidget`（`mousePressEvent` 里发 `clicked` 信号）+ 所有
    子控件 `setAttribute(Qt.WA_TransparentForMouseEvents)` + 手形光标。
    教训：凡是往表格单元格里塞自定义控件的页面，自检必须用
    `QTest.mouseClick` 发真实点击事件断言结果，不能只断言 selectRow 后的
    程序化行为。
50. **scheduler_live 偶发红：目标分钟被进程启动吃掉**：测试把定时触发设在
    `now+1 分钟`，但 mock 启动 + bot 进程启动（解释器导入，杀软扫描时更慢）
    可能超过 1 分钟；bot 装调度任务时该分钟 cron 槽位已过，APScheduler
    对已过去的槽位**不回补**（misfire_grace 默认 1 秒），任务直接排到第二天，
    自检 210 秒等不到消息。修法：目标时间改 `now+2 分钟`、`WAIT_SECONDS`
    210→300。教训：涉及「等调度器到点」的测试，目标时间必须给进程启动留足
    缓冲；APScheduler 装任务时若 cron 槽位已过就等下一轮，不会补发。
51. **桌面快捷方式「有时候创建不出来」（V0.2.2 用户反馈，双因）**：
    a) 桌面被迁移到 OneDrive 的机器（Win11 家庭版常见），真实桌面在
    `%USERPROFILE%\OneDrive\Desktop`，而安装器硬编码 `%USERPROFILE%\Desktop`
    ——快捷方式写进了不显示的目录，桌面上一片空白。修法：`desktop_dir()` /
    `startmenu_dir()` 改读 `HKCU\...\Explorer\User Shell Folders` 的
    `Desktop` / `Start Menu` 值并展开环境变量（Known Folders 用户覆盖），
    读不到才回落原路径。b) `create_shortcut` 的 COM 失败兜底（二进制写
    .lnk）之后用 WScript.Shell 读回验证，杀软把 COM 读回也拦住时，
    刚写好的好文件被当成坏文件删掉 → 彻底没快捷方式。修法：读回失败时
    改验文件头魔数（`_lnk_looks_valid`：尺寸 0x4C + CLSID），完好就保留。
    另外安装结果现在带 `shortcut_methods`（com/binary/failed），GUI 完成
    弹窗与静默日志都会写明，真机再出问题一眼定位。
52. **QTableWidget 没设 SelectRows 时，点非 0 列只选中单个 item**：
    对话页「查看选中消息的图片 / 播放语音」按钮依赖 `selectedRows()`
    （默认第 0 列）取行，用户点「角色」列的格子 → 只有 (行,1) 被选中 →
    取不到行 → 按钮一直灰着（「按钮不起作用」，真机反馈）。修法：消息表
    `setSelectionBehavior(SelectRows)` + `_selected_media_row()` 改遍历
    全部选中索引按行找媒体（双保险）。教训：表格凡是「按行取数据」的
    逻辑，要么强制 SelectRows，要么别依赖 `selectedRows()` 的默认列。
53. **`itemClicked` 只发一个参数（QTableWidgetItem\*），不是 (row, column)**：
    直连 `itemClicked.connect(self._on_message_cell_clicked)`（处理函数签名
    是 `(row, column)`）→ 点任何普通格子就 `TypeError: missing 1 required
    positional argument`（真机点时间列/角色列选消息时必炸；点缩略图/徽标
    走的是单元格控件自己的 mousePressEvent，不经过 itemClicked，所以此前
    一直没触发）。修法：`lambda item: handler(item.row(), item.column())`。
    教训：Qt 的 item* 信号参数都是 item 而不是行号列号，连 (row, column)
    签名的槽必须包一层拆参；gui_smoke 要真实点击**普通格子**（不只是
    单元格控件）才能覆盖到这条路径。

---

## 11. 已知限制与待办

**限制**

- 仅 Windows（托盘、注册表、快捷方式、安装包都是 Windows 专有实现）。
- 仅 QQ 官方机器人：受平台限制，**主动消息只能发给已经与机器人交互过的 openid**（程序会自动学习并记录目标 openid）。
- 单机运行，控制接口只监听 `127.0.0.1`。
- 没有真实账号的自动化测试：官方通道（含 V0.2 富媒体）全部靠 mock（`tests/mock_servers.py`），**真实 QQ 上发图/语音仍需在真机验证**。
- V0.2 富媒体依赖各线路端点的 OpenAI 兼容实现；不同厂商对 `image_url`（data URL vs http）、
  `/images/generations` 返回字段（`b64_json` vs `url`）、TTS `voice` 取值的支持程度不同，
  代码已做了常见分支的兜底，但**换厂商时要用「模型路由」页的「测试线路」逐个验证**。

**待办 / 可做**

- [ ] **V0.2.2 真机验证 + 发布**（分支 `v0.2.2`，基于 `543adc6`）：8 项修复 + 对话分级管理
  + 3 项新修复（多机器人群 @ 只回被 @ 的 / 对话列表直接显示图片缩略图与语音播放徽标 /
  生图前按角色人设匹配二次元或写实风格）+ 第二批（群 @ 真机回归修复：状态化去重 +
  `is_you`/多字段 mention 匹配，见坑 48 / 「主动消息」改名「消息设置」+ 顶部按机器人
  切换设置 / 富媒体行为搬到「消息设置」、生图风格搬到「机器人」页按机器人设置 /
  语音概率默认 5% / 媒体单元格真实可点击，见坑 49）已在 mock 全链验证，
  待用户真实 QQ 验证 → `main` 合入 + tag `v0.2.2` + GitHub Release `v0.2.2`。
  重新打包已完成（2026-10-08 第四批后）：`dist\BaiAi-Tavern V0.2.2.exe`（本地安装用）+
  无空格发布副本 `dist\BaiAi-Tavern-V0.2.2.exe`（SHA256
  `1700EF9C4CA97646AF1E2C72273E33B895CB94B522D13231722CEEEEFABB6D97`，
  见 `dist\SHA256SUMS.txt`）+ 绿色版 `dist\BaiAi-Tavern\`。
  上传 Release 用无空格副本 + SHA256SUMS.txt（octet-stream + `?name=`，见坑 10）。
  真机重点看（第三批）：**对话页点「角色」列选中消息后「查看 / 播放」按钮是否可用**、
  **桌面快捷方式是否正常出现**（若仍失败，看安装完成弹窗/`%LOCALAPPDATA%\BaiAi-Tavern\logs\silent_install.log`
  里 `shortcut_methods` 写的是 com / binary / failed，桌面重定向的机器已改走注册表真实桌面，见坑 51）。
  真机重点看：**多机器人不再同时发同样的主动消息**、**多机器人群里 @ 谁谁回答（其他机器人不抢话，
  平台新旧两种 @ 格式都认）**、**群消息 @/不 @ 都按开关响应（重点回归：群 @ 是否恢复回复；
  若还不回，查日志「全量群消息 @ 了机器人但未匹配到本机器人身份」的 mention 明细）**、
  **机器人 2 的 AppID/Secret 保存后仍在**、**更新下载速度（多线程）**、**更新后自动拉起新版**、
  **「消息设置」页顶栏选机器人单独设置后各自生效**、**「机器人」页按机器人设生图风格**、
  **对话页图片缩略图点击可放大 / 语音徽标点击可播**、**二次元角色发图是动漫风、真实风格角色发图是照片风**、
  **对话页按月/按天筛选与语音回放**、**15 天以上老对话不进上下文但可查**（压缩摘要生效）。
  历史坑：坑 41（APScheduler 协程静默丢失）/ 坑 42（ASC+LIMIT 截断最新消息）/
  坑 43（Accept-Ranges 判定）/ 坑 44（jitter 单位）/ 坑 45（全量群消息多机器人全回）/
  坑 46（QLabel 没有 setIcon）/ 坑 48（状态化去重 + mention 多字段）/ 坑 49（单元格吃点击）。
- [x] **V0.2 真机验证 + 发布**（已完成）：用户真实 QQ 验证通过 → 打包 → `main` 合入 V0.2、tag `v0.2`、GitHub Release `v0.2`（安装包 + SHA256SUMS.txt）已上传，自动更新链路生效。历史坑见坑 40（CI 首跑 gui 自检 exit code 1 的双层根因与修法）。
  「听语音」零配置（官方平台参考转写），真机重点确认**有参考转写时角色能听懂**（已验证过一次：
  11:05 语音消息日志里出现参考转写并正常回复）；「[IMG] 发图」用 Gemini 时选**原生接口预设**
  （nano banana 2 系列，已用真实 Key 实测出图，见坑 29 与 worktree 根目录实测图），真机确认一次即可。
  顺带验证「模型路由」的服务商预设与「获取模型列表」在真实端点上的表现，以及本轮新增的
  **角色管理界面改动**（音色对话框的角色级调节 + 试听、点头像换图、绑定菜单位置、编辑窗口滚动条、
  模型路由音色下拉是否为全量 300+）与**页面宽度修复**（点 TTS「测试线路」/ 切换引擎 /
  刷新各页后页面不再左移偏移、右侧不再显示不全，见坑 39；窄窗口 1160×760 下重点看
  「机器人」「系统设置」两页）。
  另：用户曾贴过 Gemini Key（`AQ.` 前缀）用于实测，**发版前提醒用户去 AI Studio 轮换该 Key**。
- [ ] **在真机用真实官方机器人跑一遍完整链路**（配置 → 连接 → 收消息 → 回复 → 主动消息），目前只有 mock 覆盖。
- [ ] README 增加界面截图（对下载转化帮助很大，需人工提供图片）。
- [x] `SECURITY.md` 的联系方式已改为作者公开的 QQ / 微信（与 README 一致）。
- [ ] 仓库加 topics（`qq-bot`、`pyside6`、`sillytavern`、`desktop-app` 等）提升可发现性。
- [ ] `v0.2` 发布流程沉淀成脚本（打包 + 校验 + 建 Release + 传附件一条命令）。
- [ ] 主动消息的"记忆摘要/压缩"（`bot/memory/long_term.py`）目前是加权条目，可考虑引入摘要模型。

---

## 12. 接手第一天的建议动作

```bat
:: 1) 确认环境与基线全绿
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pyflakes app bot common installer tests scripts
.venv\Scripts\python.exe -m tests.smoke_test --unit-only      :: 最快，约 10 秒
.venv\Scripts\python.exe -m tests.smoke_test                  :: 端到端
.venv\Scripts\python.exe -m tests.gui_smoke

:: 2) 跑起来看真实界面（不需要真实 QQ / LLM，自检用的 mock 也能手动起）
scripts\start.bat

:: 3) 改一个小东西走完整流程：config → bot/runtime → 页面 → 自检 → pyflakes
```

**提交前自检清单**（与 `.github/PULL_REQUEST_TEMPLATE.md` 一致）：pyflakes 干净 → 相关自检全绿 →
没提交凭据/日志/产物 → `.bat` 仍是 CRLF → 文档与 `CHANGELOG.md` 同步。
