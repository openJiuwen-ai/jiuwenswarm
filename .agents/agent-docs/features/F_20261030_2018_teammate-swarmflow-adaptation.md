---
title: 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配）
author: '@xiajing'
id: F_20261030_2018_teammate-swarmflow-adaptation
type: F
modules:
- jiuwenswarm
files:
- jiuwenswarm/agents/harness/team/handlers/workflow_state.py
- jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
designs: []
design_note: 本仓侧为 teammate 接入 swarmflow 的适配与硬化，无独立设计文档。
author_email: xiajing22@huawei.com
---

# 集群模式teammate支持swarmflow工具（jiuwenswarm 侧适配）

## 元信息

| 项 | 值 |
|---|---|
| 范围 | 状态层/advisory：workflow_state.py、workflow_monitor_handler.py、team_helpers.py、wire_truncate.py；前端归因展示：teamArea/ 下 RunGroupHeader.tsx、runGrouping.ts、TaskPlanningPanel.tsx、TeamMembersPanel.tsx、teamMemberSnapshot.ts，stores/teamSliceApply.ts，i18n/locales 的 zh/en.json，features/teamMemberSnapshot.ts（共 13 文件；frontmatter files 只登记状态层核心） |
| 测试基线 | 后端 UT：test_workflow_state 133、test_team_helpers（advisory 域 12）、test_workflow_monitor_handler、test_worker_dispatcher 等全绿；前端：run-grouping 10、team-member-snapshot、team-slice-apply、workflow-agent-identity 套件绿，tsc 0 错 |

## 实施要点

| 主题 | 实际做法 |
|---|---|
| 归因消费（后端） | `WorkflowProgress`/`WorkflowRunState` 单字段 `executor`（当前宿主，leader 也填名，每次 started 刷新）；delta 与全量快照序列化透传，徽章、叙述、注入、恢复转交同源 |
| 叙述路由 | 协调 handler 按 executor 匹配成员循环：run 的阶段叙述注入当前宿主上下文，leader 不再被淹没；executor 为 None（无团队上下文）时 leader 兜底；pause 不叙述（resume relaunch 的翻卡由重发的 WORKFLOW_STARTED + 缓存重放帧承担，无独立 resumed 事件） |
| 恢复转交 advisory | `[swarmflow-advisory]` 注入 leader 轮（同进程暂停与冷启动两形态都覆盖：first-request + follow-up 双注入点）；处理规则=用户点名直执行，否则 ask_user 单题（全部恢复/全部停止/暂不处理，「继续」不算点名）；恢复按 executor（最近执行者）三分支——在线成员 send_message 转交自恢复、leader 自己的 run 直调、成员不在团队走 launch plane 代跑 |
| 会话终止语义 | 前端终止（intent=cancel）让整队下线，在飞 run park 成 paused、不落 seal，保留恢复入口；后续消息里由 leader 的恢复 advisory 拉起。这是既定功能决策，终止不等于把 run stop 成终态 |
| 弹窗往返硬化 | `seen_human_waiting` handler 级状态去重（对齐既有四张同构表：seen_phase/seen_agent/spawned_members/seen_activity——delta 流对同一状态重复广播时 popup 检测不重发 ask_user）；skip reply 显式哨兵；P2 三层修复=chat.cancel 清场（`retire_session_interactions`）+ dispatcher 重放过滤（record 缺失 fail-close）+ 前端 ask_user_question request_id 级 dedup 守卫 |
| skip 哨兵依赖边界 | `HUMAN_REPLY_SKIP` 定义在 agent-core 配对分支（随 human-turn skip 逻辑同 commit 引入，develop 尚未合入）；claw 侧 `handle_swarmflow_reply` 的 import 收进 `if skip:` 分支——正常回答路径不触碰该符号，依赖钉版随 agent-core 合入后更新 |
| wire keep-keys | executor 进列表摘要 keep-keys（冷重开列表行可渲染徽章） |
| 前端归因展示 | 任务面板 list 视图（按 run 分组）、成员面板组头、聊天活动组头共用 `RunGroupHeader` 的单个 executor 徽章；工作流树/图视图、工具面板组头同样只画 executor 徽章；任务板（board）视图按状态分列、没有 run 组头，不显示徽章。run 分组折叠携带 executor，i18n 中英 |
| run 平铺不分成员 | leader 的 run 与 teammate 的 run **并排平铺在同一列表**（`groupByWorkflowRun` 的分组维度是 run，不是成员；TaskPlanningPanel 对全部 groups 单列表渲染）——不按成员隔离分栏、不做"我的/他的"折叠；归因差异只由组头 executor 徽章表达，这是产品决策：编排视图以 run 为一等公民 |
| 与灯（小飞机）的边界 | run 独立于对话轮存活，灯不由 run 决定；灯由任务在飞（框架层 BUSY）与 run-lamp（claw 层 run 活跃）两道 hold 撑住，teammate 的 run 通常包在任务里，灯亮满 run 全程。本特性不改灯，用户已裁决灯语义=任务全部完成+对话输出完成 |
| 镜像 worker | 组织 UI 快照对账合并不替换；带 workflow_run_id 的条目（镜像 worker）豁免清理——只存在于客户端，不构成移除证据。保留无需按 teamId 过滤：投影键在函数入口已按 conversationKey 重定向（owner → 裸 sessionId、专家 team → sessionId::teamId），且镜像 worker 事件的 team_id 是 workflow 名、expertTeamId 不认领——镜像 worker 只存在于 owner 主投影，专家投影互不相通，切 Team 不串（固化用例：applying an expert-team snapshot leaves the owner projection untouched） |

## 已知遗留

- **RPC 控制后运行方无感知**：run 被暂停/停止/恢复后，teammate 收不到通知，会继续等任务。暂不加回灌（回灌会唤醒一轮会话，有上下文污染先例），方向是超时后由运行方自行决策。
- **任务板视图没有归因徽章**：任务板按状态分列、没有 run 组头，executor 徽章只在任务面板 list 视图、工作流树/图视图和成员面板显示。
- **advisory 重复注入**：paused run 长期不处理时，每条 leader 消息都带同一份清单，展示层剔除但 token 逐轮占用。
- 弹窗文案：leader run 和同脚本的两个并发 run 仍没有 run 区分（run_id 短前缀未做）。
- external-CLI 成员受 AsyncTool 契约限制不接入（一轮同步工具通道承载不了后台 run
  与 follow-up 注入）。
- 跨重启重放的 run 现势过滤待真实复现验证。

## 验证结论

后端 UT（workflow_state/team_helpers/monitor_handler/dispatcher/residency/
recovery/cron_snapshot/team_completion）全绿；前端 run-grouping（含 executor
徽章用例）、team-member-snapshot、team-slice-apply 全绿。wire 级
端到端（advisory 弹窗转交三分支、human 往返、暂停静默）经场景验证：转交 5/5、
对照 5/5、回退 6/6，P2 修复后弹窗重投 ST 11/11 零重放帧。
