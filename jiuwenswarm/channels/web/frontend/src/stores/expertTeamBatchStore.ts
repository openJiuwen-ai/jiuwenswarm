import { create } from 'zustand';
import i18n from 'i18next';
import { useChatStore } from './chatStore';
import { prefixedMessageId } from '../utils/uuid';
import { webRequest } from '../services/webClient';
import { useTeamSelectorStore } from './teamSelectorStore';
import { useSessionStore } from './sessionStore';

export interface ExpertTeamBatchResult {
  agent_group_name: string;
  status: 'created' | 'existing' | 'failed';
  team_id?: string;
  error?: string;
}

interface BatchRuntime {
  selected: string[];
  submitting: boolean;
  results: ExpertTeamBatchResult[];
  error: string | null;
  ownerGroup?: string | null;
}

const empty: BatchRuntime = { selected: [], submitting: false, results: [], error: null };

interface BatchState {
  runtimes: Record<string, BatchRuntime>;
  toggle: (sessionId: string, name: string) => void;
  clear: (sessionId: string) => void;
  submit: (sessionId: string, ensureSession: () => Promise<string | null>) => Promise<void>;
}

function reportBatchError(sessionId: string, error: string) {
  const chat = useChatStore.getState();
  chat.ensureRuntime(sessionId);
  chat.addMessage(sessionId, {
    id: prefixedMessageId('expert-batch-error-'),
    role: 'assistant',
    content: i18n.t('chat.expertBatchError', { error }),
    timestamp: new Date().toISOString(),
  });
}

export const useExpertTeamBatchStore = create<BatchState>((set, get) => ({
  runtimes: {},
  toggle: (sessionId, name) => set((state) => {
    const current = state.runtimes[sessionId] ?? empty;
    if (current.submitting) return state;
    const selected = current.selected.includes(name)
      ? current.selected.filter((item) => item !== name) : [...current.selected, name];
    return { runtimes: { ...state.runtimes, [sessionId]: { ...current, selected, error: null } } };
  }),
  clear: (sessionId) => set((state) => state.runtimes[sessionId]?.submitting ? state : ({
    runtimes: { ...state.runtimes, [sessionId]: empty },
  })),
  submit: async (sessionId, ensureSession) => {
    const current = get().runtimes[sessionId] ?? empty;
    if (current.submitting || !current.selected.length) return;
    const pending = { ...current, submitting: true, error: null, results: [] };
    const update = (id: string, runtime: BatchRuntime) => set((state) => ({
      runtimes: { ...state.runtimes, [id]: runtime },
    }));
    update(sessionId, pending);
    let target = sessionId;
    try {
      const actualSession = await ensureSession();
      if (!actualSession) throw new Error('Session creation did not complete');
      target = actualSession;
      // Route changes can unmount the picker; progress belongs to the session.
      if (target !== sessionId) {
        update(target, pending);
        update(sessionId, empty);
      }
      const response = await webRequest<{ results: ExpertTeamBatchResult[]; owner_agent_group_name?: string | null }>(
        'org.expert_teams.add', {
          session_id: target, agent_group_names: current.selected,
          team_dispatch_mode: useSessionStore.getState().runtimes[target]?.teamDispatchMode,
          team_bootstrap_mode: useSessionStore.getState().runtimes[target]?.teamBootstrapMode,
          enable_swarmflow: useSessionStore.getState().runtimes[target]?.enableSwarmflow,
          swarmflow_budget: useSessionStore.getState().runtimes[target]?.swarmflowBudget,
        },
        { timeoutMs: 180000 },
      );
      update(target, {
        selected: response.results.filter((item) => item.status === 'failed').map((item) => item.agent_group_name),
        submitting: false, results: response.results, error: null,
        ownerGroup: response.owner_agent_group_name ?? current.ownerGroup,
      });
      const failures = response.results.filter((item) => item.status === 'failed');
      if (failures.length) {
        reportBatchError(target, failures.map((item) =>
          `${item.agent_group_name}: ${item.error || i18n.t('chat.expertBatchUnknownError')}`,
        ).join('\n'));
      }
    } catch (error) {
      reportBatchError(target, String(error));
      update(target, { ...pending, submitting: false, error: String(error) });
      return;
    }
    try {
      await useTeamSelectorStore.getState().fetchTeams(target);
    } catch (error) {
      console.warn('Failed to refresh teams after expert team creation:', error);
    }
  },
}));

export const EMPTY_EXPERT_BATCH = empty;
