---
id: S_01_goal-assessment-trajectory
title: Goal 评估轨迹的宿主投影契约
type: S
author: alan_cheng
author_email: chengshuo5@huawei.com
updated: 2026-11-12
modules:
  - jiuwenswarm/observability
  - jiuwenswarm/channels/web/frontend/src/features/trajectory
files:
  - jiuwenswarm/observability/retention.py
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/GoalAssessments.tsx
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/projector/otel-trajectory-projector.ts
---

# Goal 评估轨迹的宿主投影契约

core 在 agent-core 的 D-20261112-1120-goal-assessment-isolation 及 harness/S_11_goal-and-evaluation.md 维护用途与上下文隔离契约；本规约仅维护 Swarm 读者责任。

1. 显式 purpose=goal_assessment 的物理模型请求属于原执行 subject，不创建团队成员，不改变 session 分区或所有者。
2. 投影前将评估 inference 与其同用途子记录从执行窗口分出。执行轮的 goal id 与 attempt number 用于标注 Goal 轮次；Goal 驱动的工作仍留在执行轨迹。
3. 每个评估请求使用独立输入基线，不能污染执行窗口差分。评估请求、system/input/output/source/OTel 及 usage 在独立折叠区可达。
4. 评估展示模型返回的 continue/complete/blocked；调用失败单独显示。调用完成但无有效结构化结论不能显示为目标完成。模型结论不是 GoalManager 最终状态的替代来源。
5. 执行请求与评估请求分项计数；物理调用、原始记录和会话级累计 usage 不丢弃、不重复计入。兼容重试的物理请求仍逐次保留。
6. 保留策略统计评估的物理请求编号偏移，但不把评估输入输出保存成执行 lineage seed。实时、归档、分页及保留 checkpoint 后投影使用相同规则。
7. 缺少用途标记的旧数据按原始记录处理，不通过 system 文本、工具名称或 JSON 内容猜测用途，不改写用户原轨迹。

实现与验证：[F_20261112_1120_goal-assessment-view](../../../features/F_20261112_1120_goal-assessment-view.md)。
