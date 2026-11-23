---
id: B_20261117_2001_deferred-memory-failure
title: 延迟调用记忆工具时传播失败状态
type: B
author: ToNy_St4rk
author_email: dengjiahao9@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/rails
files:
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - jiuwenswarm/agents/harness/common/memory/jiuwen_rail_builder.py
designs: []
design_note: 修复 Issue 6151 中已有工具结果契约的状态传播，无独立设计。
---

# 延迟调用记忆工具时传播失败状态

[Issue #6151](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6151) 报告记忆操作失败后，Web 仍显示绿色“完成”。

提交 2bd1a7ae2 在记忆 Provider 接入层为旧版错误对象补充 `success: false`。直接调用已正确生成失败事件。通过 `tool_call` 调用时，包装层的 `success: true` 表示调度成功，目标操作的失败保留在 `data.result`。事件层优先读取包装层状态，导致前端误判。

事件层现在检查目标结果对象的显式失败，沿用工具结果事件的 `success: false`、`status: "error"` 和 `is_error: true`。中英文 E2A 协议记录该行为。成功、等待中以及普通文本返回的判定沿用已有逻辑。

在 Windows 上使用锁定的 agent-core 2f4bedb9c 验证。模拟 SDK 返回旧版错误，经实际记忆 Rail 注册的工具直接调用及经实际 `ToolCallTool` 调用，两条路径均生成失败事件。回归覆盖成功、等待中以及显式失败的目标结果。原 Issue 的 wheel 与真实记忆服务环境尚未复测。
