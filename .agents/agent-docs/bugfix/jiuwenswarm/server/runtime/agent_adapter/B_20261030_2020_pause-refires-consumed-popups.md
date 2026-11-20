---
id: B_20261030_2020_pause-refires-consumed-popups
title: 暂停/取消后已消费的 human 弹窗被重放层重投
type: B
author: xiajing
author_email: xiajing22@huawei.com
modules:
  - jiuwenswarm
files:
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
designs: []
design_note: 重放层过滤由主干既有机制承载，本修复补触发层清场与消费层去重两段。
---

# 暂停/取消后已消费的 human 弹窗被重放层重投

## 现象与影响

human 弹窗等待中用户点会话暂停（或取消），每次暂停后弹窗毫秒级重投：用户对同一
问题被反复打扰，且对已停止的 run 再答会造成状态错乱。

## 根因

- **触发层**：pause 对 `WorkerEventStore` 不清场——被 interrupt 的轮永远保持
  `is_processing=true` + pending ask_user interaction，任何后续 get_metadata 看到
  processing 就补 `runtime_resume after:0` 触发全量重放。
- **消费层**：前端 `useWebSocket` 的 `chat.ask_user_question` 处理器是全文件唯一
  没有去重守卫的事件，重放帧重新入队弹窗。

## 修复决策

| 层 | 修复 | 否决过的方案 |
|---|---|---|
| 触发层 | `retire_session_interactions`：chat.cancel 派发时清该会话 pending interactions + is_processing（掐掉重放触发条件） | — |
| 消费层 | 前端 ask_user_question 补 request_id 级 dedup 守卫（与 chat.final/tool_call 同款） | — |

## 验证

重投场景 ST 11/11 PASS（暂停 6 + 取消 5）：修复后全窗零 replayed ask_user 帧、各
场景恰 1 条 live 弹窗；agentserver worker 域 UT 全绿（residency/recovery/
cron_snapshot/team_completion 158 passed）。

## 关联

重连幽灵弹窗（回放过期 interaction）同根因族，消费层 dedup 守卫对其同效收口；跨
重启重放的 run 现势过滤留观察（真实复现再议）。
