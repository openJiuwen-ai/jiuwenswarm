export interface MessageRenderOptions {
  compact: boolean;
  collapsed: boolean;
  thinkingExpanded: boolean;
  activeThinkingId?: string;
  toolDetailsExpanded: boolean;
  animationPhase: number;
}

export interface RenderedHistoryEntry {
  lines: string[];
  gapAfter: boolean;
}
