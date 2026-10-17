# 轨迹源码来源

Assistant 列表优先展示正文。只有思考内容时，保留 ASSISTANT 标签、徽标、记录身份和详情内容，仅使用弱化的斜体文本区分思考与正文。

[English](PROVENANCE.md) | 中文

JiuwenSwarm 依据 MIT 许可证复制部分 DSH Trajectory 源码，并且仅调整本 feature 目录中的副本。原始 `packages/client` 文件保持不变，并继续负责 DSH Web 功能。

| JiuwenSwarm 副本 | DSH 源文件 |
|---|---|
| `../client/TrajectoryTable.tsx` 和 `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryTable.*` |
| `../client/TrajectoryTimeline.tsx` 和 `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryTimeline.*` |
| `../client/TrajectoryToolbar.tsx` 和 `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryToolbar.*` |
| `../client/TrajectoryExplorer.module.css` | `packages/client/ui-trajectory/src/client/views.module.css` |
| `timeline.ts` | `packages/client/ui-trajectory/src/client/timeline.ts` |
| `search-index.ts` | `packages/client/ui-trajectory/src/client/trajectory-search-index.ts` |
| `virtual-rows.ts` | `packages/client/ui-trajectory/src/client/trajectory-virtual-rows.ts` |
| `preview.ts` | `packages/client/ui-trajectory/src/client/trajectory-preview.ts` |
| `record.ts` | `packages/client/ui-trajectory/src/client/trajectory-record.ts` |

`timeline.ts` 增加 `withSharedWallClockDomain`：保留色块的绝对时间，只替换时间域两端，让诊断泳道共用同一条墙钟标尺。可选的 `mapTime` 会在诊断时间轴去掉空闲间隔时，把色块坐标映射到这条共享时间域上。

`../client/TrajectoryToolbar.tsx` 增加 `showScaleControls`，诊断视图可以隐藏时长和 Tokens 开关，同时保留轮次与调用的折叠。

`../client/TrajectoryTimeline.tsx` 接受 `sharedDomain`、`mapTime`、`lockViewport`，以及可选的共享视口。共享时间域即诊断标尺；`mapTime` 在去掉空闲间隔后把色块放到这条标尺上。锁定后，左键拖拽仍用来选取区间。共享视口受控时，滚轮会缩放并平移这一段窗口，所有泳道一起移动。

`../client/TrajectoryExplorer.tsx` 是 JiuwenSwarm 对这些副本的自有组装。为支持轨迹诊断，它新增可选 props：`highlightStepKeys`、`onTimelineSelectionChange`、`clearSelectionToken`、`timelineRange`、`onTimelineRangeChange`、`sharedTimeDomain`、`mapSharedTime`、`lockTimelineViewport`、`sharedViewport` 与 `onSharedViewportChange`；它们强调时间轴片段，并让各泳道共用一段墙钟选区，不改变源自 DSH 的列表。

`../theme/` 以 `packages/client/ui-theme/src/styles` 的五个 token 样式表为来源，并将作用域限制到轨迹宿主；[`../theme/PROVENANCE.zh.md`](../theme/PROVENANCE.zh.md) 记录源版本和调整。[`../primitives/PROVENANCE.zh.md`](../primitives/PROVENANCE.zh.md) 记录本地复制的 React 基础组件。JiuwenSwarm 在构建或运行时不会解析上游 workspace 包。
