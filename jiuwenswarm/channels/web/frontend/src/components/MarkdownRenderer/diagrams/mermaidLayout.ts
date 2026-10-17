export const MERMAID_CANVAS_MAX_HEIGHT = 600;
export const MERMAID_CANVAS_MIN_HEIGHT = 280;
export const MERMAID_CANVAS_MIN_DISPLAY_SCALE = 0.5;
export const MERMAID_CANVAS_TOP_OFFSET = 24;
export const MERMAID_CANVAS_BOTTOM_OFFSET = 24;

interface MermaidCanvasLayoutInput {
  naturalWidth?: number;
  naturalHeight: number;
  containerWidth?: number;
  minCanvasHeight?: number;
}

export interface MermaidCanvasLayout {
  fitScale: number;
  displayScale?: number;
  canvasHeight: number;
  alignTop?: boolean;
}

export function clampMermaidScale(scale: number): number {
  return Math.min(Math.max(scale, 0.25), 3);
}

export function calculateMermaidCanvasLayout({
  naturalWidth = 0,
  naturalHeight,
  containerWidth = 0,
  minCanvasHeight: requestedMinCanvasHeight,
}: MermaidCanvasLayoutInput): MermaidCanvasLayout | null {
  if (naturalHeight <= 0) {
    return null;
  }

  if (requestedMinCanvasHeight !== undefined) {
    const contentHeight = naturalHeight + MERMAID_CANVAS_TOP_OFFSET + MERMAID_CANVAS_BOTTOM_OFFSET;
    return {
      fitScale: 1,
      canvasHeight: requestedMinCanvasHeight,
      alignTop: contentHeight > requestedMinCanvasHeight,
    };
  }

  const availableHeight = MERMAID_CANVAS_MAX_HEIGHT - MERMAID_CANVAS_TOP_OFFSET - MERMAID_CANVAS_BOTTOM_OFFSET;
  const scaleToFitWidth = containerWidth > 0 && naturalWidth > 0 ? containerWidth / naturalWidth : 1;
  const scaleToFitHeight = availableHeight / naturalHeight;
  const fitScale = Math.min(1, scaleToFitWidth, scaleToFitHeight);
  const displayScale = Math.max(fitScale, MERMAID_CANVAS_MIN_DISPLAY_SCALE);
  const contentHeight = naturalHeight * displayScale + MERMAID_CANVAS_TOP_OFFSET + MERMAID_CANVAS_BOTTOM_OFFSET;

  return {
    fitScale,
    displayScale,
    canvasHeight: Math.min(MERMAID_CANVAS_MAX_HEIGHT, Math.max(MERMAID_CANVAS_MIN_HEIGHT, contentHeight)),
  };
}
