---
id: F_20261105_1625_team-shared-axis-user-turn-gaps
title: 集群共享轴隔夜交界对齐单 Agent user_turn 空隙
type: F
author: william9200000
author_email: wangchen309@h-partners.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/trajectory
files:
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/diagnosis/diagnosisTimeSelection.ts
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/diagnosis/useSharedTimeAxis.ts
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/TrajectoryPanel.tsx
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/client/TrajectoryExplorer.tsx
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/client/TrajectoryTimeline.tsx
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/AGENTS.md
  - jiuwenswarm/channels/web/frontend/tests/trajectoryDiagnosis.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/singleAgentSurface.test.mjs
designs: []
design_note: 无独立 D；按分析页对照单 Agent 隔夜双时间交界的结论直接落地。
---

# 集群共享轴隔夜交界对齐单 Agent user_turn 空隙

## 关联依据

- 设计：无独立 D。
- spec：未改正式 S；行为写入轨迹 feature `AGENTS.md` 共享时间轴小节。
- 日期：2026-11-05。
- 实际实施范围：共享轴压缩忽略 team harness 噪声；诊断泳道空隙统一跟主 Agent / Team Leader 的 `user_turn` 交界。

## 决策归属与关系

新决定。此前分析页已按 `user_turn` 画约 12px 交界；单 Agent 隔夜可压干净并成对标时间，集群因 harness 心跳与成员本泳道自算空隙达不到同效果。本次只补这两处对齐，不恢复已回退的 2% 比例缝与带日期强制双标。

## 实际实现与开发决策

1. 新增 `isTeamHarnessNoiseCell`：识别 `team-clock` / `team-event` / `team-inbound` 与 `harness_internal`。`collectUserInteractionWallRanges` 量墙钟范围时跳过噪声；`useSharedTimeAxis` 压缩采样时同样丢弃，避免心跳撑住隔夜空闲。
2. `TrajectoryPanel` 把领导侧 `sharedAxisTurnMarkers` 映射为 `sharedUserTurnGapBoundaries`，经 `TrajectoryExplorer` → `TrajectoryTimeline` 下发。有该 prop 时各泳道不再用本泳道 turns 自算空隙，与共享轴交界一致。
3. why-not：不按单泳道压缩空闲（会破坏跨泳道对齐）；不把成员真实忙碌段压掉（他员忙碌仍应保留在轴上）。

## 与设计的偏差

无独立设计；无偏差。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 / 记录链接 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-05 / harness 过滤与空隙 prop | 工作区未提交候选；Node 前端测试 | `npm run test:trajectory-diagnosis`（106 pass）；`npm run test:trajectory-host`（19 pass） | 通过 | 未做真实集群隔夜会话人工点验 |
| 2026-11-06 / 零时长 Input 保留 | 工作区未提交候选；Node 前端测试 | `npm run test:trajectory-diagnosis` | 110 pass | 未在页面上点验贪吃蛇会话 |

### 可见行为、副作用与证据有效范围

- 用户可见：集群分析页隔夜全员空闲（仅 harness 心跳）时，交界应收成与单 Agent 类似的紧凑 `user_turn` 双标缝；成员泳道在领导轮界处同步让位。
- 证据依赖当前前端打包测试；换会话数据或 harness 文案形态变化需重验。

## 验证结论

- 已证明范围：harness 噪声识别与压缩过滤、共享 `userTurnGapBoundaries` 宿主契约测试通过。
- 未覆盖：真实 Team 隔夜会话 UI 点验。
- 可支持的交付判断：测试通过，可合入；隔夜观感以人工点验为补充。

### 覆盖与资料同步

已更新 `trajectory/AGENTS.md` 共享时间轴说明。无独立 S 变更。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 / 条件及所需验证 |
| --- | --- | --- | --- |
| 成员真实跨夜忙碌仍保留在轴上 | 有意取舍 | 领导泳道仍可能出现「他员忙碌」空白；交界仍强制双标与 2% 缝 | 若要折叠需另议单泳道微间隙方案 |
| 真实集群隔夜 UI 点验 | 未验证 | 以本地测试为准 | 打开隔夜 Team 会话核对双日期交界 |

## 实施演进与修订

### 2026-11-05 交界强制双时间与 2% 留缝

现象：仅做 harness 过滤与领导空隙对齐后，集群隔夜交界仍常只见一条线、一个时间。根因是压缩后相邻 `user_turn` 叠在同一轴点，±6px 与 `atBoundEdge` 藏标不足以呈现单 Agent 那种成对交界；跨天又不带日期。

做法：恢复 `placeUserTurnAxisEdges`（不足可视宽 2% 则拉开）、`formatUserTurnClock`（带日期），每轮起止都打标；泳道按 `displayAxisMs` 让位。保留 harness 过滤与领导 `userTurnGapRanges`。

验证：`npm run test:trajectory-diagnosis` 109 pass；`npm run test:trajectory-host` 19 pass。

### 2026-11-05 跨缝色块按交界空白裁剪

现象：强制 2% 交界白缝内，Main Agent 的 Model/Tools 仍连续着色（多会话复现，含亲子/攻略类调研团队），成员泳道为空。

根因：原先只对端点贴齐 `user_turn` axisMs 的 span 做 pad；Leader 长工具/模型跨缝时两端都不贴边，色块直接画进显示空白。

做法：`userTurnDisplayGaps` + `clipIntervalAgainstGaps`；`TrajectoryTimeline` 用区间差替代端点 pad，跨缝拆两侧。

验证：`npm run test:trajectory-diagnosis` 110 pass；`npm run test:trajectory-host` 19 pass。

### 2026-11-06 零时长 Input 不再被裁剪丢掉

现象：分析页「时间」泳道的 Input 行整行空白，Model / Tools 仍在。账本里的 SYSTEM / USER / CONTEXT 还在。

根因：这些准备行只记 `startedAt`，`timeSeconds` 为 `null`，投影是一个点。`clipIntervalAgainstGaps` 要求结束时间大于开始时间，点被整段丢掉。裁剪前 CSS `min-width: 2px` 会把点画成细条。

做法：点保留；只有严格落在交界空白内部的点才去掉。有时长且整段落在空白里的色块仍去掉，跨缝的仍拆成两侧。

验证：`npm run test:trajectory-diagnosis` 110 pass。未在页面上点验贪吃蛇会话。
