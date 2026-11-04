---
title: 实施与决策 F索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

# 实施与决策 F索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [agent-gateway/agent_gateway/channel_manager/tui](#module-1) |
| [agent-gateway/agent_gateway/common](#module-2) |
| [gateway/channel_manager/web](#module-3) |
| [jiuwenswarm](#module-4) |
| [jiuwenswarm/agents/harness](#module-5) |
| [jiuwenswarm/agents/harness/common/rails](#module-6) |
| [jiuwenswarm/agents/harness/common/tools](#module-7) |
| [jiuwenswarm/channels/tui/frontend](#module-8) |
| [jiuwenswarm/channels/web/frontend](#module-9) |
| [jiuwenswarm/extensions/sdk](#module-10) |
| [jiuwenswarm/resources/plugins/ttse](#module-11) |
| [jiuwenswarm/runtime](#module-12) |
| [jiuwenswarm/server](#module-13) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-14) |
| [jiuwenswarm/server/runtime/session](#module-15) |

<a id="module-1"></a>

## agent-gateway/agent_gateway/channel_manager/tui

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |

<a id="module-2"></a>

## agent-gateway/agent_gateway/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |

<a id="module-3"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |

<a id="module-4"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261030_2018_teammate-swarmflow-adaptation](F_20261030_2018_teammate-swarmflow-adaptation.md) | F | 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配） | @xiajing | jiuwenswarm | jiuwenswarm/agents/harness/team/handlers/workflow_state.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py | 无独立 D：本仓侧为 teammate 接入 swarmflow 的适配与硬化，无独立设计文档。 |

<a id="module-5"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-6"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-7"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-8"></a>

## jiuwenswarm/channels/tui/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |

<a id="module-9"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-10"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-11"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-12"></a>

## jiuwenswarm/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |

<a id="module-13"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |

<a id="module-14"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-15"></a>

## jiuwenswarm/server/runtime/session

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_0545_tui-session-mode-switch-phase1](F_20261103_0545_tui-session-mode-switch-phase1.md) | F | TUI 同一会话模式切换第一阶段 | Yanqing-Shi | jiuwenswarm/channels/tui/frontend, agent-gateway/agent_gateway/channel_manager/tui, agent-gateway/agent_gateway/common, jiuwenswarm/runtime, jiuwenswarm/server, jiuwenswarm/server/runtime/session | jiuwenswarm/channels/tui/frontend/src/app-state.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts, jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts, jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts, jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts, jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs, jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs, jiuwenswarm/channels/tui/frontend/package.json, agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py, agent-gateway/agent_gateway/common/protocol_compat.py, agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/runtime/service.py, jiuwenswarm/server/runtime/session/session_metadata.py, jiuwenswarm/server/runtime/session/session_history.py, tests/unit_tests/runtime/test_session_mode_switch.py, tests/unit_tests/server/test_archive_lifecycle.py, pyproject.toml, uv.lock | [D-20261101-1139-tui-session-mode-switch](../designs/proposals/D-20261101-1139-tui-session-mode-switch/design.md) |
