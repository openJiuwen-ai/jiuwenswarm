---
id: B_20261128_1659_team-sidebar-spin-idle-sync
title: 专家团结束后侧栏空转——metadata idle 强制灭本地处理中灯
type: B
author: zhangxiangyu52
author_email: zhangxiangyu52@huawei.com
designs: []
design_note: 事件丢帧 / 守卫过严导致的运行态校正缺口修复，无独立 D；依据既有 session.get_metadata 运行态校正与 chat.processing_status 终态契约。
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/tests/teamSidebarSpinIdleSync.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
---

# 专家团结束后侧栏空转——metadata idle 强制灭本地处理中灯

## 现象与影响

专家团（team / 集群）模式一轮结束后，聊天正文与右侧任务进度已到 100%，但左侧会话栏仍转圈，输入框停在「集群模式处理中」，停止按钮仍在。用户以为还在跑，实际服务端已 idle。单 Agent 模式通常靠 `chat.final` 兜底灭灯，team 故意不在 `chat.final` 灭灯（一轮多段 final），因此更依赖 `chat.processing_status(false)`；该帧一旦丢失或在 `isLoadingHistory` 期间被整段忽略，灯就会永久亮着。

## 关联设计与当前契约

无独立 D。依据的当前契约：

- `session.get_metadata` 的 `is_processing` 由 Gateway 入队前读取当前运行态覆盖，刷新 / 进会话时用它校正停止按钮；同一 WebSocket 上的 `processing_status` 继续按发送顺序推进。
- 既有降级守卫（`allowDowngrade` / `turnRevision` / `maySnapshotRelightProcessing` / `shouldKeepLocalCronProcessing`）约定：其余场景下快照只能把运行态拉起来，不能把正在跑的轮次压回空闲（#6196 / #6154 / #6155）。
- team 模式 `chat.final` **不得** `setProcessing(false)`，灭灯只认 `chat.processing_status`。
- 企业版 cron 执行态走 `/sessions/{id}/live`，metadata 常误报 false；乐观宽限期内必须挡住误报。

## 根因与修复决策

**根因链：**

1. 终态 `chat.processing_status(false, is_complete=true)` 在 `isLoadingHistory` / `switchingMode` 时被整段 `return` 吞掉；或帧本身未到达浏览器。
2. team 又不用 `chat.final` 兜底灭灯 → 本地 `foregroundIsProcessing` 永久为 true。
3. 既有 metadata 校正因 `mayDowngrade` 仅允许「重连后首个快照」busy→idle，进会话 / 普通刷新拿不到 `allowDowngrade`，服务端已 idle 也扳不动灯。

**修复决策：** 在 `fetchSessionMetadata` 既有守卫块旁增加 **team 专用旁路**：`isTeamAgentMode` + 服务端 `is_processing===false` + 本地 `locallyBusy` + 非 execution recovery 时强制灭灯，并 `patchSession(is_processing: false)`。

**与旧守卫的取舍（检视反馈后加固）：**

| 条件 | 是否纳入旁路 | 理由 |
|------|-------------|------|
| `allowDowngrade` | **不要求** | 主修复场景往往不是重连首快照；要求它会把旁路废掉 |
| `turnRevision === snapshotTurnRevision` | **要求** | 挡住「请求在途时新开一轮」的滞后 idle 快照 |
| `!shouldKeepLocalCronProcessing(...)` | **要求** | 与旧守卫同参，避免击穿 cron 乐观宽限期 |
| `!recoveringExecution` | **要求** | 回放仍交给 cursor，避免抢跑 |

**备选与拒绝：**

- 「只改 `useWebSocket` 让终态帧在 loading 时整段穿透」被拒绝：整段穿透会连带 `drainTaskQueueIfIdle` / 计划续发等副作用，不安全；若做也只应「灭灯三件套后 return」，且仍需 metadata 旁路兜底丢帧。
- 「team 的 `chat.final` 也灭灯」被拒绝：破坏一轮多段 final 语义。
- 「旁路完整复用 `mayDowngrade`」被拒绝：`mayDowngrade` 含 `allowDowngrade`，与主修复目标冲突。

## 回归证据与适用范围

- 新增 `tests/teamSidebarSpinIdleSync.test.mjs`（`npm run test:team-sidebar-spin-idle-sync`）：
  - team + server idle + local busy → 灭灯并 patch workspace
  - agent 模式不走旁路
  - execution recovery 中不强制灭灯
  - 请求在途 turnRevision 前进 → 不强制灭灯
  - cron 乐观宽限期内（team + cron_id）→ 不强制灭灯
- 本次未改 `useWebSocket.ts` 的 loading-history 终态穿透；若丢帧仍复现且 metadata 未再拉，可作后续加固。

## 防错模式与资料同步

1. **旁路绕过降级守卫时，至少保留 turnRevision 与 cron 宽限**：不能只抄「强制灭灯」而丢掉时序与乐观态防护。
2. **team 灭灯只认 processing_status / 权威 idle 校正**，不要在 `chat.final` 上开例外。

## 遗留与后续

- Gateway 是否偶发未发终态帧属后端排查范围；前端旁路在「再拉一次 metadata」时校正。
- 可选：历史加载收尾处再踢一脚（mode=team 且本地 busy 且缓存 metadata idle）。
