---
title: 当前规约 S索引
author: 鍒樼憸
author_email: liuyu524@huawei.com
generated: true
---

# 当前规约 S索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [jiuwenswarm/agents/harness/common/rails](#module-1) |
| [jiuwenswarm/agents/harness/common/tools](#module-2) |
| [jiuwenswarm/channels/desktop](#module-3) |
| [jiuwenswarm/channels/web/frontend](#module-4) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-5) |
| [jiuwenswarm/server/supervisor](#module-6) |
| [jiuwenswarm/server/worker](#module-7) |
| [未分类](#module-8) |

<a id="module-1"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_tool-discovery-language](jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |

<a id="module-2"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_tool-discovery-language](jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |

<a id="module-3"></a>

## jiuwenswarm/channels/desktop

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_worker-lifecycle](jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |

<a id="module-4"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_app-agent-interaction](mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |

<a id="module-5"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_app-agent-interaction](mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |
| [S_01_tool-discovery-language](jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |

<a id="module-6"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_worker-lifecycle](jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |

<a id="module-7"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_worker-lifecycle](jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md) | S | Runtime Worker 生命周期与恢复规约 | 刘瑜 | jiuwenswarm/server/supervisor, jiuwenswarm/server/worker, jiuwenswarm/channels/desktop | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/supervisor/resident_lifecycle.py, jiuwenswarm/server/supervisor/shutdown.py | — |

<a id="module-8"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_multimodal-tool-loading](jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) | S | Work / Code 多模态工具加载契约 | 待确认 | — | — | — |
