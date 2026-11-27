/**
 * 会话状态管理
 */

import { create } from 'zustand';
import { Session, AgentMode, WebConnectionState, ModelEntry } from '../types';

const STORAGE_KEY = 'jiuwenclaw_context_compression';
const MODE_STORAGE_KEY = 'jiuwenclaw_mode';
const MODEL_STORAGE_KEY = 'jiuwenclaw_selected_model';

function loadFromStorage() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      return JSON.parse(stored);
    }
  } catch (error) {
    console.error('Error loading context compression from storage:', error);
  }
  return null;
}

function saveToStorage(data: any) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch (error) {
    console.error('Error saving context compression to storage:', error);
  }
}

function loadModeFromStorage(): AgentMode {
  try {
    const stored = localStorage.getItem(MODE_STORAGE_KEY);
    if (stored) {
      return normalizeAgentMode(stored);
    }
  } catch (error) {
    console.error('Error loading mode from storage:', error);
  }
  return DEFAULT_MODE;
}

function saveModeToStorage(mode: AgentMode) {
  try {
    localStorage.setItem(MODE_STORAGE_KEY, mode);
  } catch (error) {
    console.error('Error saving mode to storage:', error);
  }
}

const DEFAULT_MODE: AgentMode = 'agent.plan';

function normalizeAgentMode(mode: unknown): AgentMode {
  if (typeof mode !== 'string') return DEFAULT_MODE;
  const normalized = mode.trim().toLowerCase();
  if (normalized === 'agent.fast') return 'agent.fast';
  if (normalized === 'team') return 'team';
  return 'agent.plan';
}

function normalizeSession(session: Session): Session {
  return {
    ...session,
    mode: normalizeAgentMode(session.mode),
  };
}

interface ConnectionStats {
  state: WebConnectionState;
  inflight: number;
  lastError: string | null;
}

type HeartbeatState = 'unknown' | 'ok' | 'alert';

interface HeartbeatHistoryItem {
  message: string;
  updatedAt: string;
  status: HeartbeatState;
}

interface MemoryUsage {
  rssMb: number | null;
  usedPercent: number | null;
}

interface ContextCompressionStats {
  rate: number;
  beforeCompressed: number | null;
  afterCompressed: number | null;
}

interface TeamTaskEvent {
  id: string;
  type: string;
  team_id: string;
  task_id: string;
  status: string;
  timestamp: number;
}

interface TeamMember {
  id: string;
  member_id: string;
  status: string;
  timestamp: number;
}

interface SessionState {
  currentSession: Session | null;
  sessions: Session[];
  mode: AgentMode;
  isConnected: boolean;
  availableTools: string[];
  connectionStats: ConnectionStats;
  contextCompressionRate: number;
  contextCompressionBefore: number | null;
  contextCompressionAfter: number | null;
  memoryUsage: MemoryUsage;
  heartbeatState: HeartbeatState;
  heartbeatMessage: string | null;
  heartbeatUpdatedAt: string | null;
  heartbeatHistory: HeartbeatHistoryItem[];
  teamTaskEvents: TeamTaskEvent[];
  teamMembers: TeamMember[];
  availableModels: ModelEntry[];
  selectedModelName: string | null;

  // Actions
  setCurrentSession: (session: Session | null) => void;
  setSessions: (sessions: Session[]) => void;
  addSession: (session: Session) => void;
  updateSession: (sessionId: string, updates: Partial<Session>) => void;
  removeSession: (sessionId: string) => void;
  setMode: (mode: AgentMode) => void;
  setConnected: (connected: boolean) => void;
  setAvailableTools: (tools: string[]) => void;
  setConnectionStats: (stats: Partial<ConnectionStats>) => void;
  setContextCompressionRate: (rate: number) => void;
  setContextCompressionStats: (stats: Partial<ContextCompressionStats> | null) => void;
  setMemoryUsage: (memoryUsage: Partial<MemoryUsage> | null) => void;
  setHeartbeatStatus: (
    status: HeartbeatState,
    message?: string | null,
    updatedAt?: string | null
  ) => void;
  setTeamTaskEvents: (events: TeamTaskEvent[]) => void;
  addTeamTaskEvent: (event: TeamTaskEvent) => void;
  setTeamMembers: (members: TeamMember[]) => void;
  addTeamMember: (member: TeamMember) => void;
  updateTeamMemberStatus: (memberId: string, newStatus: string, timestamp?: number) => void;
  setAvailableModels: (models: ModelEntry[], activeModel?: string) => void;
  setSelectedModelName: (name: string) => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  currentSession: null,
  sessions: [],
  mode: loadModeFromStorage(),
  isConnected: false,
  availableTools: [],
  connectionStats: {
    state: 'idle',
    inflight: 0,
    lastError: null,
  },
  contextCompressionRate: loadFromStorage()?.rate || 0,
  contextCompressionBefore: loadFromStorage()?.beforeCompressed || null,
  contextCompressionAfter: loadFromStorage()?.afterCompressed || null,
  memoryUsage: {
    rssMb: null,
    usedPercent: null,
  },
  heartbeatState: 'unknown',
  heartbeatMessage: null,
  heartbeatUpdatedAt: null,
  heartbeatHistory: [],
  teamTaskEvents: [],
  teamMembers: [],
  availableModels: [],
  selectedModelName: (() => {
    try { return localStorage.getItem(MODEL_STORAGE_KEY); } catch { return null; }
  })(),

  setCurrentSession: (session) => {
    const normalizedSession = session ? normalizeSession(session) : null;
    set((state) => ({
      currentSession: normalizedSession,
      mode: normalizedSession?.mode || state.mode,
    }));
  },

  setSessions: (sessions) => {
    set({ sessions: sessions.map(normalizeSession) });
  },

  addSession: (session) => {
    set((state) => ({
      sessions: [normalizeSession(session), ...state.sessions],
    }));
  },

  updateSession: (sessionId, updates) => {
    const normalizedUpdates =
      Object.prototype.hasOwnProperty.call(updates, 'mode')
        ? { ...updates, mode: normalizeAgentMode((updates as { mode?: unknown }).mode) }
        : updates;
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.session_id === sessionId ? normalizeSession({ ...s, ...normalizedUpdates }) : s
      ),
      currentSession:
        state.currentSession?.session_id === sessionId
          ? normalizeSession({ ...state.currentSession, ...normalizedUpdates })
          : state.currentSession,
    }));
  },

  removeSession: (sessionId) => {
    set((state) => ({
      sessions: state.sessions.filter((s) => s.session_id !== sessionId),
      currentSession:
        state.currentSession?.session_id === sessionId
          ? null
          : state.currentSession,
    }));
  },

  setMode: (mode) => {
    const normalizedMode = normalizeAgentMode(mode);
    saveModeToStorage(normalizedMode);
    set({ mode: normalizedMode });
  },

  setConnected: (connected) => {
    set({ isConnected: connected });
  },

  setAvailableTools: (tools) => {
    set({ availableTools: tools });
  },

  setConnectionStats: (stats) => {
    set((state) => ({
      connectionStats: {
        ...state.connectionStats,
        ...stats,
      },
    }));
  },

  setContextCompressionRate: (rate) => {
    const normalizedRate = Number.isFinite(rate) ? Math.min(Math.max(rate, 0), 100) : 0;
    set({ contextCompressionRate: Number(normalizedRate.toFixed(1)) });
  },

  setContextCompressionStats: (stats) => {
    if (!stats) {
      set({
        contextCompressionRate: 0,
        contextCompressionBefore: null,
        contextCompressionAfter: null,
      });
      saveToStorage(null);
      return;
    }

    const normalizedRate =
      typeof stats.rate === 'number' && Number.isFinite(stats.rate)
        ? Number(Math.min(Math.max(stats.rate, 0), 100).toFixed(1))
        : 0;
    const normalizedBefore =
      typeof stats.beforeCompressed === 'number' && Number.isFinite(stats.beforeCompressed)
        ? Math.max(Math.round(stats.beforeCompressed), 0)
        : null;
    const normalizedAfter =
      typeof stats.afterCompressed === 'number' && Number.isFinite(stats.afterCompressed)
        ? Math.max(Math.round(stats.afterCompressed), 0)
        : null;

    const contextCompressionData = {
      rate: normalizedRate,
      beforeCompressed: normalizedBefore,
      afterCompressed: normalizedAfter
    };

    set({
      contextCompressionRate: normalizedRate,
      contextCompressionBefore: normalizedBefore,
      contextCompressionAfter: normalizedAfter,
    });

    saveToStorage(contextCompressionData);
  },

  setMemoryUsage: (memoryUsage) => {
    if (!memoryUsage) {
      set({
        memoryUsage: {
          rssMb: null,
          usedPercent: null,
        },
      });
      return;
    }

    const normalizedRssMb =
      typeof memoryUsage.rssMb === 'number' && Number.isFinite(memoryUsage.rssMb)
        ? Number(Math.max(memoryUsage.rssMb, 0).toFixed(1))
        : null;
    const normalizedUsedPercent =
      typeof memoryUsage.usedPercent === 'number' && Number.isFinite(memoryUsage.usedPercent)
        ? Number(Math.min(Math.max(memoryUsage.usedPercent, 0), 100).toFixed(1))
        : null;

    set({
      memoryUsage: {
        rssMb: normalizedRssMb,
        usedPercent: normalizedUsedPercent,
      },
    });
  },

  setHeartbeatStatus: (status, message = null, updatedAt) => {
    set((state) => {
      const resolvedUpdatedAt = updatedAt === undefined ? new Date().toISOString() : updatedAt;
      const shouldClearHistory = message == null && updatedAt === null;
      const nextHistory = shouldClearHistory
        ? []
        : (message
          ? [{ message, updatedAt: resolvedUpdatedAt ?? new Date().toISOString(), status }, ...state.heartbeatHistory]
              .slice(0, 20)
          : state.heartbeatHistory);

      return {
        heartbeatState: status,
        heartbeatMessage: message,
        heartbeatUpdatedAt: resolvedUpdatedAt,
        heartbeatHistory: nextHistory,
      };
    });
  },
  setTeamTaskEvents: (events) => {
    set({ teamTaskEvents: events });
  },
  addTeamTaskEvent: (event) => {
    set((state) => {
      const existingIndex = state.teamTaskEvents.findIndex(
        (e) => e.task_id === event.task_id
      );
      if (existingIndex >= 0) {
        const updatedEvents = [...state.teamTaskEvents];
        updatedEvents[existingIndex] = {
          ...updatedEvents[existingIndex],
          ...event,
        };
        return { teamTaskEvents: updatedEvents };
      }
      return { teamTaskEvents: [event, ...state.teamTaskEvents] };
    });
  },
  setTeamMembers: (members) => {
    set({ teamMembers: members });
  },
  addTeamMember: (member) => {
    set((state) => {
      const existingIndex = state.teamMembers.findIndex(
        (m) => m.member_id === member.member_id
      );
      if (existingIndex >= 0) {
        const updatedMembers = [...state.teamMembers];
        updatedMembers[existingIndex] = {
          ...updatedMembers[existingIndex],
          ...member,
        };
        return { teamMembers: updatedMembers };
      }
      return { teamMembers: [member, ...state.teamMembers] };
    });
  },
  updateTeamMemberStatus: (memberId, newStatus, timestamp) => {
    set((state) => {
      const existingIndex = state.teamMembers.findIndex(
        (m) => m.member_id === memberId
      );
      if (existingIndex >= 0) {
        const updatedMembers = [...state.teamMembers];
        updatedMembers[existingIndex] = {
          ...updatedMembers[existingIndex],
          status: newStatus,
          timestamp: timestamp || Date.now(),
        };
        return { teamMembers: updatedMembers };
      }
      return state;
    });
  },
  setAvailableModels: (models, activeModel) => {
    set(() => {
      const names = models.map((m) => m.model_name);
      // 优先使用后端返回的 activeModel（默认模型）
      const selected = (activeModel && names.includes(activeModel))
        ? activeModel
        : names[0] || null;
      if (selected) {
        try { localStorage.setItem(MODEL_STORAGE_KEY, selected); } catch { /* noop */ }
      }
      return { availableModels: models, selectedModelName: selected };
    });
  },
  setSelectedModelName: (name) => {
    try { localStorage.setItem(MODEL_STORAGE_KEY, name); } catch { /* noop */ }
    set({ selectedModelName: name });
  },
}));
