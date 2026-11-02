---
title: 实施与决策 F索引
author: alan_cheng
author_email: chengshuo5@huawei.com
generated: true
---

# 实施与决策 F索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 3 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [gateway/channel_manager/web](#module-1) | 1 |
| [gateway/enterprise/config/manager_config](#module-2) | 1 |
| [gateway/storage_assembly](#module-3) | 1 |
| [jiuwenswarm/channels/web/frontend](#module-4) | 1 |
| [jiuwenswarm/enterprise/config](#module-5) | 1 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-6) | 1 |

<a id="module-1"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |

<a id="module-2"></a>

## gateway/enterprise/config/manager_config

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261102_1011_gateway-memory-task-memory](F_20261102_1011_gateway-memory-task-memory.md) | F | 4.6 解决 memory 和 task memory 的 Gateway 解耦问题 | ruiqiang2 | gateway/enterprise/config/manager_config, gateway/storage_assembly, jiuwenswarm/enterprise/config | gateway/enterprise/config/manager_config/core/application_config/memory_config.py, gateway/enterprise/config/manager_config/core/application_config/task_memory_config.py, gateway/storage_assembly/setup.py, jiuwenswarm/enterprise/config/config_provider.py | 无独立 D：用户于 2026-11-02 明确授权 4.6 memory/task memory 解耦，沿用既有通知和配置读取能力，无独立设计文档。 |

<a id="module-3"></a>

## gateway/storage_assembly

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261102_1011_gateway-memory-task-memory](F_20261102_1011_gateway-memory-task-memory.md) | F | 4.6 解决 memory 和 task memory 的 Gateway 解耦问题 | ruiqiang2 | gateway/enterprise/config/manager_config, gateway/storage_assembly, jiuwenswarm/enterprise/config | gateway/enterprise/config/manager_config/core/application_config/memory_config.py, gateway/enterprise/config/manager_config/core/application_config/task_memory_config.py, gateway/storage_assembly/setup.py, jiuwenswarm/enterprise/config/config_provider.py | 无独立 D：用户于 2026-11-02 明确授权 4.6 memory/task memory 解耦，沿用既有通知和配置读取能力，无独立设计文档。 |

<a id="module-4"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-5"></a>

## jiuwenswarm/enterprise/config

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261102_1011_gateway-memory-task-memory](F_20261102_1011_gateway-memory-task-memory.md) | F | 4.6 解决 memory 和 task memory 的 Gateway 解耦问题 | ruiqiang2 | gateway/enterprise/config/manager_config, gateway/storage_assembly, jiuwenswarm/enterprise/config | gateway/enterprise/config/manager_config/core/application_config/memory_config.py, gateway/enterprise/config/manager_config/core/application_config/task_memory_config.py, gateway/storage_assembly/setup.py, jiuwenswarm/enterprise/config/config_provider.py | 无独立 D：用户于 2026-11-02 明确授权 4.6 memory/task memory 解耦，沿用既有通知和配置读取能力，无独立设计文档。 |

<a id="module-6"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
