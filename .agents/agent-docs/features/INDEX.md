---
title: 实施与决策 F索引
author: xinyang_bali
author_email: 835397263@qq.com
generated: true
---

# 实施与决策 F索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [gateway/channel_manager/web](#module-1) |
| [jiuwenswarm/agents/harness](#module-2) |
| [jiuwenswarm/agents/harness/common/rails](#module-3) |
| [jiuwenswarm/agents/harness/common/tools](#module-4) |
| [jiuwenswarm/channels/web/frontend](#module-5) |
| [jiuwenswarm/extensions/sdk](#module-6) |
| [jiuwenswarm/resources/plugins/ttse](#module-7) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-8) |

<a id="module-1"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |

<a id="module-2"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-3"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-4"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-5"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-6"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-7"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-8"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
