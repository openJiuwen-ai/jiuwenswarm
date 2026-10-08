/** Shared viewport fitting for the Designer canvas. */

// The chat panel floats over the canvas (.designer-chat-panel: left 16px,
// width 360px), so a symmetric fit tucks the leftmost column - usually the
// reference inputs - permanently underneath it.
export const DESIGNER_FIT_VIEW_PADDING = {
  top: '8%',
  right: '8%',
  bottom: '8%',
  left: '400px',
} as const;
