---
title: D/F/B/S 文档总索引
author: sync_documents.py
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

按模块分组；跨模块文档在各相关分组展示。

## 模块导航

| 模块 |
|---|
| [agent_gateway/channel_manager/im_platforms/xiaoyi](#module-1) |
| [agent_gateway/message_handler](#module-2) |
| [agent_teams](#module-3) |
| [agent_teams/workflow](#module-4) |
| [gateway/channel_manager/web](#module-5) |
| [jiuwenswarm](#module-6) |
| [jiuwenswarm/agents/harness](#module-7) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-8) |
| [jiuwenswarm/agents/harness/common](#module-9) |
| [jiuwenswarm/agents/harness/common/a4p](#module-10) |
| [jiuwenswarm/agents/harness/common/prompt](#module-11) |
| [jiuwenswarm/agents/harness/common/rails](#module-12) |
| [jiuwenswarm/agents/harness/common/recommendation](#module-13) |
| [jiuwenswarm/agents/harness/common/rsi](#module-14) |
| [jiuwenswarm/agents/harness/common/tools](#module-15) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-16) |
| [jiuwenswarm/agents/harness/team](#module-17) |
| [jiuwenswarm/agents/swarm](#module-18) |
| [jiuwenswarm/channels/tui](#module-19) |
| [jiuwenswarm/channels/web](#module-20) |
| [jiuwenswarm/channels/web/frontend](#module-21) |
| [jiuwenswarm/channels/web/frontend/src/features/rsi](#module-22) |
| [jiuwenswarm/channels/web/frontend/src/features/settings/modules/models](#module-23) |
| [jiuwenswarm/common](#module-24) |
| [jiuwenswarm/common/config_panel](#module-25) |
| [jiuwenswarm/extensions/sdk](#module-26) |
| [jiuwenswarm/gateway/channel_manager/web](#module-27) |
| [jiuwenswarm/observability](#module-28) |
| [jiuwenswarm/resources](#module-29) |
| [jiuwenswarm/resources/plugins/ttse](#module-30) |
| [jiuwenswarm/server](#module-31) |
| [jiuwenswarm/server/control](#module-32) |
| [jiuwenswarm/server/ipc](#module-33) |
| [jiuwenswarm/server/runtime](#module-34) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-35) |
| [jiuwenswarm/server/runtime/session](#module-36) |
| [jiuwenswarm/server/runtime/skill](#module-37) |
| [jiuwenswarm/server/supervisor](#module-38) |
| [jiuwenswarm/server/worker](#module-39) |
| [scripts](#module-40) |
| [tests/fixtures/ttse_plugin](#module-41) |
| [tests/unit_tests/agentserver](#module-42) |
| [tests/unit_tests/gateway](#module-43) |
| [tests/unit_tests/personal_context](#module-44) |
| [tests/unit_tests/server](#module-45) |
| [tests/unit_tests/server/extensions](#module-46) |
| [未分类](#module-47) |

<a id="module-1"></a>

## agent_gateway/channel_manager/im_platforms/xiaoyi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | agent_gateway/channel_manager/im_platforms/xiaoyi, agent_gateway/message_handler | agent_gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, agent_gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-2"></a>

## agent_gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261028_1445_xiaoyi-team-join-delivery](bugfix/gateway/channel_manager/im_platforms/xiaoyi/B_20261028_1445_xiaoyi-team-join-delivery.md) | B | 小艺成员加入团队后仅在通知栏收到 Team Leader 消息 | zhongxiaotian | agent_gateway/channel_manager/im_platforms/xiaoyi, agent_gateway/message_handler | agent_gateway/channel_manager/im_platforms/xiaoyi/xiaoyi_connect.py, agent_gateway/message_handler/join_exit_handlers.py, tests/unit_tests/gateway/test_xiaoyi_team_delivery.py | 无独立 D：当前团队席位和 fan-out 契约未变化，本次仅修复小艺平台任务生命周期。 |

<a id="module-3"></a>

## agent_teams

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0029](designs/proposals/SDD-0029-team-session-runtime-integration/design.md) | D | Team 模式接入统一 Session Runtime | @zhanglongmin | agent_teams | — | — |

<a id="module-4"></a>

## agent_teams/workflow

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |

<a id="module-5"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](features/F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |
| [B_20261102_1644_cron-web-default-project-id](bugfix/gateway/channel_manager/B_20261102_1644_cron-web-default-project-id.md) | B | 前端未选项目创建定时任务落库 project_id 回退为空串 | kdgmpw | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronProjectDisplay.ts, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。 |

<a id="module-6"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |
| [F_20261030_2018_teammate-swarmflow-adaptation](features/F_20261030_2018_teammate-swarmflow-adaptation.md) | F | 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配） | @xiajing | jiuwenswarm | jiuwenswarm/agents/harness/team/handlers/workflow_state.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py | 无独立 D：本仓侧为 teammate 接入 swarmflow 的适配与硬化，无独立设计文档。 |
| [B_20261030_2020_pause-refires-consumed-popups](bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261030_2020_pause-refires-consumed-popups.md) | B | 暂停/取消后已消费的 human 弹窗被重放层重投 | xiajing | jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts | 无独立 D：重放层过滤由主干既有机制承载，本修复补触发层清场与消费层去重两段。 |
| [B_20261031_0946_ttse-codecheck-boundaries](bugfix/jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1053_atomgit-review](bugfix/jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |

<a id="module-7"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |

<a id="module-8"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-9"></a>

## jiuwenswarm/agents/harness/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261119_1816_recovery-review-boundaries](bugfix/jiuwenswarm/server/B_20261119_1816_recovery-review-boundaries.md) | B | Worker 恢复的收包新鲜度、前置锁、取消登记和历史终态边界 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor, jiuwenswarm/server/runtime/session, jiuwenswarm/agents/harness/common | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/runtime/session/session_archive.py, jiuwenswarm/server/runtime/session/history_stream.py, jiuwenswarm/agents/harness/common/session_ops_service.py | 无独立 D：对既有恢复契约的缺陷修复，未建立独立设计。 |

<a id="module-10"></a>

## jiuwenswarm/agents/harness/common/a4p

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261119_1118_a4p-config-without-chat-session](bugfix/jiuwenswarm/channels/web/frontend/B_20261119_1118_a4p-config-without-chat-session.md) | B | 空白新建会话无法读取 A4P 开关 | yukuanj | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/a4p, jiuwenswarm/server | jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/A4PSettings.tsx, jiuwenswarm/channels/web/frontend/src/features/A4PAuthorizationModal.tsx, jiuwenswarm/agents/harness/common/a4p/rpc.py, jiuwenswarm/server/agent_ws_server.py | 无独立 D：依据本次会话确认的跨仓修复方案实施，无独立设计文档。 |

<a id="module-11"></a>

## jiuwenswarm/agents/harness/common/prompt

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-12"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261117_2001_deferred-memory-failure](bugfix/jiuwenswarm/agents/harness/common/rails/B_20261117_2001_deferred-memory-failure.md) | B | 延迟调用记忆工具时传播失败状态 | ToNy_St4rk | jiuwenswarm/agents/harness/common/rails | jiuwenswarm/agents/harness/common/rails/stream_event_rail.py, jiuwenswarm/agents/harness/common/memory/jiuwen_rail_builder.py | 无独立 D：修复 Issue 6151 中已有工具结果契约的状态传播，无独立设计。 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-13"></a>

## jiuwenswarm/agents/harness/common/recommendation

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-14"></a>

## jiuwenswarm/agents/harness/common/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](bugfix/jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | jiuwenswarm/agents/harness/common/rsi, jiuwenswarm/channels/web/frontend/src/features/rsi | jiuwenswarm/agents/harness/common/rsi/services.py, jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py, jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |
| [B_20261119_1104_paper-snapshot-restart](bugfix/jiuwenswarm/agents/harness/common/rsi/B_20261119_1104_paper-snapshot-restart.md) | B | 论文优化历史快照的重启恢复 | l_x_d | jiuwenswarm/agents/harness/common/rsi | jiuwenswarm/agents/harness/common/rsi/paper_workspace_provider.py, jiuwenswarm/agents/harness/common/rsi/provider_factory.py, tests/unit_tests/rsi/test_provider_snapshot_restart.py | 无独立 D：按确认的重启恢复用例修复既有行为，无独立设计。 |
| [S_01_observation-projection](specs/jiuwenswarm/agents/harness/common/rsi/S_01_observation-projection.md) | S | RSI 进度、报告与前端终态投影 | l_x_d | jiuwenswarm/agents/harness/common/rsi, jiuwenswarm/channels/web/frontend/src/features/rsi | jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py, jiuwenswarm/agents/harness/common/rsi/paper_workspace_provider.py, jiuwenswarm/agents/harness/common/rsi/services.py, jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts, jiuwenswarm/channels/web/frontend/src/features/rsi/components/RsiResultSummary.tsx | — |

<a id="module-15"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-16"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-17"></a>

## jiuwenswarm/agents/harness/team

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/team | jiuwenswarm/channels/web/frontend/src/features/interruptSettleGuard.ts, jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/tests/interruptSettleGuard.test.mjs, jiuwenswarm/channels/web/frontend/package.json, tests/unit_tests/agentserver/test_team_manager_registry.py | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |

<a id="module-18"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

<a id="module-19"></a>

## jiuwenswarm/channels/tui

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-20"></a>

## jiuwenswarm/channels/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1146_chat-cold-load](bugfix/jiuwenswarm/channels/web/B_20261031_1146_chat-cold-load.md) | B | 会话嵌套路由冷加载白屏修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/channels/web/test_web_static_cache_headers.py | 无独立 D：对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。 |
| [B_20261102_1104_cron-stable-model-selection](bugfix/jiuwenswarm/channels/web/B_20261102_1104_cron-stable-model-selection.md) | B | 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/frontend/src/types/cron.ts, jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronModelSelection.ts, jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts, jiuwenswarm/channels/web/frontend/src/features/free-models/chatError.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/package.json, jiuwenswarm/channels/web/frontend/tests/cronModelSelection.test.mjs, jiuwenswarm/channels/web/frontend/tests/modelPicker.test.mjs, jiuwenswarm/channels/web/frontend/tests/wsEventDedup.test.mjs, jiuwenswarm/channels/web/frontend/tests/chatError.test.mjs | 无独立 D：稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、 chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与 去重键缺维修复，无独立设计。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-21"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0026](designs/proposals/SDD-0026-trajectory-observability-web-ui/design.md) | D | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | @alan_cheng | jiuwenswarm/channels/web/frontend | — | — |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](bugfix/jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/stores/teamSliceApply.ts, jiuwenswarm/channels/web/frontend/src/stores/teamSelectorStore.ts, jiuwenswarm/channels/web/frontend/tests/teamSliceApply.test.mjs | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/team | jiuwenswarm/channels/web/frontend/src/features/interruptSettleGuard.ts, jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/tests/interruptSettleGuard.test.mjs, jiuwenswarm/channels/web/frontend/package.json, tests/unit_tests/agentserver/test_team_manager_registry.py | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [B_20261116_1652_cli-auth-effect-loop](bugfix/jiuwenswarm/channels/web/B_20261116_1652_cli-auth-effect-loop.md) | B | CLI MCP 授权因父级重渲染重复开窗和请求 | TwZhang | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/CliAuthModal.tsx, jiuwenswarm/channels/web/frontend/tests/cliAuthModal.test.mjs, jiuwenswarm/channels/web/frontend/tests/hostOAuthConnectorModal.test.mjs, jiuwenswarm/channels/web/frontend/package.json, docs/zh/MCP配置.md, docs/en/MCPConfiguration.md | 无独立 D：用户明确授权修复现有 CLI 授权生命周期缺陷，无独立设计。 |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | jiuwenswarm/server/runtime, jiuwenswarm/channels/web/frontend, tests/unit_tests/agentserver | jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx, tests/unit_tests/server/extensions/test_extension_package_hub.py, jiuwenswarm/channels/web/frontend/tests/pluginMineActions.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginDetailInstall.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginInstallConcurrency.test.mjs, tests/unit_tests/agentserver/test_paid_search_configuration.py, tests/unit_tests/agentserver/test_deep_adapter_empty_run_guard.py | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [B_20261117_1522_team-plan-toggle-lock](bugfix/jiuwenswarm/B_20261117_1522_team-plan-toggle-lock.md) | B | 集群会话计划模式开关是假开关：建队后冻结并让位动态工作流 | chenchunzhou | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/features/planMode/planModeGate.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/permissionAnswerTransport.test.mjs | 无独立 D：由 issue 6361 现象逆向定位，无独立设计文档；方案与取舍记录在 OpenSpec 变更 issue-6361-team-plan-toggle-lock 的 proposal/design/specs，修复范围限定 Web 前端开关闸门与提示文案。 |
| [B_20261119_1039_video-memory-submit-pending](bugfix/jiuwenswarm/channels/web/frontend/B_20261119_1039_video-memory-submit-pending.md) | B | 视频记忆任务 pending 导致提交工具持续执行中 | ToNy_St4rk | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/features/tool-events/toolEventNormalizer.ts, jiuwenswarm/channels/web/frontend/tests/toolEventNormalizer.test.mjs, jiuwenswarm/channels/web/frontend/tests/videoMemoryToolLifecycle.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/videoMemoryToolExports.ts, jiuwenswarm/channels/web/frontend/package.json | 无独立 D：现有工具结果状态的局部修复，无独立设计。 |
| [B_20261119_1118_a4p-config-without-chat-session](bugfix/jiuwenswarm/channels/web/frontend/B_20261119_1118_a4p-config-without-chat-session.md) | B | 空白新建会话无法读取 A4P 开关 | yukuanj | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/a4p, jiuwenswarm/server | jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/A4PSettings.tsx, jiuwenswarm/channels/web/frontend/src/features/A4PAuthorizationModal.tsx, jiuwenswarm/agents/harness/common/a4p/rpc.py, jiuwenswarm/server/agent_ws_server.py | 无独立 D：依据本次会话确认的跨仓修复方案实施，无独立设计文档。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |
| [S_01_plugin-marketplace](specs/jiuwenswarm/server/runtime/S_01_plugin-marketplace.md) | S | 插件市场列表归属与操作契约 | lizhiyuan | jiuwenswarm/server/runtime, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-22"></a>

## jiuwenswarm/channels/web/frontend/src/features/rsi

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261114_1302_terminal-metrics-and-model-errors](bugfix/jiuwenswarm/agents/harness/common/rsi/B_20261114_1302_terminal-metrics-and-model-errors.md) | B | RSI 终态统计、侧栏运行态和论文模型错误修复 | l_x_d | jiuwenswarm/agents/harness/common/rsi, jiuwenswarm/channels/web/frontend/src/features/rsi | jiuwenswarm/agents/harness/common/rsi/services.py, jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py, jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts | 无独立 D：按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。 |
| [S_01_observation-projection](specs/jiuwenswarm/agents/harness/common/rsi/S_01_observation-projection.md) | S | RSI 进度、报告与前端终态投影 | l_x_d | jiuwenswarm/agents/harness/common/rsi, jiuwenswarm/channels/web/frontend/src/features/rsi | jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py, jiuwenswarm/agents/harness/common/rsi/paper_workspace_provider.py, jiuwenswarm/agents/harness/common/rsi/services.py, jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts, jiuwenswarm/channels/web/frontend/src/features/rsi/components/RsiResultSummary.tsx | — |

<a id="module-23"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings/modules/models

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261116_1636_custom-model-reasoning-policy](bugfix/jiuwenswarm/channels/web/frontend/B_20261116_1636_custom-model-reasoning-policy.md) | B | 自定义模型误展示推理配置修复 | lizhiyuan | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelReasoning.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelValidation.ts, jiuwenswarm/channels/web/frontend/tests/modelReasoning.test.mjs, jiuwenswarm/channels/web/frontend/tests/modelSettings.test.mjs | 无独立 D：用户已确认的既有设置页能力边界收敛；不新增模型发现或跨仓接口，属于局部回归修复。 |
| [B_20261117_1727_model-output-token-limit](bugfix/jiuwenswarm/channels/web/frontend/B_20261117_1727_model-output-token-limit.md) | B | 模型单次最大输出配置修复 | lizhiyuan | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models, jiuwenswarm/common/config_panel | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelsSettings.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.css, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/outputTokens.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelValidation.ts, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/types/index.ts, jiuwenswarm/common/config.py, jiuwenswarm/common/config_panel/models_handlers.py, jiuwenswarm/channels/web/frontend/tests/modelSettings.test.mjs, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, tests/unit_tests/common/test_config_validate_model_mcc.py, tests/unit_tests/gateway/test_app_web_handlers.py, tests/unit_tests/test_model_resource_fixes.py | 无独立 D：用户在本轮会话确认的模型设置范围调整，未形成独立设计文档。 |

<a id="module-24"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | jiuwenswarm/common, jiuwenswarm/server/control | jiuwenswarm/common/permission_mode.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, tests/unit_tests/agentserver/test_front_permission_profile.py | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-25"></a>

## jiuwenswarm/common/config_panel

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261117_1727_model-output-token-limit](bugfix/jiuwenswarm/channels/web/frontend/B_20261117_1727_model-output-token-limit.md) | B | 模型单次最大输出配置修复 | lizhiyuan | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models, jiuwenswarm/common/config_panel | jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelsSettings.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.tsx, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.css, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/outputTokens.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelValidation.ts, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/types/index.ts, jiuwenswarm/common/config.py, jiuwenswarm/common/config_panel/models_handlers.py, jiuwenswarm/channels/web/frontend/tests/modelSettings.test.mjs, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, tests/unit_tests/common/test_config_validate_model_mcc.py, tests/unit_tests/gateway/test_app_web_handlers.py, tests/unit_tests/test_model_resource_fixes.py | 无独立 D：用户在本轮会话确认的模型设置范围调整，未形成独立设计文档。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-26"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-27"></a>

## jiuwenswarm/gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-28"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-29"></a>

## jiuwenswarm/resources

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |

<a id="module-30"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_02_ttse-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_ttse-plugin-runtime.md) | S | TTSE Coding 插件运行契约 | xinyang_bali | jiuwenswarm/resources/plugins/ttse | jiuwenswarm/resources/plugins/ttse/rails/ttse_plugin_rail.py, jiuwenswarm/resources/plugins/ttse/manifest.json | — |

<a id="module-31"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](bugfix/jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | jiuwenswarm/server | jiuwenswarm/server/app_agentserver.py | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261119_1118_a4p-config-without-chat-session](bugfix/jiuwenswarm/channels/web/frontend/B_20261119_1118_a4p-config-without-chat-session.md) | B | 空白新建会话无法读取 A4P 开关 | yukuanj | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/a4p, jiuwenswarm/server | jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/A4PSettings.tsx, jiuwenswarm/channels/web/frontend/src/features/A4PAuthorizationModal.tsx, jiuwenswarm/agents/harness/common/a4p/rpc.py, jiuwenswarm/server/agent_ws_server.py | 无独立 D：依据本次会话确认的跨仓修复方案实施，无独立设计文档。 |

<a id="module-32"></a>

## jiuwenswarm/server/control

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | jiuwenswarm/common, jiuwenswarm/server/control | jiuwenswarm/common/permission_mode.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, tests/unit_tests/agentserver/test_front_permission_profile.py | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-33"></a>

## jiuwenswarm/server/ipc

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261119_1816_recovery-review-boundaries](bugfix/jiuwenswarm/server/B_20261119_1816_recovery-review-boundaries.md) | B | Worker 恢复的收包新鲜度、前置锁、取消登记和历史终态边界 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor, jiuwenswarm/server/runtime/session, jiuwenswarm/agents/harness/common | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/runtime/session/session_archive.py, jiuwenswarm/server/runtime/session/history_stream.py, jiuwenswarm/agents/harness/common/session_ops_service.py | 无独立 D：对既有恢复契约的缺陷修复，未建立独立设计。 |

<a id="module-34"></a>

## jiuwenswarm/server/runtime

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](bugfix/jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | jiuwenswarm/server/runtime, tests/unit_tests/server | jiuwenswarm/server/runtime/group_service.py, tests/unit_tests/server/test_group_lock.py, tests/conftest.py | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | jiuwenswarm/server/runtime, jiuwenswarm/channels/web/frontend, tests/unit_tests/agentserver | jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx, tests/unit_tests/server/extensions/test_extension_package_hub.py, jiuwenswarm/channels/web/frontend/tests/pluginMineActions.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginDetailInstall.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginInstallConcurrency.test.mjs, tests/unit_tests/agentserver/test_paid_search_configuration.py, tests/unit_tests/agentserver/test_deep_adapter_empty_run_guard.py | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |
| [S_01_plugin-marketplace](specs/jiuwenswarm/server/runtime/S_01_plugin-marketplace.md) | S | 插件市场列表归属与操作契约 | lizhiyuan | jiuwenswarm/server/runtime, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx | — |

<a id="module-35"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261031_0946_ttse-codecheck-boundaries](bugfix/jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](bugfix/jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-36"></a>

## jiuwenswarm/server/runtime/session

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261119_1816_recovery-review-boundaries](bugfix/jiuwenswarm/server/B_20261119_1816_recovery-review-boundaries.md) | B | Worker 恢复的收包新鲜度、前置锁、取消登记和历史终态边界 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor, jiuwenswarm/server/runtime/session, jiuwenswarm/agents/harness/common | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/runtime/session/session_archive.py, jiuwenswarm/server/runtime/session/history_stream.py, jiuwenswarm/agents/harness/common/session_ops_service.py | 无独立 D：对既有恢复契约的缺陷修复，未建立独立设计。 |

<a id="module-37"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | jiuwenswarm/server/runtime/skill | jiuwenswarm/server/runtime/skill/skill_manager.py, tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |
| [B_20261116_1412_skill-import-validation-and-list](bugfix/jiuwenswarm/server/runtime/skill/B_20261116_1412_skill-import-validation-and-list.md) | B | 技能导入元数据与 ZIP 路径安全校验修复 | zhangzhongteng | jiuwenswarm/server/runtime/skill | jiuwenswarm/server/runtime/skill/skill_manager.py, tests/unit_tests/agentserver/test_skills_import_upload_and_knowledge.py, tests/unit_tests/agentserver/test_skill_manager_path_safety.py, tests/unit_tests/agentserver/test_skill_manager_clawhub_owner_handle.py, tests/unit_tests/test_app_web_skill_upload.py, docs/zh/技能.md, docs/en/Skills.md | 无独立 D：用户确认统一个人版技能导入和市场安装的元数据校验，并修复 ZIP 原始文件名及路径检查；无独立设计。 |

<a id="module-38"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261119_1816_recovery-review-boundaries](bugfix/jiuwenswarm/server/B_20261119_1816_recovery-review-boundaries.md) | B | Worker 恢复的收包新鲜度、前置锁、取消登记和历史终态边界 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor, jiuwenswarm/server/runtime/session, jiuwenswarm/agents/harness/common | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/server/runtime/session/session_archive.py, jiuwenswarm/server/runtime/session/history_stream.py, jiuwenswarm/agents/harness/common/session_ops_service.py | 无独立 D：对既有恢复契约的缺陷修复，未建立独立设计。 |

<a id="module-39"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_1518_worker-dispatch-slot-accounting](bugfix/jiuwenswarm/server/worker/B_20261105_1518_worker-dispatch-slot-accounting.md) | B | Worker 派发容量排除已返回的 handler | kdgmpw | jiuwenswarm/server/worker | jiuwenswarm/server/worker/event_store.py | 无独立 D：按会话中确认的最小修复方案调整 Worker 内部计数。 |

<a id="module-40"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261103_1124_sdk-web-retrieval](features/F_20261103_1124_sdk-web-retrieval.md) | F | 接入 SDK Web 质量控制与检索预算 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/resources, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/recommendation, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/rails, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/resources/.env.template, tests/unit_tests/agentserver/permissions/test_trusted_web_search.py, tests/unit_tests/agentserver/permissions/test_deep_required_composition.py, tests/unit_tests/agentserver/permissions/test_permission_cold_build.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx, jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts, tests/unit_tests/common/test_free_search_settings.py, tests/unit_tests/gateway/test_app_web_handlers.py, docs/en/Configuration.md, docs/zh/配置信息.md, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py, tests/unit_tests/test_release_dependencies.py, pyproject.toml, uv.lock | 无独立 D：用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。 |
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261118_1150_memory-frozen-registration](bugfix/scripts/B_20261118_1150_memory-frozen-registration.md) | B | 修复冻结后端中 JiuwenMemory 动态实现漏包 | ToNy_St4rk | scripts | scripts/jiuwenswarm.spec, scripts/memory_bundle.py, scripts/verify_memory_bundle.py, scripts/build-exe.ps1, scripts/build-exe.bat, scripts/build-electron-exe.ps1, scripts/build-macos.sh, scripts/build-electron-exe.sh, tests/unit_tests/scripts/test_memory_bundle.py | 无独立 D：按已确认的发布包故障进行局部打包修复，不改变 Memory 的公共接口或运行时配置契约。 |
| [S_02_web-retrieval](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_web-retrieval.md) | S | SDK Web 检索接入与免费来源配置 | ShowHsiang | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/common/config_panel, jiuwenswarm/server/control, jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/prompt, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/agents/harness/common/recommendation, scripts | jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/common/utils.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts, jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts, jiuwenswarm/agents/harness/common/tools/search_tools.py, jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py, jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py, jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py, jiuwenswarm/agents/harness/common/recommendation/topic_search.py, jiuwenswarm/agents/harness/common/recommendation/topic_model.py, jiuwenswarm/agents/harness/common/recommendation/topic_starter.py, scripts/release_dependencies.py, scripts/build.sh, scripts/build.ps1, setup.py, pyproject.toml | — |

<a id="module-41"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-42"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](bugfix/tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | tests/unit_tests/agentserver | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |
| [B_20261116_1701_my-plugin-installed-only](bugfix/jiuwenswarm/server/runtime/B_20261116_1701_my-plugin-installed-only.md) | B | 未安装插件草稿混入我的扩展并显示安装按钮 | lizhiyuan | jiuwenswarm/server/runtime, jiuwenswarm/channels/web/frontend, tests/unit_tests/agentserver | jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx, jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx, tests/unit_tests/server/extensions/test_extension_package_hub.py, jiuwenswarm/channels/web/frontend/tests/pluginMineActions.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginDetailInstall.test.mjs, jiuwenswarm/channels/web/frontend/tests/pluginInstallConcurrency.test.mjs, tests/unit_tests/agentserver/test_paid_search_configuration.py, tests/unit_tests/agentserver/test_deep_adapter_empty_run_guard.py | 无独立 D：用户在本次对话明确确认插件列表归属及按钮策略，属于现有市场行为的小范围修复，无独立设计文档。 |

<a id="module-43"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-44"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-45"></a>

## tests/unit_tests/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261110_1849_group-lock-cancellation](bugfix/jiuwenswarm/server/runtime/B_20261110_1849_group-lock-cancellation.md) | B | 群任务取消期间等待文件锁清理完成 | ShowHsiang | jiuwenswarm/server/runtime, tests/unit_tests/server | jiuwenswarm/server/runtime/group_service.py, tests/unit_tests/server/test_group_lock.py, tests/conftest.py | 无独立 D：修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。 |

<a id="module-46"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-47"></a>

## 未分类

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [README](designs/README.md) | D | 设计资产 | 待确认 | — | — | — |
| [REGISTRY](designs/REGISTRY.md) | D | SDD 编号台账 (REGISTRY) | 待确认 | — | — | — |
| [legacy-documents](designs/legacy-documents.md) | D | 旧格式设计资料 | 待确认 | — | — | — |
| [migration-20261026](designs/migration-20261026.md) | D | 设计文档迁移与跨仓引用 | 待确认 | — | — | — |
| [team_expert_group_manifest_design](designs/archive/team_expert_group_manifest_design.md) | D | Team 模式接入专家团 Manifest 加载设计 | 待确认 | — | — | — |
| [team_permission_design](designs/archive/team_permission_design.md) | D | Team 模式 Permissions 设计方案 | 待确认 | — | — | — |
| [team_plan_design](designs/archive/team_plan_design.md) | D | Team Plan 模式设计文档 | 待确认 | — | — | — |
| [team_worktree_isolation_agenttool_cn](designs/archive/team_worktree_isolation_agenttool_cn.md) | D | Team Teammate Worktree Isolation 设计文档 | 待确认 | — | — | — |
| [B_20261026_1602_code-multimodal-loading](bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) | B | 配置多模态模型后，Code 未按 Work 规则加载工具 | 待确认 | — | — | 待核实 |
| [B_20261026_1606_issue-5753-session-project-binding](bugfix/jiuwenswarm/server/runtime/session/B_20261026_1606_issue-5753-session-project-binding.md) | B | Issue #5753：普通会话发送消息后混入全局项目目录 | 待确认 | — | — | 待核实 |
| [B_20261026_2035_gateway-contract-collection](bugfix/tests/unit_tests/common/B_20261026_2035_gateway-contract-collection.md) | B | Gateway 拆包后协议契约测试无法收集 | 待确认 | — | — | 待核实 |
| [S_01_multimodal-tool-loading](specs/jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) | S | Work / Code 多模态工具加载契约 | 待确认 | — | — | — |
