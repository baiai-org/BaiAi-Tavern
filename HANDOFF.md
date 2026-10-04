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
| 怎么验证 | `python -m tests.smoke_test` 等 8 套自检，共 **548 项**；`python -m pyflakes app bot common installer scripts tests` 必须干净 |
| 关键硬约束 | ① 代码保持 **Python 3.9 兼容** ② 自检必须全绿 ③ 任何"外部数据 → Qt"的数值都要过 `app/qt_safe.py` |
| 当前版本 | V0.1（公开内测），仓库已开源公开，CI 绿，Release 带安装包 |
| 最大的坑 | 见第 10 节，尤其 **Qt `Signal(dict)` + 超 int64 整数** 与 **PowerShell 批量改源码** |

---

## 1. 项目定位与当前状态

**做什么**：导入 SillyTavern 角色卡（PNG / JSON / YAML）→ 绑定到 QQ 官方机器人 → 角色在你说话时用对应人格回复，并在**定时 / 空闲 / 随机**三种策略下主动找你说话。所有参数都在 `config.yaml`，全部能通过界面改。

**当前状态（V0.1）**

- 接入方式**只有 QQ 官方机器人**（AppID / AppSecret）。历史上支持过 NapCat / OneBot，**已完整移除**：相关代码、界面、下载器、安装包内容都删了，老配置里的遗留键会在 `ConfigManager.load()` 时自动清理并写出 `config.yaml.bak`。
- 多机器人 + 多角色：每个机器人一套官方凭据，绑定一个角色，互不串台。
- 安装包：**单个 EXE**（安装 / 重新安装 / 卸载合一），按用户安装到 `%LOCALAPPDATA%\Programs\BaiAi-Tavern`，不需要管理员权限；卸载默认保留 `data/`（配置 + 聊天记录）。
- 已开源公开：<https://github.com/baiai-org/BaiAi-Tavern>（Apache-2.0），Release `v0.1` 附安装包与 `SHA256SUMS.txt`。

---

## 2. 仓库与发布信息

| 项目 | 值 |
|---|---|
| 仓库 | <https://github.com/baiai-org/BaiAi-Tavern>（public，默认分支 `main`） |
| 协议 | Apache-2.0（`LICENSE`，第三方清单见 `NOTICE`） |
| CI | `.github/workflows/ci.yml`（Windows runner：pyflakes + 6 套自检；`workflow_dispatch` 时额外打包并跑 frozen 自检） |
| 发布 | Release `v0.1`，附件 `BaiAi-Tavern-V0.1.exe`（72.9MB）+ `SHA256SUMS.txt` |
| 产物 | `dist\BaiAi-Tavern V0.1.exe`（安装包）、`dist\BaiAi-Tavern\`（绿色版）、`dist\BaiAi-Tavern.exe`、`dist\bot.exe` |
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

> Release 附件名**不要带空格**：GitHub 会把空格替换成点（`BaiAi-Tavern V0.1.exe` → `BaiAi-Tavern.V0.1.exe`），
> 所以对外统一用 `BaiAi-Tavern-V0.1.exe`，本地 `dist\` 里的文件名可保持带空格。
> 上传 Release 需要 token 具备 `Contents: write`（classic token 需 `repo` + `workflow`）；
> **不要把 token 写进任何文件**，用完立即 Revoke。

---

## 3. 运行架构

```
┌──────────────────────── GUI 进程（app/）────────────────────────┐
│ app/main.py          单实例锁、日志、主题、异常钩子、--uninstall  │
│ app/main_window.py   主窗口 + 侧边栏 + 7 个页面 + 托盘            │
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
```

鉴权：仅当 `api.token` 非空时校验请求头 `X-Tavern-Token`（或 `?token=`）。**只监听 `127.0.0.1`**；
若把 `api.host` 改成 `0.0.0.0`，必须同时设置 `api.token`（见 `SECURITY.md`）。

---

## 4. 目录与文件地图（改哪里去哪个文件）

| 目录 | 内容 | 常见改动入口 |
|---|---|---|
| `app/` | GUI 进程（34 个文件，含包初始化） | 界面/交互 |
| `app/pages/*.py` | 7 个页面：仪表盘 / 机器人 / 角色管理 / 主动消息 / 对话查看 / 系统设置 / 日志 | 加功能优先看 `pages/base.py`（`Page` 基类：`build/refresh/on_status/on_event/reload_if_loaded`） |
| `app/widgets/*.py` | 可复用控件：`qq_form`（官方凭据表单）、`llm_form`、`character_card`、`fields`、`status_indicator` | 表单字段 |
| `app/onboarding.py` | 6 步配置引导（步骤编号连续，`EXPECTED_HEADS` 与自检绑定） | 引导流程 |
| `app/uikit.py` / `app/icons.py` | 手绘图标/箭头/开关（**不用 emoji 字形**） | 任何视觉元素 |
| `app/qt_safe.py` | 把外部数据转成 Qt 安全形式（超 int64 整数 → 字符串） | **新增"外部数据进信号"时必须过这里** |
| `bot/` | Bot 进程（28 个文件，含包初始化） | 业务逻辑 |
| `bot/runtime.py` | 多机器人装配、配置热重载、事件总线、`snapshot()` | 加新的状态字段 |
| `bot/qq_official/*.py` | 官方通道：`client`(REST/凭证) `gateway`(WS/心跳/重连) `messaging`(发送/探测) `receiver`(事件分发) | 官方协议相关 |
| `bot/scheduler/proactive.py` | 主动消息调度（APScheduler 任务、配额、免打扰、`trigger_once`） | 触发策略 |
| `common/` | 双进程共用：`config`(ConfigManager) `paths` `bots`(BotSpec) `text`(分段/清洗) `async_utils` `logging_setup` `utils` | 配置/路径 |
| `installer/common.py` | **安装/卸载全部逻辑**（复制、注册表、快捷方式、自删除、进程回收） | 安装行为 |
| `installer/installer_main.py` | 安装向导（安装 / 重装 / 卸载，`--silent` 等） | 向导 UI |
| `tests/` | 8 套自检 + mock（见第 9 节） | 加断言 |
| `scripts/` | `start.bat`(开发启动) `build.bat` `build_installer.bat` `make_icons.py` `make_version_info.py` `gui_entry.py` `bot_entry.py` | 打包 |
| `resources/` | 内置角色卡 3 张、图标 ico/png、`styles/dark.qss` | 资源 |

**配置系统要点**

- 结构（`common/config.py` 的 `DEFAULTS`）：`app` / `llm` / `qq` / `proactive` / `memory` / `database` / `characters` / `api` / `logging`。
- `qq` 下：`id name enabled character_id character_name user_nickname reply_enabled group_reply_enabled official{app_id,app_secret,...}`；**多机器人是 `qq` 下的列表式条目**（`raw_bot_entries()` / `common/bots.py`）。
- 值支持 `${ENV_VAR}` 展开。
- 路径覆盖（自检靠它隔离）：`BAIAI_HOME`、`BAIAI_DATA_DIR`（兼容旧名 `QQAI_HOME` / `QQAI_DATA_DIR`）；卸载注册表键可用 `BAIAI_UNINSTALL_KEY` 覆盖。
- 数据目录优先级：`BAIAI_DATA_DIR` → exe/项目目录下 `data/`（可写时）→ `%APPDATA%\BaiAi-Tavern\data`（旧目录 `QQ-AI-Tavern` 存在则沿用）。
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
scripts\build_installer.bat  :: dist\BaiAi-Tavern V0.1.exe（组装 payload → 单文件安装包）
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

## 9. 自检体系（8 套，共 548 项）

| 命令 | 项数 | 覆盖 |
|---|---|---|
| `python -m tests.smoke_test` | 176 | 单元（86）+ 端到端（90，含 mock 官方平台与 mock LLM） |
| `python -m tests.official_smoke` | 54 | 官方通道：凭证 / 网关 / 单聊 / 群聊 / 主动消息 / 重连 / 错误码 |
| `python -m tests.multibot_smoke` | 38 | 两个官方机器人 + 两个角色互不串台 |
| `python -m tests.onboarding_smoke` | 84 | 6 步配置引导（含步骤标题 `EXPECTED_HEADS`） |
| `python -m tests.gui_smoke` | 107 | 界面集成（offscreen）：七页裁切体检、官方表单、超大 ID 回归 |
| `python -m tests.scheduler_live` | 14 | 定时触发"真实到点"慢速自检 |
| `python -m tests.frozen_smoke` | 34 | **打包产物**（产物比源码旧时自动跳过，`--force` 强制） |
| `python -m tests.installer_smoke` | 41 | **真实安装包**安装/卸载 + 主程序自卸载 + 旧版本运行时升级 |

- `tests/mock_servers.py`：mock LLM + mock QQ 官方平台（`/app/getAppAccessToken`、`/users/@me`、`/gateway`、
  `/v2/users/{openid}/messages`、`/v2/groups/{group_openid}/messages`、WS `/official-ws`、控制面 `/__control/*`）；
  辅助：`MockProcess`、`reset()`、`emit_c2c()`、`emit_group()`、`official_sent()`、
  常量 `OFFICIAL_APP_ID/SECRET/TOKEN`、`DEFAULT_OPENID`。
- `tests/card_factory.py`：生成测试用 PNG / JSON / YAML 角色卡。
- 自检统一用 `BAIAI_DATA_DIR` / `QQAI_DATA_DIR` 指向临时目录，互不污染。
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

---

## 11. 已知限制与待办

**限制**

- 仅 Windows（托盘、注册表、快捷方式、安装包都是 Windows 专有实现）。
- 仅 QQ 官方机器人：受平台限制，**主动消息只能发给已经与机器人交互过的 openid**（程序会自动学习并记录目标 openid）。
- 单机运行，控制接口只监听 `127.0.0.1`。
- 没有真实账号的自动化测试：官方通道全部靠 mock（`tests/mock_servers.py`）。

**待办 / 可做**

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
