---
id: B_20261030_1416_timeline-context-over-message
title: 时间轴 CONTEXT 色块叠在 ASSISTANT 上方
type: B
author: william9200000
author_email: wangchen309@h-partners.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/trajectory
files:
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/trajectory/timeline.ts
  - jiuwenswarm/channels/web/frontend/tests/trajectoryTimeline.test.mjs
designs: []
design_note: 无独立设计；对齐既有 ledger phase 顺序（user/context 在 message 前）的时间轴观感修复。
---

# 时间轴 CONTEXT 色块叠在 ASSISTANT 上方

## 现象与影响

轨迹分析页墙钟时间轴上，绿色 CONTEXT 色块落在紫色 ASSISTANT 条中段上方；预期应在橙色 USER 与紫色 ASSISTANT 之间的空隙上方（与表格/ledger 的 prep → reply 顺序一致）。影响读模型时间轴与诊断泳道共享轴上的请求准备关系判读。

## 关联设计与当前契约

无独立 D。依据现有投影约定：`context.window.commit` 的 `recordedAt` 可作为 CONTEXT 的 `startedAt`，且可晚于同请求 ASSISTANT 的 inference start（见 `diagnosisTimeSelection.lastTimedNodeInterval` 注释）；ledger 排序用 phase 把 system/user/context 放在 message 前。

## 根因与修复决策

- **因果**：timed 投影直接用 `cell.startedAt` 画横坐标；晚到的 window commit 把 CONTEXT 画进 ASSISTANT 区间。
- **修复**：在 `deriveTimedTimeline` 做显示层校正——同一 `requestRecordId` / `physicalInferenceId` 下，晚于 reply start 的 system/user/context 滑回「已按时 prep 之后、message 之前」的开区间；无更早 prep 时贴在 reply start。不改 cell 上记录的墙钟，tooltip/表格仍显示真实 commit 时间。
- **未选方案**：改 projector 重写 `startedAt`（会污染事实时间与诊断选区）；只改 CSS/lane（问题在时间而非泳道）。

## 回归证据与适用范围

- 候选：工作区 `timeline.ts` + `trajectoryTimeline.test.mjs`
- 环境：`jiuwenswarm/channels/web/frontend`，`npm run test:trajectory-timeline`
- 结果：20 passed（含晚到 context 滑入 user/message 之间、无 user 时贴 reply start、无共享 request 时保持原时间）
- 未验证：真实长会话诊断页目视；共享轴把 user→message 空隙压成 idle 时，校正时刻可能贴到 reply 左缘（仍优于叠在紫条中段）

## 防错模式与资料同步

时间轴显示序应对齐 ledger phase；commit 墙钟可晚于 inference，显示层可校正，勿把显示坐标写回记录。已写入本 B；轨迹 AGENTS 补充一句不变量。

## 遗留与后续

无。若后续要让共享压缩轴也保留 prep 空隙，需在 `compressSharedTimeline` 采样策略上另开任务。
