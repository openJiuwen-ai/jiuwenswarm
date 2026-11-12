---
title: 设计文档索引
author: kdgmpw
author_email: lishuqi4@huawei.com
generated: true
---

<!-- 本文件由 .agents/skills/jw-design/scripts/generate_index.py 自动生成，请勿手改。重新生成：python .agents/skills/jw-design/scripts/generate_index.py -->
<!-- generated-at: 2026-11-09T08:52+08:00 -->

# 设计文档索引 (INDEX)

> 开发前 3 · 开发中 3 · 开发后 3 · 已否决 0

按模块目录分组，组内按生命周期展示；跨模块设计在各相关模块下列出，全局统计按文档去重。

## 模块目录

| 模块 | 设计数 |
|---|---|
| [agent_teams](#module-1) | 1 |
| [agent_teams/workflow](#module-2) | 3 |
| [docs](#module-3) | 1 |
| [jiuwenswarm](#module-4) | 3 |
| [jiuwenswarm/agents/harness/common](#module-5) | 2 |
| [jiuwenswarm/channels/web/frontend](#module-6) | 1 |
| [jiuwenswarm/common](#module-7) | 1 |
| [jiuwenswarm/extensions/app_market](#module-8) | 1 |
| [jiuwenswarm/gateway/channel_manager/web](#module-9) | 1 |
| [jiuwenswarm/observability](#module-10) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills](#module-11) | 1 |
| [jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin](#module-12) | 1 |
| [jiuwenswarm/runtime](#module-13) | 2 |
| [jiuwenswarm/server](#module-14) | 1 |
| [jiuwenswarm/server/runtime](#module-15) | 1 |
| [jiuwenswarm/server/runtime/agent_adapter](#module-16) | 2 |
| [jiuwenswarm/server/runtime/session](#module-17) | 1 |
| [jiuwenswarm/server/runtime/skill](#module-18) | 1 |

<a id="module-1"></a>

## agent_teams

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0029](proposals/SDD-0029-team-session-runtime-integration/design.md) | Team 模式接入统一 Session Runtime | draft | @zhanglongmin | core | 2026-10-09 |

<a id="module-2"></a>

## agent_teams/workflow

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-3"></a>

## docs

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-4"></a>

## jiuwenswarm

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-5"></a>

## jiuwenswarm/agents/harness/common

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-6"></a>

## jiuwenswarm/channels/web/frontend

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0026](proposals/SDD-0026-trajectory-observability-web-ui/design.md) | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-7"></a>

## jiuwenswarm/common

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-8"></a>

## jiuwenswarm/extensions/app_market

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-9"></a>

## jiuwenswarm/gateway/channel_manager/web

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-10"></a>

## jiuwenswarm/observability

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-11"></a>

## jiuwenswarm/resources/agent/workspace/skills

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-12"></a>

## jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |

<a id="module-13"></a>

## jiuwenswarm/runtime

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-14"></a>

## jiuwenswarm/server

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-15"></a>

## jiuwenswarm/server/runtime

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-16"></a>

## jiuwenswarm/server/runtime/agent_adapter

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1444-app-role-session](active/D-20261103-1444-app-role-session/design.md) | 应用角色会话的最小提示词运行配置 | in-development | kdgmpw | harness | 2026-11-03 |
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-17"></a>

## jiuwenswarm/server/runtime/session

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261104-1141-npc-team-member-sessions](active/D-20261104-1141-npc-team-member-sessions/design.md) | NPC 主从会话与按需搜索工具 | in-development | kdgmpw | harness | 2026-11-04 |

<a id="module-18"></a>

## jiuwenswarm/server/runtime/skill

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261103-1123-builtin-skill-catalog](active/D-20261103-1123-builtin-skill-catalog/design.md) | 内置 Skill 总目录与按需加载设计方案 | in-development | kdgmpw | harness | 2026-11-03 |

## ⚠ 无法解析（已跳过，请运行 validate 修复）

- `proposals/D-20261103-1444-app-role-session/design.md` — 文件夹缺少 design.md
