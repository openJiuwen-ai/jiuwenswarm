---
id: B_20261124_2201_rea-npx-readiness
title: 系统 Node 安装后 REA 被误判未就绪并反复回到一键安装
type: B
author: xiaotian077
author_email: zhongxiaotian@huawei.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/rea_setup.py
  - tests/unit_tests/common/test_rea_setup.py
designs: []
design_note: 对既有 REA 安装就绪判定的局部缺陷修复，不新增架构方案。
---

# 系统 Node 安装后 REA 被误判未就绪修复记录

## 现象与影响

使用标准 Unix 前缀布局的系统 Node.js 时，REA MCP 已成功连接并返回 229 个工具，
`state.json` 也已标记 `state=connected`，但安装任务最终仍被判失败。前端应用刷新后的
`scan.ready=false`，随即离开安装进度页，重新显示“您还没有安装逆向工程环境 / 一键安装”。

用户会认为基础能力没有安装成功并反复重试，但每次都只是读取状态时的就绪判定失败。

## 关联设计与当前契约

无独立设计。现有契约要求：

- 安装器可以把 MCP 启动方式写成 `node + npx-cli.js`，也可以写成 `npx`。
- 最终就绪检查必须识别安装器实际写入的启动方式。
- 标准 Node 前缀布局的 npm 文件位于 `<prefix>/lib/node_modules`，Windows 布局位于
  `node.exe` 同级的 `node_modules`。

## 根因与修复决策

`resolve_node_runtime()` 只探测了 `<node-dir>/node_modules/npm/bin/npx-cli.js`，漏掉了
Unix 标准布局的 `<prefix>/lib/node_modules/npm/bin/npx-cli.js`。当 `npx-cli.js` 未被识别时，
`_rea_record()` 会退回写入 `command=<prefix>/bin/npx`。

之后 `_mcp_registration_check()` 要求记录中的 command 必须等于 `runtime.node`。因此即使
MCP 临时连接成功、工具数量正确且记录已标记 connected，最终 `rea_mcp.ok` 仍被改写为 false，
`scan.ready` 随之为 false。安装任务把该结果判为失败，前端落入 empty 状态。

修复决策：

1. 优先解析 `npx` 符号链接的真实目标，同时兼容 Unix `lib/node_modules` 和原有同级
   `node_modules` 布局。
2. npm 版本从解析后的 `npx-cli.js` 所在 npm 目录读取。
3. MCP 就绪检查接受安装器可能写入的 `node` 或 `npx` 两种规范启动方式，并统一解析路径，
   避免 `/var` 与 `/private/var` 等路径别名造成误判。

未选择只放宽 MCP 检查：这能消除当前误判，但新的环境仍会写入脆弱的裸 `npx` 记录。同时修复
路径探测可让后续安装使用固定的 `node + npx-cli.js` 启动方式。

## 回归证据与适用范围

候选基线为原始 `upstream/develop` 的 `5a875665f`，修复位于独立分支
`codex/rea-readiness-fix-20261124`。

验证结果：

- 标准 Unix `bin + lib/node_modules` 布局能解析到真实 `npx-cli.js`。
- 仅有 `command=npx` 的旧连接记录现在返回 `ready=true`、`mcp_state=connected`。
- 原有 Windows 风格安装记录测试继续通过，写入形态仍为 `node + npx-cli.js`。
- 当前已安装的 `rea-beta4-mac` 实例扫描结果为 `ready=true`，三项基础检查全部 ready。
- 定向 pytest：5 项通过。
- `git diff --check`：通过。

Windows 的 `node.exe` 同级 `node_modules` 探测顺序保持兼容；本次修改没有引入平台分支或
新的 Windows 安装步骤。

## 防错模式与资料同步

安装完成后的最终就绪判定必须同时覆盖安装器可能产生的规范记录形态，不能把一种启动方式
当作唯一权威。路径身份比较应先规范化路径，避免符号链接和挂载别名导致误判。

## 遗留与后续

前端本地缓存中的旧 `scan.ready=false` 仍可能让页面在升级后首次进入时短暂显示旧状态；
本次修复不改变前端缓存策略。REA 页面后续可在挂载时静默刷新一次状态。
