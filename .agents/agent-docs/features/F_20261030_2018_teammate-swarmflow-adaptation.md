---
title: teammate 自主编排 swarmflow（jiuwenswarm 侧适配）
author: '@xiajing'
id: F_20261030_2018_teammate-swarmflow-adaptation
type: F
modules:
- jiuwenswarm
files:
- jiuwenswarm/agents/harness/team/handlers/workflow_state.py
- jiuwenswarm/agents/harness/team/handlers/workflow_monitor_handler.py
- jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
- jiuwenswarm/server/wire_truncate.py
- jiuwenswarm/channels/web/frontend/src/components/teamArea/RunGroupHeader.tsx
- jiuwenswarm/channels/web/frontend/src/components/teamArea/runGrouping.ts
- jiuwenswarm/channels/web/frontend/src/components/teamArea/TaskPlanningPanel.tsx
- jiuwenswarm/channels/web/frontend/src/components/teamArea/TeamMembersPanel.tsx
- jiuwenswarm/channels/web/frontend/src/components/teamArea/teamMemberSnapshot.ts
- jiuwenswarm/channels/web/frontend/src/stores/teamSliceApply.ts
- jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
- jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
- jiuwenswarm/channels/web/frontend/src/features/teamMemberSnapshot.ts
designs:
- agent-core:SDD-0031
design_note: 设计依据在 agent-core 仓 SDD-0031（跨仓共享设计，归因/转交/human 硬化的语义权威）
author_email: xiajing22@huawei.com
---

# teammate 自主编排 swarmflow（jiuwenswarm 侧适配）

## 元信息

| 项 | 值 |
|---|---|
| 范围 | 见 frontmatter files（状态层/advisory/wire/前端归因展示，13 文件） |
| 测试基线 | 后端 UT：test_workflow_state 133、test_team_helpers（advisory 域 12）、test_workflow_monitor_handler、test_worker_dispatcher 等全绿；前端：run-grouping 10、team-member-snapshot、team-slice-apply、workflow-agent-identity 套件绿，tsc 0 错 |
| 关联设计 | agent-core SDD-0031（executor 归因/叙述路由/转交三分支/human 硬化语义） |

## 实施要点

| 主题 | 实际做法 |
|---|---|
| 归因消费（后端） | `WorkflowProgress`/`WorkflowRunState` 单字段 `executor`（当前宿主，leader 也填名，每次 started 刷新）；delta 与全量快照序列化透传，徽章、叙述、注入、恢复转交同源 |
| 叙述路由 | 协调 handler 按 executor 匹配成员循环：run 的阶段叙述注入当前宿主上下文，leader 不再被淹没；executor 为 None（无团队上下文）时 leader 兜底；pause 不叙述（resume relaunch 的翻卡由重发的 WORKFLOW_STARTED + 缓存重放帧承担，无独立 resumed 事件） |
| 恢复转交 advisory | `[swarmflow-advisory]` 注入 leader 轮（同进程暂停与冷启动两形态都覆盖：first-request + follow-up 双注入点）；处理规则=用户点名直执行，否则 ask_user 单题（全部恢复/全部停止/暂不处理，「继续」不算点名）；恢复按 executor（最近执行者）三分支——在线成员 send_message 转交自恢复、leader 自己的 run 直调、成员不在团队走 launch plane 代跑 |
| 会话终止语义 | 前端终止（intent=cancel）让整队下线，在飞 run park 成 paused、不落 seal，保留恢复入口；后续消息里由 leader 的恢复 advisory 拉起。这是既定功能决策，终止不等于把 run stop 成终态 |
| 弹窗往返硬化 | `seen_human_waiting` handler 级状态去重（对齐既有四张同构表：seen_phase/seen_agent/spawned_members/seen_activity——delta 流对同一状态重复广播时 popup 检测不重发 ask_user）；skip reply 显式哨兵；P2 三层修复=chat.cancel 清场（`retire_session_interactions`）+ dispatcher 重放过滤（record 缺失 fail-close）+ 前端 ask_user_question request_id 级 dedup 守卫 |
| wire keep-keys | executor 进列表摘要 keep-keys（冷重开列表行可渲染徽章） |
| 前端归因展示 | 任务面板 list 视图（按 run 分组）、成员面板组头、聊天活动组头共用 `RunGroupHeader` 的单个 executor 徽章；工作流树/图视图、工具面板组头同样只画 executor 徽章；任务板（board）视图按状态分列、没有 run 组头，不显示徽章。run 分组折叠携带 executor，i18n 中英 |
| 与灯（小飞机）的边界 | run 独立于对话轮存活，灯不由 run 决定；灯由任务在飞（框架层 BUSY）与 run-lamp（claw 层 run 活跃）两道 hold 撑住，teammate 的 run 通常包在任务里，灯亮满 run 全程。本特性不改灯，用户已裁决灯语义=任务全部完成+对话输出完成 |
| 镜像 worker | 组织 UI 快照对账合并不替换；带 workflow_run_id 的条目（镜像 worker）豁免清理——只存在于客户端，不构成移除证据 |

## 已知遗留

- **RPC 控制后运行方无感知**：run 被暂停/停止/恢复后，teammate 收不到通知，会继续等任务。暂不加回灌（回灌会唤醒一轮会话，有上下文污染先例），方向是超时后由运行方自行决策。
- **任务板视图没有归因徽章**：任务板按状态分列、没有 run 组头，executor 徽章只在任务面板 list 视图、工作流树/图视图和成员面板显示。
- **advisory 重复注入**：paused run 长期不处理时，每条 leader 消息都带同一份清单，展示层剔除但 token 逐轮占用。
- 弹窗文案：leader run 和同脚本的两个并发 run 仍没有 run 区分（run_id 短前缀未做）。
- external-CLI 成员受 AsyncTool 契约限制不接入（一轮同步工具通道承载不了后台 run
  与 follow-up 注入），细节见 agent-core SDD-0031 §6。
- 跨重启重放的 run 现势过滤待真实复现验证。

## 验证结论

后端 UT（workflow_state/team_helpers/monitor_handler/dispatcher/residency/
recovery/cron_snapshot/team_completion）全绿；前端 run-grouping（含 executor
徽章用例）、team-member-snapshot、team-slice-apply 全绿。wire 级
端到端（advisory 弹窗转交三分支、human 往返、暂停静默）经场景验证：转交 5/5、
对照 5/5、回退 6/6，P2 修复后弹窗重投 ST 11/11 零重放帧。
