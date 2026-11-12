---
id: B_20261112_1405_stop-btn-stuck-after-skip
title: 集群模式跳过后停止按钮永久卡死：interrupt 收尾窗口护栏
type: B
author: xiajing
author_email: xiajing22@huawei.com
designs: []
design_note: 依据现场日志与轨迹定位，无独立设计文档；修复范围限定前端状态机护栏，并补后端判据不变量测试。
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/agents/harness/team
files:
  - jiuwenswarm/channels/web/frontend/src/features/interruptSettleGuard.ts
  - jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/tests/interruptSettleGuard.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
  - tests/unit_tests/agentserver/test_team_manager_registry.py
---

# 集群模式跳过后停止按钮永久卡死

## 现象

集群模式（team mode）+ 动态工作流的会话中，人工参与节点（human）被「跳过」后，
工作流已进入终态（`workflow_completed`，`human_answer=null`），HumanConfirm
节点标记 `human session 'human' failed: backend declined (skipped)`，但输入框
持续显示「集群模式处理中，输入新消息会并行执行...」，底部停止按钮连点多次
无任何 UI 变化。

被误判为「按钮点不动」，实际每次点击都发出了
`chat.interrupt{intent:"pause"}`、后端全部 `success=true`，并回
`chat.processing_status{is_processing:false}`——请求链路是通的，是前端状态
被反复点亮。

## 根因

**一句话**：pause ack 收尾本轮时把 `isPaused` 复位，而 worker 运行态快照仍
报 `is_processing=true`；该快照经「metadata 回写」与「本地合成的
processing_status 事件」两条通道把 `foregroundIsProcessing` 重新点亮，
此后后端已收尾不再发任何终态事件，界面永久停在「处理中 + 停止按钮」。

链路：

1. 用户点停止 → `chat.interrupt{intent:"pause"}`；
2. 后端 ack：`success=true`、`has_active_task=false`（Gateway 判定自身无活跃
   流任务）；
3. 前端 `useWebSocket.ts:5451` 执行 `setPaused(false)`，同时 `:5456`
   `setProcessing(false)` —— UI 短暂恢复；
4. 同一时刻 `session.get_metadata` 的运行态快照仍是 `is_processing=true`：
   - `App.tsx:2110` 按「快照只能拉起、打回空闲只认终态事件」的设计回写
     `setProcessing(sid, true)`；
   - `webClient` 的 `resumeExecution` 看到 `runtime.is_processing=true`，
     在本地合成一个不带 `event_type` 的 `chat.processing_status{is_processing:true}`
     事件，同样进入 `useWebSocket.ts:5025 setProcessing(true)`；
5. 第 3 步已把 `isPaused` 置 false，`useWebSocket.ts:5019`
   `if (isProcessingNow && isPaused) return;` 的唯一护栏失效；
6. 后端此后不再产生终态事件 → 无任何路径能熄灭，界面永久卡死。

**判定证据**：复现窗口内抓到的完整 WS 帧序列显示，stop 之后**不存在任何
`chat.processing_status(true)` 线帧**（全部为 `is_processing=false`），说明
重亮只可能来自上述两条本地通道。

**快照为何仍报 processing（部分确证）**：`session.get_metadata` 的
`runtime.is_processing` 来自 worker `server/worker/dispatcher.py:695`
`_subscribe_locked`，其中 `:710/:731` 依据 work-record 的 `is_processing`
标志聚合。该标志只在 `chat.final` / `chat.error` / `runtime.completed` /
`CHAT_CANCEL` 时退休（`server/worker/event_store.py:220-241`），pause 不产生
这些终态，记录残留 `true`。抓取到的快照形态为
`dispatches=[] + executions=[] + is_processing=true`，仅 work-record 残留可
解释，但未取得记录级明细，故本次不改 `dispatcher.py`，只以单测锁住
`is_team_session_running` 的判据语义。

## 引入提交

本缺陷由三段独立设计叠加造成，非单一提交引入：

| 失效点 | 提交 | 作者 | 日期 |
|---|---|---|---|
| `App.tsx` metadata 快照回写 `setProcessing(sid, is_processing)` | `7a3924ee13d` | zhangxiangyu52 | 2026-11-09 |
| 固化「快照只能拉起、打回空闲只认终态事件」的守卫结构 | `b338268b1ca` | kdgmpw | 2026-11-11 |
| `webClient` 用快照本地合成 `chat.processing_status` 事件 | `9c472b2d9db` | hw_liuyu | 2026-10-16 |
| pause ack 成功后 `setPaused(false)` 复位 | `129d13aa0ce` | zhengkailan | 2026-08-10 |
| `isProcessingNow && isPaused` 护栏（被上一条拆掉） | `953a77de1de` | douran | 2026-07-16 |

三者的组合后果：`9c472b2d9db` 让快照能以「假线帧」形式进入事件总线；
`7a3924ee13d` + `b338268b1ca` 使其只具备「拉起」能力、不具备「回落」能力；
`129d13aa0ce` 在 ack 中复位 `isPaused`，恰好移除 `953a77de1de` 的唯一护栏。

## 修复

前端新增「interrupt 收尾窗口」（`features/interruptSettleGuard.ts` +
`chatStore.interruptSettledEpoch`）：

- `chat.interrupt_result` 的 pause/cancel **成功** ack 时置位；
- 窗口期内：快照回写不得把 processing 拉起
  （`App.tsx` 经 `maySnapshotRelightProcessing`；断线重连的首次快照
  `allowDowngrade` 不受此限，保留恢复运行态的合法通道）；
- 窗口期内：无 `event_type` 的合成 `processing_status(true)` 不得点亮
  （`mayProcessingTrueFrameRelight`）；
- 窗口结束：用户发起新一轮发送（`sendMessage` 乐观置忙处 clear），或收到
  真实线帧 `processing_status(true)`（带 `event_type`，handler 内 clear）。

后端不改生产代码，仅在 `tests/unit_tests/agentserver/test_team_manager_registry.py`
新增 6 个不变量用例，锁住 `is_team_session_running` 在
`PAUSING` / `PAUSED` / `RUNNING` / 无状态与「paused run + paused 成员」下的
返回值语义（当前行为经测试证明已正确），为后续 dispatcher 侧修复提供判据
基线。

## 复现场景

环境：集群模式 + 动态工作流打开；human 节点位于脚本早期（其后仍有 agent
节点）。

提示词：

```
组建一个团队，成员 id 用 analyst；派任务给它，任务内容明确要求 analyst 调用 swarmflow 编排一个脚本，脚本里先调用一次人类参与节点提问「是否继续生成报告？」，等我回答后再输出一句结论并汇报
```

步骤：

1. 新建会话，发送上述提示词；
2. 等待「问题澄清」卡片出现，内容为「是否继续生成报告？」；
3. 点击「跳过」（不作答）；
4. **6 秒内**开始点击底部停止按钮，连点 3 次（间隔约 2 秒）；
5. 观察输入框占位与按钮形态。

判定：失败表现为输入框持续显示「集群模式处理中，输入新消息会并行执行...」、
按钮保持停止态（`data-variant=stop`、未禁用），且网络面板中每次点击均有
`chat.interrupt{intent:"pause"}` 且返回成功。

对照实验（用于区分是否同一缺陷）：

| 变体 | 结果 |
|---|---|
| 跳过后等待 60 秒再点停止 | 点击一次即恢复发送态 |
| human 节点为脚本最后一个节点 | 不卡死（脚本直接结束，无「成员仍在执行」窗口） |
| 跳过后 6 秒内连点 3 次 | 卡死 |

时序敏感，属竞态：跳过会唤醒成员继续执行 turn，「工作流已终态」与「成员仍
在跑」的窗口重叠时，停止操作会落入上述重亮链路。

## 验证

- `npm run test:interrupt-settle`：4 用例通过（含按复现帧序列重放：
  ack 收尾 → 快照报 processing → 合成帧 → 保持空闲 → 新发送恢复）；
- `npm run test:chat-store-streaming`：10 用例通过；
- `npx tsc --noEmit -p tsconfig.json`：无错误；
- `pytest tests/unit_tests/agentserver/test_team_manager_registry.py -k team_running`：
  14 用例通过（含 6 个新增不变量用例）；
- 端到端复跑：未完成（卡死为窄窗口竞态，多轮自动化未再命中），以帧序列
  重放单测替代。

## 未确证项

1. worker 快照中 work-record 残留 `is_processing=true` 的记录级证据（推断
   充分但未取得卡死态探针明细）；
2. `has_active_task` 在 `false` / `true` 间交替的 Gateway 侧成因（该字段由
   Gateway 计算，与 worker 运行态不同源）；
3. pause 终态帧未携带 `request_id`，前端事件去重 key 退化为整段 payload
   （窗口 1500ms），1.5 秒内连续点击的终态帧会被丢弃——仅解释「后几次点击
   无效」，不是主因，本次未修。

## 遗留风险

- 收尾窗口无超时：若后端长期误报 processing 且用户不再发送新消息，切会话或
  刷新页面仍可经 `allowDowngrade` / 无标记路径恢复；该窗口只阻止「拉起」，
  不阻止「回落」，不会造成新的卡死。
- 队列自动排空若依赖合成的 processing 事件触发，理论上有单帧延迟；真实线帧
  到达即恢复。
