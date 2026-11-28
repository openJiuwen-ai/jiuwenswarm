---
id: B_20261124_1717_main-agent-template-trace
title: 企业版 Work、Code 的主 Agent Trace 显示默认名
type: B
author: 垚垚
author_email: jane@JaneMacBook.local
modules:
- jiuwenswarm/agents/harness
- jiuwenswarm/server/runtime/agent_adapter
files:
- jiuwenswarm/agents/harness/agent_observability.py
- jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
- tests/unit_tests/agentserver/test_main_agent_template_trace.py
designs: []
design_note: 本次修复企业版 Work、Code 主 Agent 的 Trace 展示名，沿用现有会话配置与观测接口，无独立设计文档。
---

# 企业版 Work、Code 的主 Agent Trace 显示默认名

关联缺陷为 [jiuwenclaw #228](https://gitcode.com/ZYQ5333/jiuwenclaw/issues/228)。

## 根因与修复

预热会话在获得请求的 `bot_id` 之前就可能创建主 Agent。运行时用配置名初始化 `AgentCard.name`，观测 rail 在执行时读取该字段。授权资源的显示名与客户配置的模板名可以不同。

企业版 Work、Code 在采集当前轮次的 Trace 时，复用该会话已加载的企业配置。请求 `bot_id` 与配置的 `resource_id` 匹配时，主 Agent 从这份配置的 `agent_template.template_name` 取展示名，绑定到该轮次的观测 rail。缺少匹配的配置或模板名时，观测 rail 沿用运行时名称。命名无需额外读取数据库，运行时使用原有 `AgentCard.name`、`_agent_name` 和 `card.id`。显式 Agent 定义、自定义 Spec、显式指定 `agent_name` 的实例、Team 和委派的子 Agent 以各自的名称上报。

## 验证

本仓定向回归通过 186 项，其中 18 项覆盖 Trace 命名。离线验证使用模拟的模板记录和模型响应，经真实 SDK 导出 8 条 Trace、48 个 span，再用本机 Insight 的 `aggregateJiuwenOtlp` 解析。验证了主 Agent 的模板名、子 Agent 自身的名称与父子归属，以及快照缺失、资源不匹配时的回退。同时核对了运行时的 `AgentCard.name`，确认命名没有新增 I/O。

## 评审回归

SDK 每个会话只登记最近打开的根 span。同一会话的两轮执行重叠时，缺少本轮上下文的回调曾读到另一轮的模板名。适配器现在在实际打开根 span 前登记本轮，执行和清理结束后退出。有本轮根 span 的回调仍可使用该轮模板名。只有会话 ID 的回调在重叠期间沿用运行时名称，直到这些轮次全部退出。开 span 前已返回的请求不会影响在途轮次。

模板取名遇到异常输入时跳过。企业专用观测 Rail 无法构造时，适配器改用 SDK 基础 Rail。评审后的 28 项专项用例覆盖首次执行、无路由元数据、跨轮次和跨会话隔离、提前返回及基础 Rail 降级。连同相关回归，本仓通过 107 项。离线 OTLP 再次验证了 8 条 Trace、48 个 span 的模板名、子 Agent 归属和零额外配置查询。

## Agent 模板名称对齐

Manager 在授权上下文中按配置模板与运行时模板的组合返回 `agent_definition.agent_name`。同一个配置模板可能对应多个 Agent 定义，因此按配置快照取得的 `agent_template.template_name` 有时和用户选择的 Agent 名称不同。

Gateway 使用握手凭据查询授权上下文，精确匹配集群、用户、组织和 `bot_id`。同一 WebSocket 上同一上下文的并发请求共享一次有超时的查询。名称通过已有请求扩展载体传递，Full 模式经内部 HTTP 头传给 AgentServer。Work 和 Code 只在本轮主 Agent span 使用经校验的名称。名称缺失、无效或与请求的 `bot_id` 不匹配时，恢复到首次 Trace 命名修复前（`edef87c95`）的 SDK 运行时名称解析。首轮能取得授权上下文时即可使用 Agent 模板名称。

基于 Gateway `97ccf549` 和 Swarm `fcc63309f` 的本地候选，Python 3.12 定向回归分别通过 78 项和 92 项。Gateway 测试从 WebSocket 消息构造进入真实 E2A 和 REST 映射，覆盖两类握手凭据、授权上下文匹配、并发复用及失败回退。Swarm 测试从真实 REST 头还原名称，验证 Work、Code 首轮同步和流式执行的 span 属性，覆盖未加载配置、已加载配置模板时仍回退到运行时名称，以及 Team 命名。测试使用模拟 Manager 响应，客户环境部署仍待验证。
