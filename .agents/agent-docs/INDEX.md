---
title: D/F/B/S 文档总索引
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
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
| [deploy/yuanrong](#module-5) |
| [gateway/channel_manager/web](#module-6) |
| [jiuwenswarm](#module-7) |
| [jiuwenswarm/agents/harness](#module-8) |
| [jiuwenswarm/agents/harness/code/rails/heartbeat](#module-9) |
| [jiuwenswarm/agents/harness/common/rails](#module-10) |
| [jiuwenswarm/agents/harness/common/tools](#module-11) |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-12) |
| [jiuwenswarm/agents/harness/team](#module-13) |
| [jiuwenswarm/agents/swarm](#module-14) |
| [jiuwenswarm/channels/tui](#module-15) |
| [jiuwenswarm/channels/web](#module-16) |
| [jiuwenswarm/channels/web/frontend](#module-17) |
| [jiuwenswarm/common](#module-18) |
| [jiuwenswarm/common/e2a](#module-19) |
| [jiuwenswarm/common/schema](#module-20) |
| [jiuwenswarm/extensions/sdk](#module-21) |
| [jiuwenswarm/extensions/video_duplex](#module-22) |
| [jiuwenswarm/gateway/channel_manager/web](#module-23) |
| [jiuwenswarm/observability](#module-24) |
| [jiuwenswarm/resources](#module-25) |
| [jiuwenswarm/resources/plugins/ttse](#module-26) |
| [jiuwenswarm/server](#module-27) |
| [jiuwenswarm/server/control](#module-28) |
| [jiuwenswarm/server/ipc](#module-29) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-30) |
| [jiuwenswarm/server/runtime/skill](#module-31) |
| [jiuwenswarm/server/supervisor](#module-32) |
| [jiuwenswarm/server/worker](#module-33) |
| [scripts](#module-34) |
| [tests](#module-35) |
| [tests/fixtures/ttse_plugin](#module-36) |
| [tests/unit_tests/agentserver](#module-37) |
| [tests/unit_tests/gateway](#module-38) |
| [tests/unit_tests/personal_context](#module-39) |
| [tests/unit_tests/server/extensions](#module-40) |
| [未分类](#module-41) |

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

## deploy/yuanrong

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-6"></a>

## gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261031_1139_gateway-session-e2a](features/F_20261031_1139_gateway-session-e2a.md) | F | Web 会话管理统一经 E2A 交由 Server 执行 | ruiqiang2 | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：用户确认的 Gateway 解耦任务 4.1，沿用 develop 个人版现有链路，无独立设计编号。 |
| [B_20261102_1644_cron-web-default-project-id](bugfix/gateway/channel_manager/B_20261102_1644_cron-web-default-project-id.md) | B | 前端未选项目创建定时任务落库 project_id 回退为空串 | kdgmpw | gateway/channel_manager/web | gateway/channel_manager/web/app_web_handlers.py, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronProjectDisplay.ts, tests/unit_tests/gateway/test_app_web_handlers.py | 无独立 D：恢复 4b00671f6（#5713）已实现的行为，属于对该修复在企业版合入后被回退的局部回归修复，无独立设计。 |

<a id="module-7"></a>

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

<a id="module-8"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-9"></a>

## jiuwenswarm/agents/harness/code/rails/heartbeat

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-10"></a>

## jiuwenswarm/agents/harness/common/rails

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |

<a id="module-11"></a>

## jiuwenswarm/agents/harness/common/tools

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |

<a id="module-12"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261027_1553_reminder-tool-routing](bugfix/jiuwenswarm/agents/harness/common/tools/cron/B_20261027_1553_reminder-tool-routing.md) | B | Code 模式中文提醒错误路由到 heartbeat | MengHan18 | jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/agents/harness/code/rails/heartbeat | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 的局部工具选路缺陷修复，无独立设计。 |

<a id="module-13"></a>

## jiuwenswarm/agents/harness/team

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/team | jiuwenswarm/channels/web/frontend/src/features/interruptSettleGuard.ts, jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/tests/interruptSettleGuard.test.mjs, jiuwenswarm/channels/web/frontend/package.json, tests/unit_tests/agentserver/test_team_manager_registry.py | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |

<a id="module-14"></a>

## jiuwenswarm/agents/swarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |

<a id="module-15"></a>

## jiuwenswarm/channels/tui

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-16"></a>

## jiuwenswarm/channels/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1146_chat-cold-load](bugfix/jiuwenswarm/channels/web/B_20261031_1146_chat-cold-load.md) | B | 会话嵌套路由冷加载白屏修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/channels/web/test_web_static_cache_headers.py | 无独立 D：对已有静态服务契约的局部修复，与 nginx 的资源别名保持兼容，无独立设计。 |
| [B_20261102_1104_cron-stable-model-selection](bugfix/jiuwenswarm/channels/web/B_20261102_1104_cron-stable-model-selection.md) | B | 定时任务模型选择丢失稳定 ID 与执行失败报错不可见修复 | kdgmpw | jiuwenswarm/channels/web | jiuwenswarm/channels/web/frontend/src/types/cron.ts, jiuwenswarm/channels/web/frontend/src/components/CronPanel/CronTaskDrawer.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/index.tsx, jiuwenswarm/channels/web/frontend/src/components/CronPanel/cronModelSelection.ts, jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts, jiuwenswarm/channels/web/frontend/src/features/free-models/chatError.ts, jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json, jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json, jiuwenswarm/channels/web/frontend/package.json, jiuwenswarm/channels/web/frontend/tests/cronModelSelection.test.mjs, jiuwenswarm/channels/web/frontend/tests/modelPicker.test.mjs, jiuwenswarm/channels/web/frontend/tests/wsEventDedup.test.mjs, jiuwenswarm/channels/web/frontend/tests/chatError.test.mjs | 无独立 D：稳定选择契约（ModelPicker selection/onSelectionChange、CronJob.model_selection、 chat.error 去重窗口）均为既有实现，本次是 cron 抽屉这一旧契约调用方的迁移补接与 去重键缺维修复，无独立设计。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261103_1451_hub-default-url-consistency](bugfix/jiuwenswarm/channels/web/B_20261103_1451_hub-default-url-consistency.md) | B | Swarm Skills Hub 默认地址前后端/文档不一致导致回退域名不可达 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/tui | jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx, jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/teamskills.ts, jiuwenswarm/channels/web/frontend/tests/skillHubEndpointContract.test.mjs, docs/en/SwarmSkills.md, docs/zh/SwarmSkills.md, docs/en/Configuration.md, docs/zh/配置信息.md | 无独立 D：纯默认值与文档文案对齐修复，无独立设计；后端默认值（hub_client.DEFAULT_HUB_BASE_URL）与 README 已是正确口径。 |

<a id="module-17"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0026](designs/proposals/SDD-0026-trajectory-observability-web-ui/design.md) | D | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | @alan_cheng | jiuwenswarm/channels/web/frontend | — | — |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [B_20261030_1250_org-snapshot-removes-mirrored-workers](bugfix/jiuwenswarm/channels/web/frontend/B_20261030_1250_org-snapshot-removes-mirrored-workers.md) | B | 组织 UI 快照投影整表覆盖导致镜像 workflow worker 从成员面板消失 | xiajing | jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/frontend/src/stores/teamSliceApply.ts, jiuwenswarm/channels/web/frontend/src/stores/teamSelectorStore.ts, jiuwenswarm/channels/web/frontend/tests/teamSliceApply.test.mjs | 无独立 D：对既有"快照完整替换"契约（跨 Team 切换不串数据）的成员级例外修复，属 F_20261027_1026 特性线的验证期 bugfix，无独立设计文档。 |
| [B_20261103_1112_raw-file-range-seek](bugfix/jiuwenswarm/channels/web/B_20261103_1112_raw-file-range-seek.md) | B | /file-api/raw-file 不支持 Range 请求导致 design 模式视频预览进度条不可快进 | openjiuwen_ai | jiuwenswarm/channels/web, jiuwenswarm/channels/web/frontend | jiuwenswarm/channels/web/app_web.py, jiuwenswarm/channels/web/frontend/vite.config.ts, tests/unit_tests/test_app_web_raw_file.py | 无独立 D：根因与修法均为标准 HTTP 单区间 Range 语义（对齐 gateway 既有 serve_file_chunks 实现），无独立设计。 |
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261112_1405_stop-btn-stuck-after-skip](bugfix/jiuwenswarm/B_20261112_1405_stop-btn-stuck-after-skip.md) | B | 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏 | xiajing | jiuwenswarm/channels/web/frontend, jiuwenswarm/agents/harness/team | jiuwenswarm/channels/web/frontend/src/features/interruptSettleGuard.ts, jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/tests/interruptSettleGuard.test.mjs, jiuwenswarm/channels/web/frontend/package.json, tests/unit_tests/agentserver/test_team_manager_registry.py | 无独立 D：依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |

<a id="module-18"></a>

## jiuwenswarm/common

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | jiuwenswarm/common, jiuwenswarm/server/control | jiuwenswarm/common/permission_mode.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, tests/unit_tests/agentserver/test_front_permission_profile.py | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-19"></a>

## jiuwenswarm/common/e2a

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261114_1714_local-request-methods](features/F_20261114_1714_local-request-methods.md) | F | Localize ReqMethod and dependent request models | mingo007 | jiuwenswarm/common/schema, jiuwenswarm/common/e2a, jiuwenswarm/extensions/video_duplex | jiuwenswarm/common/schema/req_method.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/common/e2a/gateway_normalize.py, jiuwenswarm/extensions/video_duplex/backend/task_adapter.py, jiuwenswarm/extensions/video_duplex/extension.py, tests/unit_tests/common/test_local_req_method.py, tests/unit_tests/common/test_req_method_contract.py | 无独立 D：Explicit user request to copy ReqMethod-related implementation into JiuwenSwarm; no separate design document. |
| [S_01_local-request-methods](specs/jiuwenswarm/common/S_01_local-request-methods.md) | S | JiuwenSwarm request method ownership | mingo007 | jiuwenswarm/common/schema, jiuwenswarm/common/e2a | jiuwenswarm/common/schema/req_method.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/common/e2a/gateway_normalize.py, jiuwenswarm/common/e2a/agent_compat.py | — |

<a id="module-20"></a>

## jiuwenswarm/common/schema

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261114_1714_local-request-methods](features/F_20261114_1714_local-request-methods.md) | F | Localize ReqMethod and dependent request models | mingo007 | jiuwenswarm/common/schema, jiuwenswarm/common/e2a, jiuwenswarm/extensions/video_duplex | jiuwenswarm/common/schema/req_method.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/common/e2a/gateway_normalize.py, jiuwenswarm/extensions/video_duplex/backend/task_adapter.py, jiuwenswarm/extensions/video_duplex/extension.py, tests/unit_tests/common/test_local_req_method.py, tests/unit_tests/common/test_req_method_contract.py | 无独立 D：Explicit user request to copy ReqMethod-related implementation into JiuwenSwarm; no separate design document. |
| [S_01_local-request-methods](specs/jiuwenswarm/common/S_01_local-request-methods.md) | S | JiuwenSwarm request method ownership | mingo007 | jiuwenswarm/common/schema, jiuwenswarm/common/e2a | jiuwenswarm/common/schema/req_method.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/common/e2a/gateway_normalize.py, jiuwenswarm/common/e2a/agent_compat.py | — |

<a id="module-21"></a>

## jiuwenswarm/extensions/sdk

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-22"></a>

## jiuwenswarm/extensions/video_duplex

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261114_1714_local-request-methods](features/F_20261114_1714_local-request-methods.md) | F | Localize ReqMethod and dependent request models | mingo007 | jiuwenswarm/common/schema, jiuwenswarm/common/e2a, jiuwenswarm/extensions/video_duplex | jiuwenswarm/common/schema/req_method.py, jiuwenswarm/common/schema/message.py, jiuwenswarm/common/schema/agent.py, jiuwenswarm/common/e2a/gateway_normalize.py, jiuwenswarm/extensions/video_duplex/backend/task_adapter.py, jiuwenswarm/extensions/video_duplex/extension.py, tests/unit_tests/common/test_local_req_method.py, tests/unit_tests/common/test_req_method_contract.py | 无独立 D：Explicit user request to copy ReqMethod-related implementation into JiuwenSwarm; no separate design document. |

<a id="module-23"></a>

## jiuwenswarm/gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-24"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-25"></a>

## jiuwenswarm/resources

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |

<a id="module-26"></a>

## jiuwenswarm/resources/plugins/ttse

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [S_02_ttse-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_02_ttse-plugin-runtime.md) | S | TTSE Coding 插件运行契约 | xinyang_bali | jiuwenswarm/resources/plugins/ttse | jiuwenswarm/resources/plugins/ttse/rails/ttse_plugin_rail.py, jiuwenswarm/resources/plugins/ttse/manifest.json | — |

<a id="module-27"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261029_0927_missing-supervisor-entry](bugfix/jiuwenswarm/server/B_20261029_0927_missing-supervisor-entry.md) | B | 重启 AgentServer 时缺少 supervisor 启动函数 | xinyang_bali | jiuwenswarm/server | jiuwenswarm/server/app_agentserver.py | 无独立 D：用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |

<a id="module-28"></a>

## jiuwenswarm/server/control

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261112_2025_permission-profile-refresh](bugfix/jiuwenswarm/server/B_20261112_2025_permission-profile-refresh.md) | B | 刷新后智能审批档位错误回显为默认权限 | nypdmax | jiuwenswarm/common, jiuwenswarm/server/control | jiuwenswarm/common/permission_mode.py, jiuwenswarm/common/config_panel/config_set_handlers.py, jiuwenswarm/server/control/config_service.py, tests/unit_tests/agentserver/test_front_permission_profile.py | 无独立 D：补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。 |

<a id="module-29"></a>

## jiuwenswarm/server/ipc

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261110_1042_issue6191-ipc-recovery](bugfix/jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, tests/unit_tests/agentserver/test_ipc_receive_recovery.py | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-30"></a>

## jiuwenswarm/server/runtime/agent_adapter

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261027_1843_ttse-plugin-rail](features/F_20261027_1843_ttse-plugin-rail.md) | F | TTSE 以插件 Rail 接入主 Coding Agent | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness, jiuwenswarm/extensions/sdk, jiuwenswarm/resources/plugins/ttse | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/extensions/sdk/plugin_runtime.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/resources/plugins/ttse/manifest.json, scripts/package_ttse_plugin.py | 无独立 D：用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。 |
| [F_20261027_2015_bilingual-progressive-tool-discovery](features/jiuwenswarm/agents/harness/common/tools/F_20261027_2015_bilingual-progressive-tool-discovery.md) | F | 中英文渐进式工具发现 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, tests/unit_tests/agentserver/test_tool_discovery_language.py | 无独立 D：针对 Issue 5615 后续扩展的已确认实现，未建立独立仓内设计编号 |
| [F_20261029_1757_mcp-app-agent-stack-refresh](features/F_20261029_1757_mcp-app-agent-stack-refresh.md) | F | MCP App 到 Agent 的 stacked PR 更新 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/App.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | 无独立 D：本次迁移已有 follow-up 实现，不新建方案；需求依据为主仓库 issue 4944 和原 MR !1。 |
| [B_20261031_0946_ttse-codecheck-boundaries](bugfix/jiuwenswarm/B_20261031_0946_ttse-codecheck-boundaries.md) | B | TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复 | xinyang_bali | jiuwenswarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/openjiuwen_fs_rwlock_patch.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, scripts/package_ttse_plugin.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py | 无独立 D：按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。 |
| [B_20261031_1042_pr9317-review](bugfix/jiuwenswarm/server/B_20261031_1042_pr9317-review.md) | B | PR 9317 插件模式校验与 Front 遗留启动链清理 | xinyang_bali | jiuwenswarm/server, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/server/app_agentserver.py, jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, tests/unit_tests/test_app_agentserver.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：根据 openJiuwen-bot 检视意见清理现有实现，不引入新接口或架构方案，无独立 D。 |
| [B_20261031_1053_atomgit-review](bugfix/jiuwenswarm/server/B_20261031_1053_atomgit-review.md) | B | PR 9317 AtomGit 取消、补丁兼容与注销恢复修复 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/openjiuwen_fs_rwlock_patch.py, tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/agentserver/test_ttse_background_drain.py, tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py | 无独立 D：根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。 |
| [B_20261106_1132_organization-round-cleanup](bugfix/jiuwenswarm/agents/swarm/B_20261106_1132_organization-round-cleanup.md) | B | 组织回合澄清等待、失败尝试与初次建卡取消的收尾修复 | “ZhangRan-Zora | jiuwenswarm/agents/swarm, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/swarm/group_execution.py, jiuwenswarm/agents/swarm/group_org_bootstrap.py, jiuwenswarm/server/runtime/agent_adapter/team_helpers.py, tests/agents/swarm/test_group_org_bootstrap.py | 无独立 D：按 PR 9636 三条检视意见修复既有组织生命周期边界，无独立设计。 |
| [S_01_app-agent-interaction](specs/mcpApps/S_01_app-agent-interaction.md) | S | MCP App 到 Agent 交互规约 | LI Jinyang | jiuwenswarm/channels/web/frontend, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts, jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, jiuwenswarm/server/runtime/agent_adapter/user_turn.py | — |
| [S_01_tool-discovery-language](specs/jiuwenswarm/agents/harness/common/tools/S_01_tool-discovery-language.md) | S | 渐进式工具发现语言与原始执行契约 | MengHan18 | jiuwenswarm/agents/harness/common/tools, jiuwenswarm/agents/harness/common/rails, jiuwenswarm/server/runtime/agent_adapter | jiuwenswarm/agents/harness/common/tools/tool_discovery_catalog.py, jiuwenswarm/agents/harness/common/tools/tool_retrieval_toolkits.py, jiuwenswarm/agents/harness/common/rails/tool_retrieval_prompt_rail.py, jiuwenswarm/agents/harness/code/rails/heartbeat_rail.py, jiuwenswarm/agents/harness/team/rails/team_member_skill_toolkit_rail.py, jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py | — |
| [S_03_native-plugin-runtime](specs/jiuwenswarm/server/runtime/agent_adapter/S_03_native-plugin-runtime.md) | S | 原生插件通用运行服务与生命周期 | xinyang_bali | jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/extensions/sdk | jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py, jiuwenswarm/extensions/sdk/plugin_runtime.py | — |

<a id="module-31"></a>

## jiuwenswarm/server/runtime/skill

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261103_0936_skill-action-param-type-validation](bugfix/jiuwenswarm/server/runtime/skill/B_20261103_0936_skill-action-param-type-validation.md) | B | 技能安装/更新/卸载入口参数类型校验缺失修复 | openjiuwen_ai | jiuwenswarm/server/runtime/skill | jiuwenswarm/server/runtime/skill/skill_manager.py, tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py | 无独立 D：三个入口的参数契约（string 标识、boolean force）已由前端 webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口 类型校验层缺失的补接，无独立设计。 |

<a id="module-32"></a>

## jiuwenswarm/server/supervisor

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261107_1505_6226-forwarding-replay](bugfix/jiuwenswarm/server/supervisor/B_20261107_1505_6226-forwarding-replay.md) | B | 历史回放触发转发队列溢出及前端计时不收尾 | kdgmpw | jiuwenswarm/server/supervisor, jiuwenswarm/channels/web/frontend | jiuwenswarm/server/supervisor/runtime_supervisor.py, jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts, tests/unit_tests/agentserver/test_supervisor_forwarding.py, jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs | 无独立 D：对现有有界转发及前端终态处理的小范围修复，无独立设计。 |
| [B_20261110_1042_issue6191-ipc-recovery](bugfix/jiuwenswarm/server/ipc/B_20261110_1042_issue6191-ipc-recovery.md) | B | IPC 突发积压与降级就绪重连导致 Runtime 失败 | 刘瑜 | jiuwenswarm/server/ipc, jiuwenswarm/server/supervisor | jiuwenswarm/server/ipc/stream.py, jiuwenswarm/server/supervisor/runtime_supervisor.py, tests/unit_tests/agentserver/test_ipc_receive_recovery.py | 无独立 D：对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。 |

<a id="module-33"></a>

## jiuwenswarm/server/worker

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261105_1518_worker-dispatch-slot-accounting](bugfix/jiuwenswarm/server/worker/B_20261105_1518_worker-dispatch-slot-accounting.md) | B | Worker 派发容量排除已返回的 handler | kdgmpw | jiuwenswarm/server/worker | jiuwenswarm/server/worker/event_store.py | 无独立 D：按会话中确认的最小修复方案调整 Worker 内部计数。 |

<a id="module-34"></a>

## scripts

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261111_1528_unified-observability-config](features/F_20261111_1528_unified-observability-config.md) | F | Team 与单 Agent 可观测性配置统一为 observability 段 | alan_cheng | jiuwenswarm/common, jiuwenswarm/agents/harness, jiuwenswarm/resources, deploy/yuanrong, scripts | jiuwenswarm/common/observability_config.py, jiuwenswarm/common/config.py, jiuwenswarm/agents/harness/agent_observability.py, jiuwenswarm/agents/harness/team/team_manager.py, jiuwenswarm/resources/config.yaml, deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml, scripts/reset_trajectory_data.py, tests/unit_tests/common/test_observability_config.py, tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py | 无独立 D：用户在会话中确认的配置收敛改动，无独立设计编号。 |
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-35"></a>

## tests

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261113_1040_del-ut-slim](features/F_20261113_1040_del-ut-slim.md) | F | develop 测试库瘦身第一批：删除 agent-gateway 镜像测试层与死代码副本 | kdgmpw | tests | TESTING.md, tests/unit_tests/channels/cli/test_chat.py, tests/unit_tests/test_config.py, tests/unit_tests/runtime/test_runtime_service.py, tests/unit_tests/common/test_mcp_dynamic_token_patch.py, tests/unit_tests/gateway/test_cli_channel_handlers.py, tests/unit_tests/server/test_session_metadata.py, tests/unit_tests/agentserver/test_team_model_selection.py, tests/unit_tests/agentserver/test_skill_sync.py, tests/unit_tests/agentserver/test_runtime_worker_phase2.py, tests/unit_tests/agentserver/test_team_helpers.py, tests/system_tests/test_project_memory.py, tests/auth/（整目录删除，4 个上游镜像测试）, tests/unit_tests/agentserver/memory/（删除 4 个镜像/占位测试）, tests/unit_tests/agentserver/test_skill_manager_rebuild_followup.py（删除）, tests/unit_tests/agentserver/test_envelope_placeholder.py（删除）, tests/integration/test_acp_cli_stdio_integration.py（删除）, tests/unit_tests/gateway/ 与 tests/unit_tests/channel/ 等（删除 65 个上游逐字节镜像测试，完整清单见本特性提交 diff） | 无独立 D：基于会话内对 develop 测试库的两轮静态扫描（10 个分组深扫 + 17,316 个用例级 AST 指纹比对）与上游 agent-gateway 仓的逐字节校验，用户在会话中直接确认执行，无独立设计编号。 |

<a id="module-36"></a>

## tests/fixtures/ttse_plugin

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-37"></a>

## tests/unit_tests/agentserver

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |
| [B_20261031_1048_ttse-core-dependent-ut](bugfix/tests/unit_tests/agentserver/B_20261031_1048_ttse-core-dependent-ut.md) | B | 暂时移除依赖未合入 core 工厂能力的插件 UT | xinyang_bali | tests/unit_tests/agentserver | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py | 无独立 D：用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。 |

<a id="module-38"></a>

## tests/unit_tests/gateway

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-39"></a>

## tests/unit_tests/personal_context

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1038_cron-migration-ut](bugfix/tests/unit_tests/B_20261031_1038_cron-migration-ut.md) | B | cron 会话迁移的实时流测试与类清点兼容修复 | kdgmpw | tests/unit_tests/gateway, tests/unit_tests/personal_context | tests/unit_tests/gateway/test_cron_live_bus.py, tests/unit_tests/personal_context/test_closed_class_inventory.py | 无独立 D：对现有测试契约和解释器兼容性的小范围修复，无独立设计。 |

<a id="module-40"></a>

## tests/unit_tests/server/extensions

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [B_20261031_1006_ttse-runtime-fixture](bugfix/tests/unit_tests/agentserver/B_20261031_1006_ttse-runtime-fixture.md) | B | TTSE runtime UT 使用版本化夹具修复整批 setup error | xinyang_bali | tests/unit_tests/agentserver, tests/unit_tests/server/extensions, tests/fixtures/ttse_plugin, scripts | tests/unit_tests/agentserver/test_ttse_plugin_runtime.py, tests/unit_tests/server/extensions/test_ttse_plugin_package.py, tests/unit_tests/conftest.py, tests/fixtures/ttse_plugin/manifest.json, tests/fixtures/ttse_plugin/config.yaml, tests/fixtures/ttse_plugin/rails/ttse_plugin_rail.py, scripts/package_ttse_plugin.py | 无独立 D：修复用户报告的 CI 测试数据缺失，保持现有 plugin_runtime v1 与生产默认打包路径，无独立 D。 |

<a id="module-41"></a>

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
