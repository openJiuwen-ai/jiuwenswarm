# 轨迹源码来源

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

`../theme/` 以 `packages/client/ui-theme/src/styles` 的五个 token 样式表为来源，并将作用域限制到轨迹宿主；[`../theme/PROVENANCE.zh.md`](../theme/PROVENANCE.zh.md) 记录源版本和调整。[`../primitives/PROVENANCE.zh.md`](../primitives/PROVENANCE.zh.md) 记录本地复制的 React 基础组件。JiuwenSwarm 在构建或运行时不会解析上游 workspace 包。
