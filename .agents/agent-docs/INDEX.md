---
title: D/F/B/S 文档总索引
author: Yoggaboomin
author_email: yutao131@huawei.com
generated: true
---

# D/F/B/S 文档总索引

由 sync_documents.py 从文档 frontmatter 自动生成；请修改源文档后重新生成，不手工维护。

共 20 篇文档，按模块分组；跨模块文档在各相关分组展示，总数按文档去重。

## 模块导航

| 模块 | 文档数 |
|---|---|
| [agent_teams](#module-1) | 1 |
| [agent_teams/workflow](#module-2) | 3 |
| [gateway](#module-3) | 1 |
| [gateway/cron](#module-4) | 1 |
| [gateway/message_handler](#module-5) | 1 |
| [jiuwenswarm](#module-6) | 3 |
| [jiuwenswarm/agents/harness](#module-7) | 1 |
| [jiuwenswarm/agents/harness/common/tools/cron](#module-8) | 1 |
| [jiuwenswarm/channels/web/frontend](#module-9) | 1 |
| [jiuwenswarm/gateway/channel_manager/web](#module-10) | 1 |
| [jiuwenswarm/observability](#module-11) | 1 |
| [jiuwenswarm/server](#module-12) | 2 |
| [未分类](#module-13) | 12 |

<a id="module-1"></a>

## agent_teams

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0029](designs/proposals/SDD-0029-team-session-runtime-integration/design.md) | D | Team 模式接入统一 Session Runtime | @zhanglongmin | agent_teams | — | — |

<a id="module-2"></a>

## agent_teams/workflow

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |

<a id="module-3"></a>

## gateway

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) | D | cron 快照同步机制退役：数据面前置 RPC 删除与权威读收口 | Yoggaboomin | gateway, jiuwenswarm/agents/harness, jiuwenswarm/server | — | — |

<a id="module-4"></a>

## gateway/cron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261028_1135_cron-snapshot-retirement](features/F_20261028_1135_cron-snapshot-retirement.md) | F | cron 快照退役与权威收口实施 | Yoggaboomin | gateway/message_handler, gateway/cron, jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/server | gateway/message_handler/message_handler.py, gateway/cron/controller.py, jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py, jiuwenswarm/server/agent_ws_server.py, tests/unit_tests/agentserver/test_cron_runtime.py, tests/unit_tests/agentserver/test_a4p_split_process.py, tests/unit_tests/gateway/test_cron_controller_project_binding.py, tests/unit_tests/gateway/test_message_handler_cron_push.py, tests/unit_tests/gateway/test_cron_snapshot_sync_scope.py | [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) |

<a id="module-5"></a>

## gateway/message_handler

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261028_1135_cron-snapshot-retirement](features/F_20261028_1135_cron-snapshot-retirement.md) | F | cron 快照退役与权威收口实施 | Yoggaboomin | gateway/message_handler, gateway/cron, jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/server | gateway/message_handler/message_handler.py, gateway/cron/controller.py, jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py, jiuwenswarm/server/agent_ws_server.py, tests/unit_tests/agentserver/test_cron_runtime.py, tests/unit_tests/agentserver/test_a4p_split_process.py, tests/unit_tests/gateway/test_cron_controller_project_binding.py, tests/unit_tests/gateway/test_message_handler_cron_push.py, tests/unit_tests/gateway/test_cron_snapshot_sync_scope.py | [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) |

<a id="module-6"></a>

## jiuwenswarm

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0009](designs/archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | D | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0010](designs/archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | D | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | @chenchunzhou | agent_teams/workflow, jiuwenswarm | — | — |
| [SDD-0028](designs/archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | D | swarmflow verify / fork 可观测与前端呈现 | @xiajing | agent_teams/workflow, jiuwenswarm | — | — |

<a id="module-7"></a>

## jiuwenswarm/agents/harness

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) | D | cron 快照同步机制退役：数据面前置 RPC 删除与权威读收口 | Yoggaboomin | gateway, jiuwenswarm/agents/harness, jiuwenswarm/server | — | — |

<a id="module-8"></a>

## jiuwenswarm/agents/harness/common/tools/cron

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [F_20261028_1135_cron-snapshot-retirement](features/F_20261028_1135_cron-snapshot-retirement.md) | F | cron 快照退役与权威收口实施 | Yoggaboomin | gateway/message_handler, gateway/cron, jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/server | gateway/message_handler/message_handler.py, gateway/cron/controller.py, jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py, jiuwenswarm/server/agent_ws_server.py, tests/unit_tests/agentserver/test_cron_runtime.py, tests/unit_tests/agentserver/test_a4p_split_process.py, tests/unit_tests/gateway/test_cron_controller_project_binding.py, tests/unit_tests/gateway/test_message_handler_cron_push.py, tests/unit_tests/gateway/test_cron_snapshot_sync_scope.py | [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) |

<a id="module-9"></a>

## jiuwenswarm/channels/web/frontend

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0026](designs/proposals/SDD-0026-trajectory-observability-web-ui/design.md) | D | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | @alan_cheng | jiuwenswarm/channels/web/frontend | — | — |

<a id="module-10"></a>

## jiuwenswarm/gateway/channel_manager/web

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-11"></a>

## jiuwenswarm/observability

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [SDD-0025](designs/proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | D | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | @alan_cheng | jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web | — | — |

<a id="module-12"></a>

## jiuwenswarm/server

| ID / 文档 | 类型 | 标题 | 作者 | 模块 | 代码文件 | 关联 D |
|---|---|---|---|---|---|---|
| [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) | D | cron 快照同步机制退役：数据面前置 RPC 删除与权威读收口 | Yoggaboomin | gateway, jiuwenswarm/agents/harness, jiuwenswarm/server | — | — |
| [F_20261028_1135_cron-snapshot-retirement](features/F_20261028_1135_cron-snapshot-retirement.md) | F | cron 快照退役与权威收口实施 | Yoggaboomin | gateway/message_handler, gateway/cron, jiuwenswarm/agents/harness/common/tools/cron, jiuwenswarm/server | gateway/message_handler/message_handler.py, gateway/cron/controller.py, jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py, jiuwenswarm/server/agent_ws_server.py, tests/unit_tests/agentserver/test_cron_runtime.py, tests/unit_tests/agentserver/test_a4p_split_process.py, tests/unit_tests/gateway/test_cron_controller_project_binding.py, tests/unit_tests/gateway/test_message_handler_cron_push.py, tests/unit_tests/gateway/test_cron_snapshot_sync_scope.py | [D-20261028-1105-cron-snapshot-retirement](designs/active/D-20261028-1105-cron-snapshot-retirement/design.md) |

<a id="module-13"></a>

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
