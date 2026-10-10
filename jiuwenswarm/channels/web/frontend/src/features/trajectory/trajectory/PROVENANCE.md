# Trajectory source provenance

English | [中文](PROVENANCE.zh.md)

JiuwenSwarm copies selected DSH Trajectory sources under the MIT license and adapts only the copies in this feature directory. The original `packages/client` files remain unchanged and continue to own the DSH Web feature.

| JiuwenSwarm copy | DSH source |
|---|---|
| `../client/TrajectoryTable.tsx` and `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryTable.*` |
| `../client/TrajectoryTimeline.tsx` and `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryTimeline.*` |
| `../client/TrajectoryToolbar.tsx` and `.module.css` | `packages/client/ui-trajectory/src/client/TrajectoryToolbar.*` |
| `../client/TrajectoryExplorer.module.css` | `packages/client/ui-trajectory/src/client/views.module.css` |
| `timeline.ts` | `packages/client/ui-trajectory/src/client/timeline.ts` |
| `search-index.ts` | `packages/client/ui-trajectory/src/client/trajectory-search-index.ts` |
| `virtual-rows.ts` | `packages/client/ui-trajectory/src/client/trajectory-virtual-rows.ts` |
| `preview.ts` | `packages/client/ui-trajectory/src/client/trajectory-preview.ts` |
| `record.ts` | `packages/client/ui-trajectory/src/client/trajectory-record.ts` |

`../theme/` starts from the five token sheets in `packages/client/ui-theme/src/styles` and scopes them to the trajectory host; [`../theme/PROVENANCE.md`](../theme/PROVENANCE.md) records the source revision and adaptation. [`../primitives/PROVENANCE.md`](../primitives/PROVENANCE.md) records the locally copied React primitives. JiuwenSwarm does not resolve an upstream workspace package at build or runtime.
