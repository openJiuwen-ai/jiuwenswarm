export const breakpoints = {
  xs: 640,
  sm: 823,
  md: 900,
  lg: 1028,
  xl: 1264,
  toolPanelAutoHide: 1280,
  wide: 1440,
  wideSidebar: 1441,
  graph: 1535,
  ultraWide: 1600,
  max: 1920,
} as const;

export type BreakpointKey = keyof typeof breakpoints;

export const SIDEBAR_WIDTH = 240;
export const SIDEBAR_WIDTH_WIDE = 296;
export const CHAT_MIN_WIDTH = 512;
export const TOOL_PANEL_MIN_WIDTH = 512;
export const DIVIDER_WIDTH = 4;
export const ICON_RAIL_WIDTH = 72;

export const panelThresholds = {
  fitBoth: (sidebarWidth: number) => ICON_RAIL_WIDTH + sidebarWidth + CHAT_MIN_WIDTH + DIVIDER_WIDTH + TOOL_PANEL_MIN_WIDTH,
  fitToolPanelOnly: ICON_RAIL_WIDTH + CHAT_MIN_WIDTH + DIVIDER_WIDTH + TOOL_PANEL_MIN_WIDTH,
} as const;

export function getSidebarWidth(): number {
  return window.innerWidth >= breakpoints.wideSidebar ? SIDEBAR_WIDTH_WIDE : SIDEBAR_WIDTH;
}

export function canFitBoth(): boolean {
  return window.innerWidth >= panelThresholds.fitBoth(getSidebarWidth());
}

export function canFitToolPanelOnly(): boolean {
  return window.innerWidth >= panelThresholds.fitToolPanelOnly;
}

export function canFitToolPanelItself(): boolean {
  return window.innerWidth >= TOOL_PANEL_MIN_WIDTH;
}
