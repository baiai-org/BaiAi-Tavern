# 改动说明

<!-- 一句话说明这个 PR 做了什么 -->

## 为什么

<!-- 解决的问题 / 关联的 Issue（Fixes #123） -->

## 怎么改的

<!-- 关键实现点，便于 review；涉及界面请附截图 -->

## 自检结果

<!-- 勾选并附上输出末尾的「N 项通过，M 项失败」 -->

- [ ] `python -m pyflakes app bot common installer scripts tests`
- [ ] `python -m tests.smoke_test --unit-only`
- [ ] `python -m tests.smoke_test`
- [ ] `python -m tests.official_smoke`
- [ ] `python -m tests.gui_smoke`（改了界面时）
- [ ] `python -m tests.installer_smoke`（改了安装/打包时）

## 检查清单

- [ ] 代码保持 Python 3.9 兼容（`from __future__ import annotations`，未使用 `X | Y`）
- [ ] 新增/修改的行为有对应的自检断言
- [ ] 没有提交任何真实凭据、日志、`data/`、`dist/`、`build/`
- [ ] `.bat` 文件仍是 CRLF 换行
- [ ] 文档（README / CHANGELOG）已同步
