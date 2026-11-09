---
id: B_20261110_0017_cua-repl-reset
title: 为原生桌面 REPL 提供显式会话恢复入口
type: B
author: layShang
author_email: zhanghaibo40@h-partners.com
modules:
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/rails
files:
  - jiuwenswarm/agents/harness/common/tools/cua_runtime.py
  - jiuwenswarm/agents/harness/common/tools/cua_repl.py
  - jiuwenswarm/agents/harness/common/rails/cua_extension_rail.py
  - tests/unit_tests/agentserver/test_cua_repl_recovery.py
  - tests/integration/test_cua_repl_session.py
designs: []
design_note: 补齐已依赖 SDK 的显式 reset 契约，不改变任务规划或引入自动重试，无独立设计。
---

# 为原生桌面 REPL 提供显式会话恢复入口

## 现象与影响

桌面读取超时或原生清理不确定后，SDK 可能退役当前原生会话。后续执行返回
`Native session is retired`，或要求调用 `computer_use_reset`。WorkSwarm 原来只注册
`cua_repl`，代理没有对应的恢复工具；换一段代码继续观察仍可能使用失效会话。

核对基线为 WorkSwarm develop `550bc92ba62b587e0d1b6f48cb1704be57f40ccd`，
agent-core 固定依赖 `f3f8f576164cc405c046e43079c8ad00ee17f0c4`，
打包用 computer-use 固定依赖 `65f3e67cd670ea727086d4376f582771ccc4d2d4`。
故障注入使用该 SDK 的 `Runtime` 和 `CallLifecycle`：退役后连续两次执行返回错误，
WorkSwarm 包装器未关闭，也未提供重置入口。该结果证明接口缺口，不代表已在新版真实桌面复现全部慢任务。

## 关联设计与当前契约

沿用 computer-use SDK 的 `ExecutionSession.execute/reset/close`，会话及原生进程清理由
SDK 负责。`reset` 成功后下次执行使用新的 worker；原来的 Python 变量、窗口/元素句柄及观察失效。
依据为固定版本 `interface_adapter/mcp/session.py`、`runtime.py` 与
`computer_use_engine/native/lifecycle.py`。不复制 core 或 SDK 的内部状态机到 WorkSwarm。

## 根因与修复决策

- 普通 SDK 执行错误通过 `isError` 返回，而非抛出 Python 异常；原包装器的异常关闭分支无法区分
  会话退役与普通代码错误。SDK 当前没有向 WorkSwarm 提供可可靠判别退役的结构化字段。
- 注册独立的无参数工具 `cua_repl_reset`，通过 SDK 的公开 `reset` 接口恢复。
  工具不接受代码、不回放动作、不撤销已完成的界面操作。成功后要求重建变量并重新观察。
- 保留普通 Python 错误后的变量；不把所有 `isError` 都当成会话损坏，也不按错误文本匹配做自动重置。
- 原生清理未确认时阻止 REPL 输入；清理状态按所属 runtime 记录，只有对应会话确认清理后才移除。
  继续使用桌面串行锁及委派生命周期，任务关闭不能被重置重新打开。
- 本次只补会话恢复，不改模型、默认轮数、委派超时、底层驱动、依赖锁或官方平台支持范围。

## 回归证据与适用范围

环境为 Python 3.12.16、上述固定 Core/SDK 版本；从候选工作区运行 pytest，未运行格式化或静态检查。

- 干净基线的三个生产模块加相同新增回归：12 failed，原因是缺少 reset 接口和注册工具。
  另以真实 SDK `CallLifecycle`/`Runtime` 故障注入确认会话退役后仍复用；未触碰桌面。
- 首轮候选：新增生命周期单元 12 项、真实 SDK worker 集成 2 项和现有 CUA wiring 38 项，
  合计 52 passed。普通错误保留变量；显式 reset 后旧 worker 退出、变量不存在、新 worker 可继续执行；
  `close` 可在 3 秒内打断等待中的执行，无须等待 45 秒 watchdog。
- 独立检视发现取消期间清理异常覆盖 `CancelledError`，已修复并增加关闭成功/失败两项回归。
  修订后 14 项单元加 2 项真实 worker 集成，16 passed；检视方独立重跑新增取消用例，2 passed。
  38 项原装配回归的受测装配路径未再改变。
- 通过真实 Core `create_cua_agent`、Rail 初始化和 `AbilityManager.list_tool_info` 检查，
  模型工具列表含 `cua_repl` 与 `cua_repl_reset`，后者 schema 不接受代码。

测试入口：`tests/unit_tests/agentserver/test_cua_repl_recovery.py`、
`tests/integration/test_cua_repl_session.py`、`tests/unit_tests/agentserver/test_cua_subagent_wiring.py`。
SDK 集成测试在没有可选打包依赖时明确 skip；本次安装固定 SDK 后实际运行通过，未 skip。
这些测试未启动原生驱动，不涉及 GUI 或模型请求。

冻结包及真实模型 Finder 任务另行验证；本记录不把模拟会话测试称为端到端任务验收。
本机为 Intel macOS 实验环境，官方 README 的 macOS 支持范围为 ARM64。

## 防错模式与资料同步

保持“动作派发”“业务完成”“会话仍可用”三种状态的区别。恢复入口先确认资源清理，
然后重新观察副作用；不能因为前一次返回失败就重放完整动作序列。
中英文桌面自动化文档同步说明恢复工具、变量失效和平台入口。

## 遗留与后续

历史 Finder 任务的主要耗时还涉及无业务进展的重命名试探、大量模型往返和跨委派丢失有效策略。
这个补丁不提供业务进度判定、整体时间预算或自动策略沉淀，不据此宣称解决了全部长耗时问题。
Windows legacy 单步工具不经过本 REPL 的桌面锁；本次清理阻塞保证仅适用于 REPL 路径。
后续修改 SDK 生命周期、工具注册或清理并发语义时，应重跑相应回归和实际冻结包验证。
