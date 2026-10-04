# 参与贡献

感谢你愿意花时间改进 BaiAi-Tavern！下面是最小必要的信息。

> 想先了解整体设计（架构、不变量、历史坑、发版流程），请看 **[HANDOFF.md](HANDOFF.md)**。

## 开发环境

```bat
:: Windows 10/11 + Python 3.10+（3.9 也能跑，代码保持 3.9 兼容）
git clone <你的仓库地址>
cd BaiAi-Tavern
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 启动（会自动生成 data/config.yaml）
scripts\start.bat
```

不需要真实的 QQ 机器人或 LLM Key 也能开发：全部自检都跑在本地 mock 上。

## 提交前请自检

```bat
.venv\Scripts\python.exe -m pyflakes app bot common installer scripts tests   :: 静态检查
.venv\Scripts\python.exe -m tests.smoke_test --unit-only                      :: 快速（约 10 秒）
.venv\Scripts\python.exe -m tests.smoke_test                                 :: 端到端（含 mock 官方平台）
.venv\Scripts\python.exe -m tests.official_smoke
.venv\Scripts\python.exe -m tests.gui_smoke
```

改了打包脚本或安装逻辑，再跑：

```bat
scripts\build.bat
scripts\build_installer.bat
.venv\Scripts\python.exe -m tests.frozen_smoke
.venv\Scripts\python.exe -m tests.installer_smoke
```

> 自检都是「可执行的检查器」：`python -m tests.<名字>`，不依赖 pytest。
> 每条检查都会打印 `[PASS]` / `[FAIL]`，末尾给出通过项数；有失败时退出码非 0。

## 代码约定

* **Python 3.9 兼容**：使用 `from __future__ import annotations`，不要用 `X | Y` 类型写法。
* 中文注释与中文用户可见文案；字符串格式化统一用 `%`。
* 界面代码不要用 emoji/符号字形（系统缺字形会变方块），图标一律用 `app/uikit.py` 绘制。
* 所有副作用（窗口、定时器、Qt 信号、外部进程）都必须可回收，随页面/控件销毁一起清理。
* 任何写到磁盘或发到外部接口的字段都要考虑空值、超大整数、`nan/inf`（见 `app/qt_safe.py`）。
* `.bat` 文件必须保持 **CRLF** 换行（仓库已用 `.gitattributes` 固定）。

## 提交 PR

1. 从 `main` 开一个功能分支：`git checkout -b feature/你的改动`；
2. 保持提交粒度清晰，提交信息写清楚「改了什么、为什么」；
3. PR 描述里附上：改动目的、影响范围、**跑过哪些自检以及结果**；界面相关请附截图；
4. 新增功能请同步补自检断言（见 `tests/`）。

## 安全问题

不要在 Issue / PR 里粘贴真实的 API Key、AppSecret、openid 或聊天记录。
凭据泄露请按 `SECURITY.md` 的方式私下告知。

## 协议

提交贡献即表示你同意以 **Apache-2.0** 协议授权你的贡献（见 `LICENSE`）。
