---
title: 设计文档索引
author: sync_documents.py
generated: true
---

<!-- 本文件由 .agents/skills/jw-design/scripts/generate_index.py 自动生成，请勿手改。重新生成：python .agents/skills/jw-design/scripts/generate_index.py -->

# 设计文档索引 (INDEX)

按模块目录分组，组内按生命周期展示；跨模块设计在各相关模块下列出。

## 模块目录

| 模块 |
|---|
| [agent-core/openjiuwen/agent_teams](#module-agent-core-openjiuwen-agent_teams) |
| [agent_teams](#module-agent_teams) |
| [agent_teams/workflow](#module-agent_teams-workflow) |
| [docs](#module-docs) |
| [jiuwenswarm](#module-jiuwenswarm) |
| [jiuwenswarm/agents/harness/common](#module-jiuwenswarm-agents-harness-common) |
| [jiuwenswarm/agents/harness/team](#module-jiuwenswarm-agents-harness-team) |
| [jiuwenswarm/agents/swarm](#module-jiuwenswarm-agents-swarm) |
| [jiuwenswarm/channels/web/frontend](#module-jiuwenswarm-channels-web-frontend) |
| [jiuwenswarm/channels/web/frontend/src/features/settings](#module-jiuwenswarm-channels-web-frontend-src-features-settings) |
| [jiuwenswarm/common](#module-jiuwenswarm-common) |
| [jiuwenswarm/common/config_panel](#module-jiuwenswarm-common-config_panel) |
| [jiuwenswarm/extensions/app_market](#module-jiuwenswarm-extensions-app_market) |
| [jiuwenswarm/gateway/channel_manager/web](#module-jiuwenswarm-gateway-channel_manager-web) |
| [jiuwenswarm/observability](#module-jiuwenswarm-observability) |
| [jiuwenswarm/resources/agent/workspace/skills](#module-jiuwenswarm-resources-agent-workspace-skills) |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin) |
| [jiuwenswarm/runtime](#module-jiuwenswarm-runtime) |
| [jiuwenswarm/server](#module-jiuwenswarm-server) |
| [jiuwenswarm/server/control](#module-jiuwenswarm-server-control) |
| [jiuwenswarm/server/jiuwenmate](#module-jiuwenswarm-server-jiuwenmate) |
| [jiuwenswarm/server/runtime](#module-jiuwenswarm-server-runtime) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-jiuwenswarm-server-runtime-agent_adapter) |
| [jiuwenswarm/server/runtime/session](#module-jiuwenswarm-server-runtime-session) |
| [jiuwenswarm/server/runtime/skill](#module-jiuwenswarm-server-runtime-skill) |
| [openjiuwen/agent_teams](#module-openjiuwen-agent_teams) |

<a id="module-agent-core-openjiuwen-agent_teams"></a>

## agent-core/openjiuwen/agent_teams

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261120-1701-swarmchat-auto-reply](proposals/D-20261120-1701-swarmchat-auto-reply/design.md) | SwarmChat 群成员自动判断是否回复设计方案 | draft | weichenhao | messaging | 2026-11-24 |

<a id="module-agent_teams"></a>

## agent_teams

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0029](proposals/SDD-0029-team-session-runtime-integration/design.md) | Team 模式接入统一 Session Runtime | draft | @zhanglongmin | core | 2026-10-09 |

<a id="module-agent_teams-workflow"></a>

## agent_teams/workflow

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-docs"></a>

## docs

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm"></a>

## jiuwenswarm

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-jiuwenswarm-agents-harness-common"></a>

## jiuwenswarm/agents/harness/common

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-agents-harness-team"></a>

## jiuwenswarm/agents/harness/team

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261111-1722-team-goal-config](active/D-20261111-1722-team-goal-config/design.md) | Swarm 团队 Goal 配置接入方案 | in-development | alan_cheng | harness | 2026-11-11 |

<a id="module-jiuwenswarm-agents-swarm"></a>

## jiuwenswarm/agents/swarm

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261120-1701-swarmchat-auto-reply](proposals/D-20261120-1701-swarmchat-auto-reply/design.md) | SwarmChat 群成员自动判断是否回复设计方案 | draft | weichenhao | messaging | 2026-11-24 |

<a id="module-jiuwenswarm-channels-web-frontend"></a>

## jiuwenswarm/channels/web/frontend

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261120-1701-swarmchat-auto-reply](proposals/D-20261120-1701-swarmchat-auto-reply/design.md) | SwarmChat 群成员自动判断是否回复设计方案 | draft | weichenhao | messaging | 2026-11-24 |
| [SDD-0026](proposals/SDD-0026-trajectory-observability-web-ui/design.md) | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-jiuwenswarm-channels-web-frontend-src-features-settings"></a>

## jiuwenswarm/channels/web/frontend/src/features/settings

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261111-1722-team-goal-config](active/D-20261111-1722-team-goal-config/design.md) | Swarm 团队 Goal 配置接入方案 | in-development | alan_cheng | harness | 2026-11-11 |

<a id="module-jiuwenswarm-common"></a>

## jiuwenswarm/common

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-jiuwenswarm-common-config_panel"></a>

## jiuwenswarm/common/config_panel

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261111-1722-team-goal-config](active/D-20261111-1722-team-goal-config/design.md) | Swarm 团队 Goal 配置接入方案 | in-development | alan_cheng | harness | 2026-11-11 |

<a id="module-jiuwenswarm-extensions-app_market"></a>

## jiuwenswarm/extensions/app_market

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-jiuwenswarm-gateway-channel_manager-web"></a>

## jiuwenswarm/gateway/channel_manager/web

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-jiuwenswarm-observability"></a>

## jiuwenswarm/observability

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-jiuwenswarm-resources-agent-workspace-skills"></a>

## jiuwenswarm/resources/agent/workspace/skills

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-jiuwenswarm-resources-agent-workspace-skills-workswarm-builtin"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-jiuwenswarm-runtime"></a>

## jiuwenswarm/runtime

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-server"></a>

## jiuwenswarm/server

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-server-control"></a>

## jiuwenswarm/server/control

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261111-1722-team-goal-config](active/D-20261111-1722-team-goal-config/design.md) | Swarm 团队 Goal 配置接入方案 | in-development | alan_cheng | harness | 2026-11-11 |

<a id="module-jiuwenswarm-server-jiuwenmate"></a>

## jiuwenswarm/server/jiuwenmate

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261120-1701-swarmchat-auto-reply](proposals/D-20261120-1701-swarmchat-auto-reply/design.md) | SwarmChat 群成员自动判断是否回复设计方案 | draft | weichenhao | messaging | 2026-11-24 |

<a id="module-jiuwenswarm-server-runtime"></a>

## jiuwenswarm/server/runtime

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261120-1701-swarmchat-auto-reply](proposals/D-20261120-1701-swarmchat-auto-reply/design.md) | SwarmChat 群成员自动判断是否回复设计方案 | draft | weichenhao | messaging | 2026-11-24 |

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-server-runtime-agent_adapter"></a>

## jiuwenswarm/server/runtime/agent_adapter

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-server-runtime-session"></a>

## jiuwenswarm/server/runtime/session

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-jiuwenswarm-server-runtime-skill"></a>

## jiuwenswarm/server/runtime/skill

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-openjiuwen-agent_teams"></a>

## openjiuwen/agent_teams

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261111-1722-team-goal-config](active/D-20261111-1722-team-goal-config/design.md) | Swarm 团队 Goal 配置接入方案 | in-development | alan_cheng | harness | 2026-11-11 |
