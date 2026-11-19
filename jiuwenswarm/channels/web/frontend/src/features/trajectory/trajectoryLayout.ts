/** Deterministic sizing rules for the raw OTel inspector. */

export const RAW_INSPECTOR_DEFAULT_HEIGHT = 220;
export const RAW_INSPECTOR_MIN_HEIGHT = 120;
export const RAW_INSPECTOR_MAX_RATIO = 0.6;
export const RAW_INSPECTOR_KEYBOARD_STEP = 16;

export interface RawInspectorHeightBounds {
  min: number;
  max: number;
}

export function shouldInsetTrajectoryForFloatingTasks(
  mode: string,
  activeView: string,
  taskPanelAvailable: boolean,
  taskPanelHidden: boolean,
  taskPanelExpanded: boolean,
): boolean {
  return (mode === 'agent' || mode === 'team')
    && (activeView === 'trajectory' || activeView === 'diagnosis')
    && taskPanelAvailable
    && !taskPanelHidden
    && !taskPanelExpanded;
}

/**
 * Bottom clearance the trajectory view gives up to the docked chat composer.
 *
 * The trajectory stops at the composer's top edge rather than running under
 * it, so its own footer and raw-record panel stay inside the visible area. A
 * composer that is undocked or collapsed to watch-only takes nothing.
 *
 * @param docked - Whether the composer is kept available on the trajectory view.
 * @param collapsed - Whether the docked composer is collapsed to watch-only.
 * @param measuredHeight - Composer height in pixels, as laid out.
 * @returns Non-negative whole-pixel clearance.
 */
export type TrajectoryComposerPolicy = 'available' | 'locked' | 'hidden';

/**
 * Whether the docked composer may stay open on the trajectory analysis page.
 *
 * Choosing a range ("诊断轨迹") closes it and keeps the toggle inert. The
 * diagnosis sub-page does not show it at all. Browsing the trajectory leaves
 * the toggle usable.
 */
export function trajectoryComposerPolicy(
  subpage: 'trajectory' | 'diagnosis',
  selecting: boolean,
): TrajectoryComposerPolicy {
  if (subpage === 'diagnosis') return 'hidden';
  if (selecting) return 'locked';
  return 'available';
}

export function trajectoryComposerClearance(
  docked: boolean,
  collapsed: boolean,
  measuredHeight: number,
): number {
  if (!docked || collapsed) return 0;
  if (!Number.isFinite(measuredHeight)) return 0;
  return Math.max(0, Math.ceil(measuredHeight));
}

export function rawInspectorHeightBounds(containerHeight: number): RawInspectorHeightBounds {
  return {
    min: RAW_INSPECTOR_MIN_HEIGHT,
    max: Math.max(
      RAW_INSPECTOR_MIN_HEIGHT,
      Math.floor(Math.max(0, containerHeight) * RAW_INSPECTOR_MAX_RATIO),
    ),
  };
}

export function clampRawInspectorHeight(height: number, containerHeight: number): number {
  const bounds = rawInspectorHeightBounds(containerHeight);
  return Math.min(bounds.max, Math.max(bounds.min, height));
}

export function rawInspectorKeyboardHeight(
  currentHeight: number,
  key: string,
  containerHeight: number,
): number | null {
  const bounds = rawInspectorHeightBounds(containerHeight);
  switch (key) {
    case 'ArrowUp':
      return clampRawInspectorHeight(
        currentHeight + RAW_INSPECTOR_KEYBOARD_STEP,
        containerHeight,
      );
    case 'ArrowDown':
      return clampRawInspectorHeight(
        currentHeight - RAW_INSPECTOR_KEYBOARD_STEP,
        containerHeight,
      );
    case 'Home':
      return bounds.min;
    case 'End':
      return bounds.max;
    default:
      return null;
  }
}
