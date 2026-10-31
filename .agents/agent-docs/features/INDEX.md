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
| [gateway/enterprise](#module-2) | 1 |
| [jiuwenswarm/channels/web/frontend](#module-3) | 1 |
| [jiuwenswarm/extensions/runtime_management_extension](#module-4) | 1 |
| [jiuwenswarm/server](#module-5) | 1 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-6) | 1 |

<a id="module-1"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |

<a id="module-2"></a>

## gateway/enterprise

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1541_gateway-hub-catalog-e2a](F_20261031_1541_gateway-hub-catalog-e2a.md) | F | 企业管理 Hub 目录查询经 E2A 交由 Server 执行 | ruiqiang2 | gateway/enterprise, jiuwenswarm/server, jiuwenswarm/extensions/runtime_management_extension | gateway/enterprise/config/manager_config/routers/application_config_routers.py, gateway/enterprise/plane.py, jiuwenswarm/server/handlers/hub_catalog.py, jiuwenswarm/server/dispatch.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/extensions/runtime_management_extension/runtime_routed_client.py, jiuwenswarm/extensions/runtime_management_extension/pod_rest_map.py, tests/unit_tests/gateway/test_hub_catalog_e2a.py | 无独立 D：用户确认的 Gateway 解耦任务 4.5，采用现有无会话 E2A 管理通路，无独立设计编号。 |

<a id="module-3"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-4"></a>

## jiuwenswarm/extensions/runtime_management_extension

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1541_gateway-hub-catalog-e2a](F_20261031_1541_gateway-hub-catalog-e2a.md) | F | 企业管理 Hub 目录查询经 E2A 交由 Server 执行 | ruiqiang2 | gateway/enterprise, jiuwenswarm/server, jiuwenswarm/extensions/runtime_management_extension | gateway/enterprise/config/manager_config/routers/application_config_routers.py, gateway/enterprise/plane.py, jiuwenswarm/server/handlers/hub_catalog.py, jiuwenswarm/server/dispatch.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/extensions/runtime_management_extension/runtime_routed_client.py, jiuwenswarm/extensions/runtime_management_extension/pod_rest_map.py, tests/unit_tests/gateway/test_hub_catalog_e2a.py | 无独立 D：用户确认的 Gateway 解耦任务 4.5，采用现有无会话 E2A 管理通路，无独立设计编号。 |

<a id="module-5"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1541_gateway-hub-catalog-e2a](F_20261031_1541_gateway-hub-catalog-e2a.md) | F | 企业管理 Hub 目录查询经 E2A 交由 Server 执行 | ruiqiang2 | gateway/enterprise, jiuwenswarm/server, jiuwenswarm/extensions/runtime_management_extension | gateway/enterprise/config/manager_config/routers/application_config_routers.py, gateway/enterprise/plane.py, jiuwenswarm/server/handlers/hub_catalog.py, jiuwenswarm/server/dispatch.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/extensions/runtime_management_extension/runtime_routed_client.py, jiuwenswarm/extensions/runtime_management_extension/pod_rest_map.py, tests/unit_tests/gateway/test_hub_catalog_e2a.py | 无独立 D：用户确认的 Gateway 解耦任务 4.5，采用现有无会话 E2A 管理通路，无独立设计编号。 |

<a id="module-6"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
