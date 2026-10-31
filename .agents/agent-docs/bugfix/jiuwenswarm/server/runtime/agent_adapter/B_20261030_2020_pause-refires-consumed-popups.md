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
  - jiuwenswarm/server/supervisor/worker_dispatcher.py
  - jiuwenswarm/server/runtime/agent_adapter/worker_event_store.py
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
designs: []
design_note: 三层链根因（触发层不清场→重放层带过期帧→消费层无 dedup）的组合修复，弹窗往返语义权威在 agent-core SDD-0031 §5。
---

# 暂停/取消后已消费的 human 弹窗被重放层重投

## 现象与影响

human 弹窗等待中用户点会话暂停（或取消），每次暂停后弹窗毫秒级重投：用户对同一
问题被反复打扰，且对已停止的 run 再答会造成状态错乱。

## 根因（三层链）

1. **触发层**：pause 对 `WorkerEventStore` 不清场——被 interrupt 的轮永远保持
   `is_processing=true` + pending ask_user interaction，任何后续 get_metadata 看
   到 processing 就补 `runtime_resume after:0` 触发全量重放。
2. **重放层**：dispatcher 重放 buffered 帧时不滤已过期的 interaction 帧，已消费弹
   掉的 ask_user 原样重上 wire（replayed 标记不阻止消费）。
3. **消费层**：前端 `useWebSocket` 的 `chat.ask_user_question` 处理器是全文件唯一
   没有去重守卫的事件，重放帧重新入队弹窗。

## 修复决策（三层各堵一段）

| 层 | 修复 | 否决过的方案 |
|---|---|---|
| 状态层 | `retire_session_interactions`：chat.cancel 派发时清该会话 pending interactions + is_processing（掐掉触发条件） | — |
| 重放层 | dispatcher 重放前滤 interaction 帧，**record 缺失 = fail-close**（首修版 fail-open 被自建 ST 暴露后门：LRU 清除后 record 缺失误判 pending 照样重投，翻正。缺 record 本身就是已清场的证据；live 轮的 frame 必有 record，无误滤路径） | fail-open 判定（被 ST 复跑证伪后否决） |
| 消费层 | 前端 ask_user_question 补 request_id 级 dedup 守卫（与 chat.final/tool_call 同款） | — |

## 验证

重投场景 ST 11/11 PASS（暂停 6 + 取消 5）：修复后全窗零 replayed ask_user 帧、各
场景恰 1 条 live 弹窗；agentserver worker 域 UT 全绿（residency/recovery/
cron_snapshot/team_completion 158 passed）。

## 关联

重连幽灵弹窗（回放过期 interaction）同根因族，消费层 dedup 守卫对其同效收口；跨
重启重放的 run 现势过滤留观察（真实复现再议）。
