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
| [agent_teams](#module-agent_teams) |
| [agent_teams/workflow](#module-agent_teams-workflow) |
| [jiuwenswarm](#module-jiuwenswarm) |
| [jiuwenswarm/channels/web/frontend](#module-jiuwenswarm-channels-web-frontend) |
| [jiuwenswarm/gateway/channel_manager/web](#module-jiuwenswarm-gateway-channel_manager-web) |
| [jiuwenswarm/observability](#module-jiuwenswarm-observability) |

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

<a id="module-jiuwenswarm"></a>

## jiuwenswarm

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-jiuwenswarm-channels-web-frontend"></a>

## jiuwenswarm/channels/web/frontend

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0026](proposals/SDD-0026-trajectory-observability-web-ui/design.md) | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | draft | @alan_cheng | observability | 2026-09-15 |

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
