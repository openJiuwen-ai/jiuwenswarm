export type DesignerVerticalSpan = { top: number; bottom: number };

/**
 * How far the canvas must move up so a node toolbar below its node is fully
 * visible. Never moves the toolbar's top edge out of view.
 */
export function designerToolbarRevealShift(
  toolbar: DesignerVerticalSpan,
  visible: DesignerVerticalSpan,
  margin = 16,
): number {
  const overflow = toolbar.bottom + margin - visible.bottom;
  if (overflow <= 0) return 0;
  const headroom = toolbar.top - (visible.top + margin);
  return Math.max(0, Math.min(overflow, headroom));
}
