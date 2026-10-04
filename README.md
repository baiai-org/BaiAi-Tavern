# BaiAi-Tavern

[![CI](https://github.com/baiai-org/BaiAi-Tavern/actions/workflows/ci.yml/badge.svg)](https://github.com/baiai-org/BaiAi-Tavern/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](requirements.txt)
[![Platform](https://img.shields.io/badge/platform-Windows-0078d4.svg)](#)

**版本 V0.1　·　作者 [baiai.org](https://baiai.org) QQ:951424960 微信:BH8GYP　·　协议 [Apache-2.0](LICENSE)**

带图形界面的 **QQ 多角色 AI 主动消息桌面应用**。

导入 SillyTavern 角色卡（PNG / JSON / YAML），让 AI 角色住进 QQ：既能在你说话时用对应人格回复，
也能在每天固定时刻、你长时间没说话时、或在活跃时段内随机地**主动找你聊天**。
全部操作都在图形界面里完成，最终打包成 **一个安装包 EXE**：双击安装、有桌面图标、
随时可以从「Windows 设置 → 应用」里卸载。

> **QQ 接入方式只有一种：QQ 官方机器人**（QQ 开放平台）。
> 在 [q.qq.com](https://q.qq.com) 创建机器人应用，把 **AppID / AppSecret** 填进程序即可 ——
> 合规、不需要小号、没有封号风险。
> 早期版本支持过的第三方协议组件（NapCat / OneBot）**已彻底移除**，旧配置里的相关字段会在首次
> 启动时自动清理（原文件备份为 `config.yaml.bak`）。

```
┌───────────────────────────────────────────────────────────┐
│  BaiAi-Tavern.exe（PySide6 界面 + 系统托盘）                │
│     仪表盘 · 机器人 · 角色管理 · 主动消息 · 对话查看 · 设置 · 日志 │
│                                            （左下角：关于）  │
└───────────────┬───────────────────────────────────────────┘
                │ 本机 HTTP API (127.0.0.1:8765) + WebSocket 事件推送
┌───────────────▼───────────────────────────────────────────┐
│  bot.exe（FastAPI + APScheduler + SQLite）                 │
│  网关连接 · 角色对话 · 提示词引擎 · 记忆系统 · 主动调度        │
└───────────────┬───────────────────────────────────────────┘
                │ WebSocket 网关（收）+ REST 接口（发）
┌───────────────▼───────────────────────────────────────────┐
│  QQ 官方机器人开放平台（api.sgroup.qq.com）                  │
└───────────────────────────────────────────────────────────┘
```

---

## 0. 下载安装（普通用户）

不想自己编译的话，直接到 **Releases** 页面下载打包好的安装包：

**➡ [github.com/baiai-org/BaiAi-Tavern/releases](https://github.com/baiai-org/BaiAi-Tavern/releases)**

| 文件 | 说明 |
|---|---|
| `BaiAi-Tavern-V0.1.exe` | Windows 安装包（约 73MB，单文件，安装 + 卸载都在里面） |
| `SHA256SUMS.txt` | 校验值，下载后可自行核对：`certutil -hashfile "BaiAi-Tavern-V0.1.exe" SHA256` |

安装：双击 → 选择安装位置（默认 `%LOCALAPPDATA%\Programs\BaiAi-Tavern`，按用户安装、不需要管理员权限）
→ 勾选是否创建桌面/开始菜单快捷方式 → 完成。卸载在「Windows 设置 → 应用」里，
或在开始菜单里再次运行安装包选「卸载」；**卸载默认保留你的配置与聊天记录**。

> 系统要求：Windows 10 / 11（64 位）。无需安装 Python 或其它运行库。
> 首次运行会弹出 6 步配置引导，准备好「LLM 接口的 API Key」和「QQ 官方机器人的 AppID/AppSecret」即可。

---

## 1. 快速开始（源码运行）

```bat
:: 1) 准备环境（Python 3.10+ 推荐；3.9 亦可）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2) 一键启动（会自动生成 data/config.yaml）
scripts\start.bat
```

启动后界面会自动拉起 Bot 进程，并在系统托盘常驻。窗口默认按屏幕自适应（不小于 1160×760），
左侧依次是**仪表盘 / 机器人 / 角色管理 / 主动消息 / 对话查看 / 系统设置 / 日志**，
左下角还有一枚**「关于」**按钮（点开可以看到版本 V0.1、作者 baiai.org，以及按 1. 2. 3. 编号列出的
全部开源项目与主页链接）。

**首次运行会自动弹出「配置引导」**，固定 6 步，跟着走一遍就能用：

| 步骤 | 内容 |
|---|---|
| 1 欢迎 | 说明接入的是 QQ 官方机器人（不需要小号），以及需要准备的三样东西 |
| 2 LLM 接口 | **下拉选择常用服务商**（自动填好 Base URL）→ 填 API Key → 点**「获取模型列表」从上游拉取模型并在列表里点选** → 点「测试连接」当场验证 |
| 3 QQ 配置 | 填官方机器人 **AppID / AppSecret**（附申请入口），并**指定这个机器人由哪个角色说话** |
| 4 角色卡 | 一键导入**内置的 3 个默认角色**，或导入 PNG/JSON/YAML 角色卡（导入后立即出现在列表里） |
| 5 主动消息 | 选择定时/空闲/随机触发与每日上限（默认保守配置） |
| 6 完成 | 配置检查（缺什么会标红），点「完成」立即生效 |

也可以随时点「取消引导（我自己配置）」跳过，之后在「系统设置」里配；
想再看一遍引导：**系统设置 → 配置引导**，或把 `config.yaml` 里的 `app.onboarding_done` 改成 `false`。

### 手动配置顺序

1. **系统设置 → LLM**：选服务商 → 填 `API Key` → 点「获取模型列表」选模型 → 点「测试连接」。
2. **系统设置 → QQ（官方机器人）**：填 **AppID / AppSecret** → 点「测试连接」，
   状态变成「已连接官方网关」即可。
3. **机器人**：多机器人时在这里新增/停用，并给每个机器人**绑定角色**。
4. **角色管理**：点「导入内置角色」一键导入默认 3 个角色，或导入角色卡、自己写人设。
5. **主动消息**：设置触发时间、空闲阈值、频率上限、免打扰，点「保存并应用」。

> 只跑源码也可以：`python -m app.main`（界面）、`python -m bot.main`（Bot 进程，通常由界面自动拉起）。

**`启动.bat` 的行为**：依次在「脚本所在目录 → `dist\BaiAi-Tavern\` → `dist\`」中查找
`BaiAi-Tavern.exe` 并启动；若都不存在，则回退到源码模式（优先 `.venv\Scripts\pythonw.exe -m app.main`）。
所以在仓库根目录双击它，和在发布目录里双击它，都能正常拉起程序。

> ⚠️ 修改 `.bat` 文件时请保持 **CRLF 换行**：cmd.exe 无法正确解析仅 LF 换行的批处理
> （会出现 `'目录不存在' is not recognized` 这类把一行拆成几段的报错）。
> 仓库已用 `.gitattributes`（`*.bat text eol=crlf`）固定这一点。

---

## 2. 打包与安装包

### 2.1 绿色版（免安装）

```bat
scripts\build.bat
```

脚本依次安装依赖、生成图标与版本信息、用 PyInstaller 打包 GUI 与 Bot 进程，并组装发布目录：

```
dist\BaiAi-Tavern\
├── BaiAi-Tavern.exe     # 主程序（界面 + 托盘，也负责卸载自己）
├── bot.exe              # Bot 服务进程（由界面自动拉起）
├── config.example.yaml  # 配置模板（首次运行自动复制为 data/config.yaml）
├── 启动.bat             # 一键启动
├── resources\           # 主题与图标
└── data\                # 首次运行时自动创建（配置 / 数据库 / 角色卡 / 日志）
```

> 重新打包**不会**动 `data\`、`config.yaml`，升级不丢配置和聊天记录。

### 2.2 安装包（**一个 EXE**，安装 + 卸载都在里面）

```bat
scripts\build_installer.bat
```

产出**一个文件**：

| 文件 | 说明 |
|---|---|
| `dist\BaiAi-Tavern V0.1.exe` | 安装程序（约 82MB，内部已带好程序文件，不需要联网） |

它同时承担卸载：

* 双击运行 → 图形向导：未安装时显示「开始安装」；**已安装时显示已安装版本**，
  可以「重新安装」（升级 / 修复，用户数据不动）或「卸载」；
* **安装前会自动关掉安装目录里正在运行的旧版本**（界面与 `bot.exe`）—— 正在运行的 exe 会锁住自己，
  否则升级安装会报「以下文件复制失败：bot.exe: [Errno 13] Permission denied」；
* 安装位置默认 `%LOCALAPPDATA%\Programs\BaiAi-Tavern`（**按用户安装，不需要管理员权限、不弹 UAC**）；
* 桌面快捷方式 + 开始菜单快捷方式默认都创建（可在界面取消）；
* 写入 `HKCU\...\Uninstall\BaiAi-Tavern`，所以「Windows 设置 → 应用」里能看到并卸载；
* **不再发布单独的卸载 EXE**：卸载项直接指向安装目录里的主程序
  （`BaiAi-Tavern.exe --uninstall`），安装程序自己也提供「卸载」按钮。

命令行用法（给脚本 / 自动化用）：

```bat
:: 静默安装到指定目录（不建快捷方式、装完不运行）
"dist\BaiAi-Tavern V0.1.exe" --silent --dir "D:\Apps\BaiAi-Tavern" --no-desktop --no-startmenu --no-run

:: 静默卸载（保留用户数据）；加 --remove-data 则连数据一起删
"dist\BaiAi-Tavern V0.1.exe" --silent --uninstall
"D:\Apps\BaiAi-Tavern\BaiAi-Tavern.exe" --uninstall --silent
```

安装/卸载本身有自检：`python -m tests.installer_smoke`（真的装一遍、再卸一遍）：

| 检查项 | 说明 |
|---|---|
| 程序文件 | 复制完整、旧版本残留（独立卸载程序、第三方协议目录）被清掉 |
| 快捷方式 | 桌面 + 开始菜单，**能被系统读回并指向安装目录里的主程序** |
| 卸载信息 | 名称 / 版本 V0.1 / 作者 baiai.org / `--uninstall` 命令写入 HKCU |
| 卸载行为 | 程序文件与快捷方式清干净、**用户数据默认保留**、注册表项立刻消失 |
| 自删除 | 主程序删除正在运行的自己（延迟批处理），目录只剩 `data\` |
| 成品验证 | 用真实安装包装一次、再用安装出来的主程序卸一次 |

打包参数：`pyinstaller.spec`（GUI，含 `installer.common` 以便 `--uninstall`）、
`pyinstaller_bot.spec`（Bot）、`pyinstaller_installer.spec`（安装包，内部以 `payload` 目录携带程序文件）。
四个 exe 都写入了版本信息（`scripts/make_version_info.py` 生成），
资源管理器里右键 →「属性 → 详细信息」可以看到 **产品名称 BaiAi-Tavern、产品版本 V0.1、公司 baiai.org**。

---

## 3. 接入 QQ（QQ 官方机器人）

### 3.1 申请机器人（一次性，约 5 分钟）

1. 打开 [q.qq.com](https://q.qq.com) → 用手机 QQ 扫码登录 → **创建机器人**（个人开发者即可）；
2. 在「开发设置」里拿到 **AppID** 与 **AppSecret**；
3. 把机器人**加为你的好友**（或拉进群并 @ 它）—— 官方平台只允许机器人先收到消息才能回复；
4. 回程序：**系统设置 → QQ（官方机器人）** 填 AppID / AppSecret → 点「测试连接」，
   状态显示「已连接官方网关」即成功。

### 3.2 主动消息发给谁（openid）

官方接口**不能按 QQ 号发送**，只能按 `openid`。程序有三种方式确定目标：

1. **自动记住**（推荐）：让别人（或你自己的另一个号）先给机器人发一条消息，程序会记住这个 `openid`；
   界面上能看到「已记住的私聊对象」，也可以点「忘记已记住的」重来；
2. **手动填写**：QQ 配置里的「主动消息目标」直接填 `openid`；
3. **发到群**：填「主动消息群」`group_openid`，填了就优先发群。

### 3.3 官方平台的规则（程序已按这些实现）

* **被动回复有效期 5 分钟**：回复时自动带上 `msg_id` / `msg_seq`；同一 `msg_id` 最多回复 5 次；
* **主动消息需要权限与额度**：未申请时平台会返回 `22009 / 30402` 之类的错误码，
  程序会把原因显示在「最近跳过原因」/日志里；
* **必须保持程序运行**：官方要求机器人网关在线才能发消息；
* **沙盒模式**：勾选后只对沙盒测试成员生效（域名切到 `sandbox.api.sgroup.qq.com`），适合先跑通流程；
* 语音/图片消息目前只处理文本内容（官方富媒体接口未接入）。

常见错误码（程序会翻译成人话）：

| 错误码 | 含义 |
|---|---|
| 100007 / 10004 | AppID 无效，或机器人状态异常（被封禁 / 已删除） |
| 100016 | AppID 或 AppSecret 不正确 |
| 11244 | 机器人未加入该群，或群聊消息权限不可用 |
| 22009 / 30402 | 主动消息额度用尽 / 未开通主动消息权限 |
| 50054 | 被动回复已过期（超过 5 分钟） |

---

## 4. 多机器人 + 多角色

一个「机器人」= 一个 QQ 官方机器人应用（一套 AppID / AppSecret），可以**绑定一个角色**。
于是同一个程序能同时扮演多个「机器人 + 角色」组合：

* 在**机器人**页面点「＋ 新增机器人」，填名称、AppID / AppSecret、绑定角色；
* 每个机器人的凭据、主动消息目标、绑定角色互相独立，互不串台；
* 第 1 个机器人存在 `qq:` 段，第 2..N 个存在 `bots:` 段（结构相同）；
* 聊天时想临时换角色：发 `#角色名 消息`；
* 停用某个机器人后，它的消息与主动消息都会被跳过，并在日志里给出原因。

---

## 5. 自检（不需要 QQ，也不需要真实 API Key）

```bat
:: 1) 单元 + 端到端自检（真实 Bot 进程 + mock 官方平台 / mock LLM）
python -m tests.smoke_test

:: 2) QQ 官方机器人通道自检（mock 凭证 / 网关 / REST 接口）
python -m tests.official_smoke

:: 3) 多机器人 / 多角色自检（两个机器人各自绑定一个角色，互不串台）
python -m tests.multibot_smoke

:: 4) 界面集成自检（真实 Bot + 真实界面，offscreen 模式）
python -m tests.gui_smoke

:: 5) 配置引导自检（首次运行弹引导 → 走完 → 跳过）
python -m tests.onboarding_smoke

:: 6) 调度器“到点自动发送”自检（约 2 分钟，慢速）
python -m tests.scheduler_live

:: 7) 打包产物自检（需要先执行 scripts\build.bat）
python -m tests.frozen_smoke

:: 8) 安装包自检（真的装一次、再卸一次；需要先执行 scripts\build_installer.bat 才能测成品）
python -m tests.installer_smoke
```

自检都用**本地 mock**（mock 官方平台：凭证接口 + REST 发送 + WebSocket 网关；mock LLM）复刻外部依赖，
因此不需要真实 QQ 机器人、不消耗 token。覆盖范围包括：角色卡解析（PNG/JSON/YAML）、触发规则
（定时/空闲/随机/活跃时段/免打扰/每日上限/最小间隔/概率/避免连续同一角色）、文本拆分与清理、
提示词替换、数据库读写、HTTP API、官方通道的被动回复（`msg_id`/`msg_seq`）与主动消息、
openid 记忆、WebSocket 事件推送、界面各页面与控件、配置引导、安装/卸载。

<!-- TEST-RESULTS -->
当前状态（本机实测，全部通过）：

| 自检 | 结果 |
|---|---|
| `python -m tests.smoke_test` | **176 项全部通过**（86 单元 + 90 端到端） |
| `python -m tests.official_smoke` | **54 项全部通过**（官方通道：凭证 / 网关 / 单聊 / 群聊 / 主动消息 / 重连） |
| `python -m tests.multibot_smoke` | **38 项全部通过**（两个官方机器人 + 两个角色，互不串台） |
| `python -m tests.gui_smoke` | **107 项全部通过**（含「关于」窗口、官方表单、七页裁切体检、超大 ID 回归） |
| `python -m tests.onboarding_smoke` | **84 项全部通过**（固定 6 步配置引导） |
| `python -m tests.scheduler_live` | **14 项全部通过**（定时触发到点自动发送） |
| `python -m tests.frozen_smoke` | **34 项全部通过**（打包产物；产物比源码旧时会自动跳过） |
| `python -m tests.installer_smoke` | **41 项全部通过**（真实安装包安装 / 卸载 + 主程序自卸载 + 旧版本运行时升级） |

合计 **548 项**。测试代码与产品代码同步演进，发现真实缺陷会先修产品再补断言。

---

## 6. 配置说明（`data/config.yaml`）

| 段 | 关键项 | 说明 |
|---|---|---|
| `app` | `onboarding_done` / `start_bot_on_launch` / `close_to_tray` / `autostart_with_windows` | 界面行为与自启 |
| `llm` | `base_url` / `api_key` / `model` / `max_tokens` / `temperature` / `fallback_messages` | 兼容 OpenAI 协议即可；`fallback_messages` 是生成失败时的兜底话术 |
| `qq` | `id` / `name` / `enabled` / `character_id` | 机器人身份与绑定角色（第 1 个机器人） |
| `qq.official` | `app_id` / `app_secret` / `sandbox` / `intents` / `target_openid` / `group_openid` / `allow_all_users` / `allowed_users` / `allowed_groups` / `markdown` / `max_reply_segments` / `reply_segment_max_len` | 官方机器人凭据、白名单与发送目标 |
| `bots` | 同上结构 | 第 2..N 个机器人 |
| `proactive` | `scheduled_times` / `idle_hours` / `random_*` / `active_hours` / `dnd_hours` / `global_daily_limit` / `min_interval_minutes` / `probability` | 主动消息触发与限流 |
| `memory` | `short_term_max` / `long_term_retrieve` / `auto_extract` | 记忆系统 |
| `api` | `host` / `port` / `token` | GUI ↔ Bot 的本机接口（`token` 非空时需带 `X-Tavern-Token`） |
| `logging` | `level` / `max_bytes` / `backup_count` | 日志滚动 |

`${ENV_VAR}` 可以引用环境变量（例如 `api_key: "${DEEPSEEK_API_KEY}"`）。
修改后 Bot 会在每个调度周期检查文件变化并热重载，也可在界面点「重载配置」。

环境变量（新名字 `BAIAI_*`，旧的 `QQAI_*` 仍然兼容）：

| 变量 | 作用 |
|---|---|
| `BAIAI_DATA_DIR`（旧 `QQAI_DATA_DIR`） | 重定向数据目录（`data/` 整体） |
| `BAIAI_CONFIG`（旧 `QQAI_CONFIG`） | 指定配置文件路径 |
| `BAIAI_HOME`（旧 `QQAI_HOME`） | 指定程序根目录 |
| `BAIAI_PAYLOAD` | 只给安装包自检用：指定 payload 目录 |
| `BAIAI_UNINSTALL_KEY` | 只给自检用：覆盖「卸载信息」的注册表项，避免影响机器上真实的安装 |

数据目录默认在 `<程序目录>\data`（绿色版）或 `%APPDATA%\BaiAi-Tavern`（安装版）；
如果机器上还留着改名前的 `%APPDATA%\QQ-AI-Tavern`，程序会继续用旧目录，不会让你丢配置。

---

## 7. 项目结构

```
bai-ai-tavern/
├── app/                    # GUI（PySide6）
│   ├── main.py             # 入口：单实例锁、主题、异常兜底，以及 --uninstall 卸载
│   ├── main_window.py      # 主窗口：侧边栏 + 页面堆叠 + 状态栏 + 引导入口
│   ├── onboarding.py       # 首次运行配置引导（固定 6 步，可跳过）
│   ├── about.py            # 「关于」窗口：名称/版本/作者 + 引用的开源项目（1. 2. 3. 带链接）
│   ├── tray.py             # 系统托盘：图标状态、右键菜单、气泡通知
│   ├── context.py          # 应用上下文：状态轮询、事件流、子进程编排
│   ├── api_client.py       # 调用 Bot 的 HTTP 客户端
│   ├── bot_process.py      # Bot 子进程管理
│   ├── llm_check.py        # 直接测试 LLM 端点 + 拉取上游模型列表
│   ├── llm_presets.py      # 内置常用 LLM 服务商预设
│   ├── icons.py / theme.py / uikit.py   # 图标绘制与深色主题
│   ├── config_store.py     # 配置读写（写前合并磁盘变更）
│   ├── autostart.py        # 开机自启（注册表）
│   ├── pages/              # 仪表盘 / 机器人 / 角色管理 / 主动消息 / 对话 / 设置 / 日志
│   └── widgets/            # 角色卡、状态灯、表单控件、LLM 配置控件、官方机器人配置控件
├── bot/                    # Bot 进程（FastAPI + APScheduler）
│   ├── main.py             # 入口：FastAPI 应用 + uvicorn + 启动/关闭钩子
│   ├── runtime.py          # 运行时容器（配置/数据库/引擎/多机器人/调度器）
│   ├── accounts.py         # BotAccount：一个官方机器人的网关、目标与绑定角色
│   ├── chat_router.py      # 「收消息 → 选角色 → 生成 → 发送」链路
│   ├── api.py              # 控制接口 /api/* 与 WebSocket /ws/events
│   ├── database/           # models.py 建表、crud.py 数据访问
│   ├── character_manager/  # loader.py 角色卡解析、registry.py 注册表
│   ├── memory/             # short_term.py 对话上下文、long_term.py 记忆检索
│   ├── ai_engine/          # prompt_builder.py 提示词、llm_client.py 调用、engine.py 编排
│   ├── scheduler/          # proactive.py 调度、triggers.py 触发规则
│   └── qq_official/        # client.py 凭证/REST、gateway.py 网关、receiver.py 事件、messaging.py 发送层
├── common/                 # GUI 与 Bot 共用：路径、配置、日志、文本、机器人配置解析
├── installer/              # 安装 / 卸载逻辑与向导（安装包 EXE 内部使用）
├── resources/              # 主题、图标、内置角色卡
├── scripts/                # build.bat / build_installer.bat / start.bat / 入口脚本 / 图标与版本信息生成
├── tests/                  # 自检：mock 官方平台与 LLM、角色卡工厂、端到端 / 界面 / 打包 / 安装
├── pyinstaller*.spec       # 三份打包配置（GUI / Bot / 安装包）
├── config.example.yaml     # 配置模板（含逐项注释）
└── 启动.bat                # 最终用户一键启动
```

> 实现说明：GUI 与 Bot 是两个进程，之间只有本机 HTTP/WebSocket（`api.port`，默认 8765）。
> 这样界面卡死或重启都不影响已建立的网关连接，也便于单独调试 Bot。

---

## 8. 主动消息机制

| 触发方式 | 实现 | 默认 |
|---|---|---|
| 定时触发 | APScheduler `CronTrigger`（每个时间点一个任务，带 90 秒抖动） | 09:00 / 21:00 |
| 空闲触发 | `IntervalTrigger` 周期检查“用户多久没说话” | 超过 6 小时，每 15 分钟检查 |
| 随机触发 | 在活跃时段内随机 `DateTrigger`，触发后重排下一次 | 关闭，间隔 120–300 分钟 |
| 手动触发 | 界面“立即触发一次”，跳过时间窗口/概率/上限 | — |

每次触发都会依次通过：**总开关 → 免打扰 → 活跃时段 →（空闲条件）→ 全局上限 → 最小间隔 → 概率**
→ 选出候选角色（过滤掉达上限的角色，并按需排除上次发言的角色，按“今日发言少者优先”加权）
→ 带上最近对话与相关记忆生成消息 → 按官方单条长度拆分发送 → 写入日志并推送事件到界面。

任何一步不满足都会**记录可读原因**（例如“当前处于免打扰时段 23:00-08:00”），
在“仪表盘 / 主动消息”页面直接可见，方便排查“为什么没发”。

---

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| 提示「尚未填写 AppID / AppSecret」 | 这是**官方机器人唯一的必需配置**：去 [q.qq.com](https://q.qq.com) 创建应用后填到「系统设置 → QQ」；填完点「测试连接」 |
| 测试连接报 `100016` | AppID 或 AppSecret 填错了（注意别把「机器人 QQ 号」当 AppID） |
| 测试连接报 `100007` | AppID 无效，或机器人状态异常（未上线 / 被封禁 / 已删除） |
| 网关连上了但收不到消息 | 官方平台要求先有人给机器人发消息；确认机器人已加好友（或已入群并 @ 它），且 `intents` 为 33554432 |
| 主动消息发不出去 | 官方「主动消息」需要申请权限与额度；界面上「最近跳过原因」会写明（如 `22009`）。另外必须先有 openid：让对方先发一条消息 |
| 界面提示「无法连接到 Bot 进程」 | 看 `data/logs/bot.log`；多为 `api.port` 被占用或缺少 `bot.exe` |
| LLM 报错/超时 | 用「系统设置 → 测试连接」定位；失败会自动重试，全部失败时使用兜底话术 |
| 「获取模型列表」失败 | 该网关可能不支持 `/models`（部分中转站）；先用「测试连接」确认 Key 与地址，再手动填模型名 |
| 托盘图标不见了 | Windows 会折叠托盘图标，展开即可；双击图标恢复窗口 |
| 想换数据位置 | 设置环境变量 `BAIAI_DATA_DIR=D:\BaiAi\data` 后启动 |
| 上下箭头/下拉三角/对勾的图标 | 由程序启动时绘制并缓存到 `data/cache/ui/v3/*.png`，删掉会自动重建 |
| 按钮图标 | 全部用 QPainter 画出来，不依赖字体里的符号字形（系统缺字形时 emoji 会变成方块） |
| 用任务管理器强杀了界面，Bot 还在跑 | 在任务管理器里结束 `bot.exe`，或重新打开界面后点“停止 Bot” |
| 安装完桌面没有图标 | 安装时勾选了「不创建桌面快捷方式」；重新运行安装包一次即可（覆盖安装，数据不动） |
| 安装时报「bot.exe: Permission denied」/「以下文件复制失败」 | 旧版本还在运行（界面或 `bot.exe` 占着文件）。新版安装程序会**自动关掉**它们；若仍失败，手动退出托盘里的程序（或结束任务管理器里的 `bot.exe`）后重试 |
| 卸载后想彻底清干净 | 卸载时（或 `--uninstall` 后再运行一次安装包）勾选/加 `--remove-data`；或手动删掉 `data` 目录与 `%APPDATA%\BaiAi-Tavern` |
| 旧版本升级后配置里还有 NapCat 字段 | 首次启动会自动清理并备份为 `config.yaml.bak`；程序已不再支持第三方协议 |

---

## 10. 已知限制

* **主动消息受官方平台限制**：需要申请权限，且有每日额度；未开通时只能被动回复。
* 官方接口按 `openid` 发送，因此「主动找谁」需要先有一次对话（或手动填 openid）。
* 长期记忆采用关键词重叠加权检索（不引入向量库），以换取小体积与低内存占用。
* 目前只提供深色主题。
* 事件推送（WebSocket）用于即时通知，界面状态仍以 3 秒轮询为主，二者互为兜底。
* 语音/图片等富媒体消息暂未接入，只处理文本。

---

## 11. 参与贡献与开源协议

* **开源协议**：[Apache License 2.0](LICENSE)（可自由使用、修改、分发，包括商用；
  需保留版权与许可声明，修改过的文件需注明改动）。
* **第三方组件**：见 [NOTICE](NOTICE)（PySide6/Qt 为 LGPLv3 动态链接使用，PyInstaller 打包受其例外条款覆盖）。
  程序内「关于」窗口也列出了完整的开源清单。
* **参与开发**：见 [CONTRIBUTING.md](CONTRIBUTING.md)（环境搭建、自检命令、代码约定、PR 流程）。
* **安全问题**：见 [SECURITY.md](SECURITY.md) —— 请不要在公开 Issue 里粘贴 API Key / AppSecret / openid。
* **本仓库不包含**：任何真实凭据、聊天记录、`data/`、`dist/`、`build/` 产物（见 `.gitignore`），
  也不包含 SillyTavern 的代码（只实现对其角色卡格式的读取）。

---

## 12. 开发提示

> **接手开发请先读 [HANDOFF.md](HANDOFF.md)**：架构图、文件地图、核心不变量、开发硬约束、
> 历史坑清单（每条都是真实踩过的）、自检体系与发版流程都在那里。

```bat
:: 只跑单元自检（不起服务）
python -m tests.smoke_test --unit-only

:: 单独启动 mock 官方平台 + mock LLM（默认 3100 端口）
python -m tests.mock_servers --port 3100

:: 以控制台方式运行 Bot（便于看实时日志）
.venv\Scripts\python.exe -m bot.main

:: 只打包其中一个
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean pyinstaller_bot.spec
```

日志：`data/logs/bot.log`（Bot，含 uvicorn 与网关日志）、`data/logs/gui.log`（界面）、
`data/logs/bot_stdout.log`（Bot 控制台输出）。数据库：`data/bot.db`（SQLite，WAL 模式）。
角色卡与头像：`data/characters/`。

「关于」窗口里列出了本程序引用的全部开源项目（按 1. 2. 3. 编号并附主页链接），
点「复制开源清单」可以整份复制走；建议在分发/二次开发时保留。
