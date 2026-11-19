export const TEAM_PANEL_WIDTH_REFERENCE_NAME = 'research:agent';
// Calibrated to keep the representative member name visible in the overview card.
export const TEAM_PANEL_MIN_WIDTH_PX = 520;

const WORKSPACE_DIVIDER_WIDTH_PX = 4;
const CHAT_PANEL_MIN_WIDTH_PCT = 20;
const CHAT_PANEL_MAX_WIDTH_PCT = 70;

export function getMaxTeamChatWidthPct(workspaceWidth: number): number {
  if (!Number.isFinite(workspaceWidth) || workspaceWidth <= 0) return CHAT_PANEL_MAX_WIDTH_PCT;

  const availableChatWidthPct = (
    (workspaceWidth - TEAM_PANEL_MIN_WIDTH_PX - WORKSPACE_DIVIDER_WIDTH_PX) / workspaceWidth
  ) * 100;

  return Math.max(
    CHAT_PANEL_MIN_WIDTH_PCT,
    Math.min(CHAT_PANEL_MAX_WIDTH_PCT, availableChatWidthPct),
  );
}
