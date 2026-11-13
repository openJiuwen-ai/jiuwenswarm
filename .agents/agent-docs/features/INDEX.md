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
| [deploy/yuanrong](#module-1) |
| [gateway/channel_manager/web](#module-2) |
| [jiuwenswarm](#module-3) |
| [jiuwenswarm/agents/harness](#module-4) |
| [jiuwenswarm/agents/harness/common/rails](#module-5) |
| [jiuwenswarm/agents/harness/common/tools](#module-6) |
| [jiuwenswarm/channels/web/frontend](#module-7) |
| [jiuwenswarm/common](#module-8) |
| [jiuwenswarm/extensions/sdk](#module-9) |
| [jiuwenswarm/resources](#module-10) |
| [jiuwenswarm/resources/plugins/ttse](#module-11) |
| [jiuwenswarm/server](#module-12) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-13) |
| [scripts](#module-14) |
| [tests](#module-15) |

<a id="module-1"></a>

## deploy/yuanrong

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-2"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |

<a id="module-3"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261030_2018_teammate-swarmflow-adaptation](F_20261030_2018_teammate-swarmflow-adaptation.md) | F | 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配） | @xiajing | jiuwenswarm | jiuwenswarm/agents/harness/team/handlers/workflow_state.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py | 无独立 D：本仓侧为 teammate 接入 swarmflow 的适配与硬化，无独立设计文档。 |

<a id="module-4"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261111_1528_unified-observability-config](F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-5"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-6"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |

<a id="module-7"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [F_20261113_1113_method-decoupling](F_20261113_1113_method-decoupling.md) | F | E2A method 字符串化并回归 Swarm 解析 | mingo007 | jiuwenswarm/common, jiuwenswarm/server, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/server/request_policy.py, jiuwenswarm/server/front/router.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/channels/web/frontend/src/services/webHttpClient.ts, tests/unit_tests/common/test_request_method_policy.py, tests/unit_tests/common/test_req_method_contract.py, tests/unit_tests/gateway/method_passthrough_harness.py, docs/zh/E2A-protocol.md, docs/en/E2A-protocol.md | 无独立 D：用户确认的 method 解耦任务，无独立 D 设计编号。 |

<a id="module-8"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |
| [F_20261113_1113_method-decoupling](F_20261113_1113_method-decoupling.md) | F | E2A method 字符串化并回归 Swarm 解析 | mingo007 | jiuwenswarm/common, jiuwenswarm/server, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/server/request_policy.py, jiuwenswarm/server/front/router.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/channels/web/frontend/src/services/webHttpClient.ts, tests/unit_tests/common/test_request_method_policy.py, tests/unit_tests/common/test_req_method_contract.py, tests/unit_tests/gateway/method_passthrough_harness.py, docs/zh/E2A-protocol.md, docs/en/E2A-protocol.md | 无独立 D：用户确认的 method 解耦任务，无独立 D 设计编号。 |

<a id="module-9"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-10"></a>

## jiuwenswarm/resources

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-11"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-12"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261113_1113_method-decoupling](F_20261113_1113_method-decoupling.md) | F | E2A method 字符串化并回归 Swarm 解析 | mingo007 | jiuwenswarm/common, jiuwenswarm/server, jiuwenswarm/channels/web/frontend | jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/server/request_policy.py, jiuwenswarm/server/front/router.py, jiuwenswarm/server/agent_ws_server.py, jiuwenswarm/server/http_invoke.py, jiuwenswarm/channels/web/frontend/src/services/webHttpClient.ts, tests/unit_tests/common/test_request_method_policy.py, tests/unit_tests/common/test_req_method_contract.py, tests/unit_tests/gateway/method_passthrough_harness.py, docs/zh/E2A-protocol.md, docs/en/E2A-protocol.md | 无独立 D：用户确认的 method 解耦任务，无独立 D 设计编号。 |

<a id="module-13"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |

<a id="module-14"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-15"></a>

## tests

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261113_1040_del-ut-slim](F_20261113_1040_del-ut-slim.md) | F | develop 测试库瘦身第一批：删除 agent-gateway 镜像测试层与死代码副本 | kdgmpw | tests | TESTING.md, tests/unit_tests/channels/cli/test_chat.py, tests/unit_tests/test_config.py, tests/unit_tests/runtime/test_runtime_service.py, tests/unit_tests/common/test_mcp_dynamic_token_patch.py, tests/unit_tests/gateway/test_cli_channel_handlers.py, tests/unit_tests/server/test_session_metadata.py, tests/unit_tests/agentserver/test_team_model_selection.py, tests/unit_tests/agentserver/test_skill_sync.py, tests/unit_tests/agentserver/test_runtime_worker_phase2.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/system_tests/test_project_memory.py, tests/auth/（整目录删除，4 个上游镜像测试）, tests/unit_tests/agentserver/memory/（删除 4 个镜像/占位测试）, tests/unit_tests/agentserver/test_skill_manager_rebuild_followup.py（删除）, tests/unit_tests/agentserver/test_envelope_placeholder.py（删除）, tests/integration/test_acp_cli_stdio_integration.py（删除）, tests/unit_tests/gateway/ 与 tests/unit_tests/channel/ 等（删除 65 个上游逐字节镜像测试，完整清单见本特性提交 diff） | 无独立 D：基于会话内对 develop 测试库的两轮静态扫描（10 个分组深扫 + 17,316 个用例级 AST 指纹比对）与上游 agent-gateway 仓的逐字节校验，用户在会话中直接确认执行，无独立设计编号。 |
