# Trajectory source provenance

Assistant ledger previews prefer visible output. Reasoning-only previews keep the ASSISTANT label, badge, record identity, and inspector content, while using muted italic text to distinguish reasoning from visible output.

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

`timeline.ts` adds `withSharedWallClockDomain`, which keeps span coordinates and replaces only the domain ends so diagnosis lanes share one wall-clock ruler. An optional `mapTime` moves those coordinates onto the shared domain when the diagnosis axis collapses idle gaps.

`../client/TrajectoryToolbar.tsx` adds `showScaleControls` so the diagnosis view can hide the duration and token switches while keeping turn and call folding.

`../client/TrajectoryTimeline.tsx` accepts `sharedDomain`, `mapTime`, `lockViewport`, and an optional shared viewport. The shared domain is the diagnosis ruler; `mapTime` places spans on it after idle gaps are removed. Locking keeps left-drag as a selection. When a shared viewport is controlled, the wheel zooms and pans that one window for every lane.

`../client/TrajectoryExplorer.tsx` is JiuwenSwarm's own assembly of these copies. For trajectory diagnosis it adds optional props, `highlightStepKeys`, `onTimelineSelectionChange`, `clearSelectionToken`, `timelineRange`, `onTimelineRangeChange`, `sharedTimeDomain`, `mapSharedTime`, `lockTimelineViewport`, `sharedViewport` and `onSharedViewportChange`; they emphasise timeline spans, share one wall-clock brush across lanes, and leave the DSH-derived ledger untouched.

`../theme/` starts from the five token sheets in `packages/client/ui-theme/src/styles` and scopes them to the trajectory host; [`../theme/PROVENANCE.md`](../theme/PROVENANCE.md) records the source revision and adaptation. [`../primitives/PROVENANCE.md`](../primitives/PROVENANCE.md) records the locally copied React primitives. JiuwenSwarm does not resolve an upstream workspace package at build or runtime.
