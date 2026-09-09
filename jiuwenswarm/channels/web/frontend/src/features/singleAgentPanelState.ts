import { useCallback, useEffect, useState } from 'react';

export type SingleAgentToolTab = 'planning' | 'subagents' | 'artifacts' | 'review';

export interface SingleAgentPanelState {
  expanded: boolean;
  activeTab: SingleAgentToolTab;
  selectedArtifactId?: string;
  selectedSubagentId?: string | null;
}

interface UseSingleAgentPanelStateResult {
  singleAgentPanelExpanded: boolean;
  singleAgentPanelActiveTab: SingleAgentToolTab;
  singleAgentPanelSelectedArtifactId?: string;
  singleAgentPanelSelectedSubagentId?: string | null;
  setSingleAgentPanelExpanded: (expanded: boolean) => void;
  setSingleAgentPanelActiveTab: (tab: SingleAgentToolTab) => void;
  setSingleAgentPanelSelectedArtifactId: (artifactId: string) => void;
  setSingleAgentPanelSelectedSubagentId: (subagentId: string | null) => void;
}

const SINGLE_AGENT_PANEL_STATE_KEY = 'jiuwenclaw_single_agent_panel_state';
const SINGLE_AGENT_PANEL_STATE_EVENT = 'jiuwenclaw-single-agent-panel-state-change';
const DEFAULT_STATE: SingleAgentPanelState = {
  expanded: false,
  activeTab: 'planning',
};

function normalizeState(value: unknown): SingleAgentPanelState {
  if (!value || typeof value !== 'object') return { ...DEFAULT_STATE };
  const raw = value as Record<string, unknown>;
  const activeTab = raw.activeTab;
  return {
    expanded: typeof raw.expanded === 'boolean' ? raw.expanded : false,
    activeTab:
      activeTab === 'planning' || activeTab === 'subagents' || activeTab === 'artifacts' || activeTab === 'review' ? activeTab : DEFAULT_STATE.activeTab,
    ...(typeof raw.selectedArtifactId === 'string' && raw.selectedArtifactId.trim() ? { selectedArtifactId: raw.selectedArtifactId } : {}),
    ...(typeof raw.selectedSubagentId === 'string' && raw.selectedSubagentId.trim() ? { selectedSubagentId: raw.selectedSubagentId } : {}),
  };
}

function loadState(): SingleAgentPanelState {
  try {
    const raw = window.localStorage.getItem(SINGLE_AGENT_PANEL_STATE_KEY);
    return raw ? normalizeState(JSON.parse(raw)) : { ...DEFAULT_STATE };
  } catch {
    return { ...DEFAULT_STATE };
  }
}

function publishState(state: SingleAgentPanelState): void {
  try {
    window.localStorage.setItem(SINGLE_AGENT_PANEL_STATE_KEY, JSON.stringify(state));
  } catch {
    // Keep in-memory state usable when storage is unavailable.
  }
  window.dispatchEvent(new CustomEvent<SingleAgentPanelState>(SINGLE_AGENT_PANEL_STATE_EVENT, { detail: state }));
}

export function openSingleAgentPanel(activeTab: SingleAgentToolTab, selectedArtifactId?: string): void {
  const nextState: SingleAgentPanelState = {
    ...loadState(),
    expanded: true,
    activeTab,
    ...(selectedArtifactId ? { selectedArtifactId } : {}),
  };
  publishState(nextState);
}

export function useSingleAgentPanelState(): UseSingleAgentPanelStateResult {
  const [state, setState] = useState<SingleAgentPanelState>(loadState);

  const updateState = useCallback((patch: Partial<SingleAgentPanelState>) => {
    setState(current => {
      const nextState = { ...current, ...patch };
      publishState(nextState);
      return nextState;
    });
  }, []);

  useEffect(() => {
    const handleStateChange = (event: Event) => {
      const nextState = (event as CustomEvent<SingleAgentPanelState>).detail;
      setState(normalizeState(nextState));
    };
    window.addEventListener(SINGLE_AGENT_PANEL_STATE_EVENT, handleStateChange);
    return () => window.removeEventListener(SINGLE_AGENT_PANEL_STATE_EVENT, handleStateChange);
  }, []);

  const setSingleAgentPanelExpanded = useCallback(
    (expanded: boolean) => {
      updateState({ expanded });
    },
    [updateState],
  );

  const setSingleAgentPanelActiveTab = useCallback(
    (activeTab: SingleAgentToolTab) => {
      updateState({ activeTab });
    },
    [updateState],
  );

  const setSingleAgentPanelSelectedArtifactId = useCallback(
    (selectedArtifactId: string) => {
      updateState({ selectedArtifactId });
    },
    [updateState],
  );

  const setSingleAgentPanelSelectedSubagentId = useCallback(
    (selectedSubagentId: string | null) => {
      updateState({ selectedSubagentId });
    },
    [updateState],
  );

  return {
    singleAgentPanelExpanded: state.expanded,
    singleAgentPanelActiveTab: state.activeTab,
    singleAgentPanelSelectedArtifactId: state.selectedArtifactId,
    singleAgentPanelSelectedSubagentId: state.selectedSubagentId,
    setSingleAgentPanelExpanded,
    setSingleAgentPanelActiveTab,
    setSingleAgentPanelSelectedArtifactId,
    setSingleAgentPanelSelectedSubagentId,
  };
}
