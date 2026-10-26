import { useCallback, useEffect, useState } from 'react';
import type { TabType, TeamDetailTab } from '../components/teamArea/shared';
import {
  DEFAULT_TEAM_PANEL_STATE,
  parseTeamPanelStateRaw,
  type TeamPanelState,
} from './teamPanelStateNormalize';

export type { TeamPanelState } from './teamPanelStateNormalize';
export { DEFAULT_TEAM_PANEL_STATE, normalizeTeamPanelState, parseTeamPanelStateRaw } from './teamPanelStateNormalize';

const TEAM_PANEL_STATE_KEY = 'jiuwenclaw_team_panel_state';
const TEAM_PANEL_STATE_EVENT = 'jiuwenclaw-team-panel-state-change';

interface UseTeamPanelStateResult {
  teamAreaExpanded: boolean;
  teamAreaActiveTab: TabType;
  teamAreaActiveDetailTab: TeamDetailTab;
  teamAreaSelectedMemberId?: string;
  teamAreaSelectedArtifactId?: string;
  setTeamAreaExpanded: (expanded: boolean) => void;
  setTeamAreaActiveTab: (tab: TabType) => void;
  setTeamAreaActiveDetailTab: (tab: TeamDetailTab) => void;
  setTeamAreaSelectedMemberId: (memberId: string) => void;
  setTeamAreaSelectedArtifactId: (artifactId: string) => void;
}

function loadTeamPanelState(): TeamPanelState {
  try {
    return parseTeamPanelStateRaw(window.localStorage.getItem(TEAM_PANEL_STATE_KEY));
  } catch {
    // localStorage may be unavailable (private mode / blocked storage).
    return { ...DEFAULT_TEAM_PANEL_STATE };
  }
}

function saveTeamPanelState(nextState: TeamPanelState): void {
  try {
    window.localStorage.setItem(TEAM_PANEL_STATE_KEY, JSON.stringify(nextState));
  } catch {
    // Ignore quota / unavailable storage errors; in-memory state still works.
  }
}

function notifyTeamPanelState(nextState: TeamPanelState): void {
  window.dispatchEvent(new CustomEvent<TeamPanelState>(TEAM_PANEL_STATE_EVENT, {
    detail: nextState,
  }));
}

export function openTeamPanel(
  activeTab: TabType,
  activeDetailTab: TeamDetailTab = 'members',
  selectedMemberId?: string
): void {
  const nextState: TeamPanelState = {
    expanded: true,
    activeTab,
    activeDetailTab,
    selectedMemberId,
  };
  saveTeamPanelState(nextState);
  notifyTeamPanelState(nextState);
}

export function openArtifactPanel(selectedArtifactId: string): void {
  const nextState: TeamPanelState = {
    ...loadTeamPanelState(),
    expanded: true,
    activeTab: 'artifacts',
    selectedArtifactId,
  };
  saveTeamPanelState(nextState);
  notifyTeamPanelState(nextState);
}

export function useTeamPanelState(): UseTeamPanelStateResult {
  const [state, setState] = useState<TeamPanelState>(loadTeamPanelState);

  const updateState = useCallback((patch: Partial<TeamPanelState>) => {
    setState((current) => {
      const nextState = { ...current, ...patch };
      saveTeamPanelState(nextState);
      notifyTeamPanelState(nextState);
      return nextState;
    });
  }, []);

  const setTeamAreaExpanded = useCallback((expanded: boolean) => {
    updateState({ expanded });
  }, [updateState]);

  const setTeamAreaActiveTab = useCallback((activeTab: TabType) => {
    updateState({ activeTab });
  }, [updateState]);

  const setTeamAreaActiveDetailTab = useCallback((activeDetailTab: TeamDetailTab) => {
    updateState({ activeDetailTab });
  }, [updateState]);

  const setTeamAreaSelectedMemberId = useCallback((selectedMemberId: string) => {
    updateState({ selectedMemberId });
  }, [updateState]);

  const setTeamAreaSelectedArtifactId = useCallback((selectedArtifactId: string) => {
    updateState({ selectedArtifactId });
  }, [updateState]);

  useEffect(() => {
    function handleTeamPanelStateChange(event: Event) {
      setState((event as CustomEvent<TeamPanelState>).detail);
    }

    window.addEventListener(TEAM_PANEL_STATE_EVENT, handleTeamPanelStateChange);
    return () => {
      window.removeEventListener(TEAM_PANEL_STATE_EVENT, handleTeamPanelStateChange);
    };
  }, []);

  return {
    teamAreaExpanded: state.expanded,
    teamAreaActiveTab: state.activeTab,
    teamAreaActiveDetailTab: state.activeDetailTab,
    teamAreaSelectedMemberId: state.selectedMemberId,
    teamAreaSelectedArtifactId: state.selectedArtifactId,
    setTeamAreaExpanded,
    setTeamAreaActiveTab,
    setTeamAreaActiveDetailTab,
    setTeamAreaSelectedMemberId,
    setTeamAreaSelectedArtifactId,
  };
}
