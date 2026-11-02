export const MERMAID_CANVAS_MAX_HEIGHT = 600;
export const MERMAID_CANVAS_MIN_HEIGHT = 280;
export const MERMAID_CANVAS_TOP_OFFSET = 24;
export const MERMAID_CANVAS_BOTTOM_OFFSET = 24;

interface MermaidCanvasLayoutInput {
  naturalWidth: number;
  naturalHeight: number;
  containerWidth: number;
}

export interface MermaidCanvasLayout {
  fitScale: number;
  canvasHeight: number;
  alignTop: boolean;
}

export function clampMermaidScale(scale: number): number {
  return Math.min(Math.max(scale, 0.25), 3);
}

export function calculateMermaidCanvasLayout({ naturalWidth, naturalHeight, containerWidth }: MermaidCanvasLayoutInput): MermaidCanvasLayout | null {
  if (naturalHeight <= 0) {
    return null;
  }

  const availableHeight = MERMAID_CANVAS_MAX_HEIGHT - MERMAID_CANVAS_TOP_OFFSET - MERMAID_CANVAS_BOTTOM_OFFSET;
  const scaleToFitWidth = containerWidth > 0 && naturalWidth > 0 ? containerWidth / naturalWidth : 1;
  const scaleToFitHeight = availableHeight / naturalHeight;
  const fitScale = clampMermaidScale(Math.min(1, scaleToFitWidth, scaleToFitHeight));
  const contentHeight = naturalHeight * fitScale + MERMAID_CANVAS_TOP_OFFSET + MERMAID_CANVAS_BOTTOM_OFFSET;

  return {
    fitScale,
    canvasHeight: Math.min(MERMAID_CANVAS_MAX_HEIGHT, Math.max(MERMAID_CANVAS_MIN_HEIGHT, contentHeight)),
    alignTop: contentHeight > MERMAID_CANVAS_MAX_HEIGHT,
  };
}
