---
title: 当前规约 S索引
author: xinyang_bali
author_email: 835397263@qq.com
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
| [jiuwenswarm/channels/web/frontend](#module-3) |
| [jiuwenswarm/channels/web/frontend/src/features/settings](#module-4) |
| [jiuwenswarm/common](#module-5) |
| [jiuwenswarm/extensions/sdk](#module-6) |
| [jiuwenswarm/resources/plugins/ttse](#module-7) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-8) |
| [未分类](#module-9) |

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

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_app-agent-interaction](mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |

<a id="module-4"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_affinity-settings](jiuwenswarm/common/kv_cache_affinity_config/S_01_affinity-settings.md) | S | 算力亲和设置与模型能力契约 | MengHan18 | jiuwenswarm/common, jiuwenswarm/channels/web/frontend/src/features/settings | jiuwenswarm/common/kv_cache_affinity_config.py, jiuwenswarm/common/config.py | — |

<a id="module-5"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_affinity-settings](jiuwenswarm/common/kv_cache_affinity_config/S_01_affinity-settings.md) | S | 算力亲和设置与模型能力契约 | MengHan18 | jiuwenswarm/common, jiuwenswarm/channels/web/frontend/src/features/settings | jiuwenswarm/common/kv_cache_affinity_config.py, jiuwenswarm/common/config.py | — |

<a id="module-6"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_03_native-plugin-runtime](jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-7"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_02_ttse-plugin-runtime](jiuwenswarm/server/runtime/agent_adapter/S_02_ttse-plugin-runtime.md) | S | TTSE Coding 插件运行契约 | xinyang_bali | jiuwenswarm/resources/plugins/ttse | jiuwenswarm/resources/plugins/ttse/rails/ttse_plugin_rail.py, jiuwenswarm/resources/plugins/ttse/manifest.json | — |

<a id="module-8"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_app-agent-interaction](mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |
| [S_01_tool-discovery-language](jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |
| [S_03_native-plugin-runtime](jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-9"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_01_multimodal-tool-loading](jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) | S | Work / Code 多模态工具加载契约 | 待确认 | — | — | — |
