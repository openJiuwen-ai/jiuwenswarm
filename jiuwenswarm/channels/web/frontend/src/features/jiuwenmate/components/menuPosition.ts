// Placement math for ActionMenu's panel. The panel is fixed-positioned from the
// trigger's viewport rect so that it escapes clipping scroll containers (the
// schedule board table, the colleague list, the side panel body) while staying
// anchored to the trigger. Kept free of React so it can be unit tested directly.

export type MenuAlign = 'start' | 'end';

export type MenuPlacement = 'bottom' | 'top';

export interface MenuAnchorRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface MenuSize {
  width: number;
  height: number;
}

export interface ViewportSize {
  width: number;
  height: number;
}

export interface MenuPosition {
  left: number;
  top: number;
  placement: MenuPlacement;
}

/** Gap between the trigger and the panel. */
export const MENU_GAP = 4;

/** Minimum clearance kept to the viewport edges. */
export const VIEWPORT_MARGIN = 8;

function clamp(value: number, min: number, max: number): number {
  // A panel wider/taller than the viewport must still start at the margin.
  return Math.min(Math.max(value, min), Math.max(min, max));
}

/**
 * Computes the fixed-position coordinates of the menu panel.
 *
 * Opens below the trigger by default, flips above when the bottom space is
 * taken, and clamps into the viewport when neither side fits. `start` aligns the
 * panel's left edge with the trigger's left edge, `end` its right edge with the
 * trigger's right edge.
 */
export function computeMenuPosition(
  anchor: MenuAnchorRect,
  size: MenuSize,
  viewport: ViewportSize,
  align: MenuAlign,
): MenuPosition {
  const below = anchor.bottom + MENU_GAP;
  const above = anchor.top - MENU_GAP - size.height;
  const maxTop = viewport.height - size.height - VIEWPORT_MARGIN;

  let placement: MenuPlacement = 'bottom';
  let top: number;
  if (below <= maxTop) {
    top = below;
  } else if (above >= VIEWPORT_MARGIN) {
    placement = 'top';
    top = above;
  } else {
    top = clamp(below, VIEWPORT_MARGIN, maxTop);
  }

  const desiredLeft = align === 'end' ? anchor.right - size.width : anchor.left;
  const left = clamp(desiredLeft, VIEWPORT_MARGIN, viewport.width - size.width - VIEWPORT_MARGIN);

  return { left, top, placement };
}
