export type TeamPanelActiveTab = 'planning' | 'team' | 'artifacts' | 'review';
export type TeamPanelDetailTab = 'members' | 'group';

export interface TeamPanelState {
  expanded: boolean;
  activeTab: TeamPanelActiveTab;
  activeDetailTab: TeamPanelDetailTab;
  selectedMemberId?: string;
  selectedArtifactId?: string;
}

export const DEFAULT_TEAM_PANEL_STATE: TeamPanelState = {
  expanded: false,
  activeTab: 'team',
  activeDetailTab: 'members',
};

const VALID_ACTIVE_TABS = new Set<TeamPanelActiveTab>([
  'planning',
  'team',
  'artifacts',
  'review',
]);
const VALID_DETAIL_TABS = new Set<TeamPanelDetailTab>(['members', 'group']);

/** Normalize a raw parsed value into a safe TeamPanelState. */
export function normalizeTeamPanelState(value: unknown): TeamPanelState {
  if (!value || typeof value !== 'object') {
    return { ...DEFAULT_TEAM_PANEL_STATE };
  }

  const raw = value as Record<string, unknown>;
  const activeTab =
    typeof raw.activeTab === 'string' && VALID_ACTIVE_TABS.has(raw.activeTab as TeamPanelActiveTab)
      ? (raw.activeTab as TeamPanelActiveTab)
      : DEFAULT_TEAM_PANEL_STATE.activeTab;
  const activeDetailTab =
    typeof raw.activeDetailTab === 'string' &&
    VALID_DETAIL_TABS.has(raw.activeDetailTab as TeamPanelDetailTab)
      ? (raw.activeDetailTab as TeamPanelDetailTab)
      : DEFAULT_TEAM_PANEL_STATE.activeDetailTab;
  const selectedMemberId =
    typeof raw.selectedMemberId === 'string' && raw.selectedMemberId.trim()
      ? raw.selectedMemberId
      : undefined;
  const selectedArtifactId =
    typeof raw.selectedArtifactId === 'string' && raw.selectedArtifactId.trim()
      ? raw.selectedArtifactId
      : undefined;

  return {
    expanded: typeof raw.expanded === 'boolean' ? raw.expanded : DEFAULT_TEAM_PANEL_STATE.expanded,
    activeTab,
    activeDetailTab,
    ...(selectedMemberId ? { selectedMemberId } : {}),
    ...(selectedArtifactId ? { selectedArtifactId } : {}),
  };
}

/** Parse a localStorage raw string into TeamPanelState. */
export function parseTeamPanelStateRaw(raw: string | null): TeamPanelState {
  if (!raw) {
    return { ...DEFAULT_TEAM_PANEL_STATE };
  }
  try {
    return normalizeTeamPanelState(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_TEAM_PANEL_STATE };
  }
}
