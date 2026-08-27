/**
 * 会话状态管理（多 session 版本）
 *
 * 全局字段保持不变，session 级字段按 session 隔离存储在 runtimes 中。
 */

import { create } from 'zustand';
import {
  Session,
  AgentMode,
  ModelEntry,
  Message,
  ContextCompressionRuntime,
  ContextCompressionSummary,
  TeamMemberContextCompressionState,
} from '../types';
import {
  createTaskProgressBaseline,
  mergeTaskProgressBaseline,
  registerConfirmedTaskCreation,
  type TaskProgressBaseline,
} from '../features/teamTaskProgressBaseline';
import { isTeamAgentMode, stripPlanSuffix } from '../features/planMode/wireMode';

const MODE_STORAGE_KEY = 'jiuwenclaw_mode';
const MODEL_STORAGE_KEY = 'jiuwenclaw_selected_model';

function loadModeFromStorage(): AgentMode {
  if (typeof localStorage === 'undefined') return DEFAULT_MODE;
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
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(MODE_STORAGE_KEY, mode);
  } catch (error) {
    console.error('Error saving mode to storage:', error);
  }
}

const DEFAULT_MODE: AgentMode = 'agent';

function normalizeAgentMode(mode: unknown): AgentMode {
  if (typeof mode !== 'string') return DEFAULT_MODE;
  if (isTeamAgentMode(mode)) return 'team';
  // 后端 session.mode 可能是新命名 `agent.{work|code}.plan`，先剥掉 plan 后缀
  // 再归一化（旧 `team.plan.*` / `team.code.*` 已被上面的 isTeamAgentMode 拦截）。
  const normalized = stripPlanSuffix(mode.trim().toLowerCase());
  if (normalized === 'auto_harness') return 'auto_harness';
  return 'agent';
}

function normalizeSession(session: Session): Session {
  return {
    ...session,
    mode: normalizeAgentMode(session.mode),
  };
}

/**
 * 按 `alias || model_name` 在可选模型列表里解析出"实际生效"的模型条目。
 *
 * 背景（bug003）：会话记录的 `selectedModelName` 只是一个名字字符串，模型改名/改别名后
 * 这个字符串可能不再对应任何可选模型。之前 UI 显示（`InputArea.tsx` 的 `ModelSelector`）
 * 会做兜底匹配，但实际发给后端的 `getEffectiveModelName` 没有做同样的兜底，导致"显示值"
 * 和"实际请求的 model_name"可能不一致，且旧字符串失配后无法感知。抽成共享函数后两边统一
 * 走同一次解析，谁都不会再吐出陈旧、未经校验的名字字符串。
 *
 * @param chatAvailableModels 当前可选的模型列表（is_default!==false 的模型）
 * @param selectedModelName 该会话记录的模型名字字符串（可能是改名前的陈旧值）
 * @param defaultModelName 后端配置的默认模型名字字符串
 * @returns 解析命中的模型条目；`chatAvailableModels` 为空（模型列表尚未加载）时返回 null
 */
export function resolveEffectiveModel(
  chatAvailableModels: ModelEntry[],
  selectedModelName: string | null,
  defaultModelName: string | null,
): ModelEntry | null {
  if (chatAvailableModels.length === 0) return null;
  const displayed = selectedModelName || defaultModelName;
  // selectedModelName 可能存的是展示名（用户从下拉框选择时存的是 alias），
  // 也可能存的是真实 API id（后端 session.metadata.model 回传恢复时是
  // model_name，例如 Zen 免费模型的 "deepseek-v4-flash-free"）。两者都要能
  // 命中同一个 entry，否则后端回传 model_name 后无法匹配有 alias 的免费
  // 模型，会回退到 chatAvailableModels[0]（首个配置模型），表现为"对话
  // 完成后下拉框自动切回配置的模型"。
  return (
    chatAvailableModels.find(
      (m) => m.alias === displayed || m.model_name === displayed,
    ) ??
    chatAvailableModels.find((m) => (m.alias || m.model_name) === displayed) ??
    chatAvailableModels[0]
  );
}

/**
 * Resolve the model shown by the chat selector.
 *
 * 单 Agent 与集群（team）模式共用同一套解析：都展示会话自选的模型，
 * 失配时回退默认模型（见 resolveEffectiveModel）。
 */
export function resolveChatModelSelection(
  chatAvailableModels: ModelEntry[],
  selectedModelName: string | null,
  defaultModelName: string | null,
): ModelEntry | null {
  return resolveEffectiveModel(
    chatAvailableModels,
    selectedModelName,
    defaultModelName,
  );
}

/** Resolve a configured display name to the model ID required by backend RPCs. */
export function resolveConfiguredModelName(
  availableModels: ModelEntry[],
  configuredModelName: string | null,
): string | null {
  const normalizedName = configuredModelName?.trim();
  if (!normalizedName) return null;
  return availableModels.find(
    (model) => model.alias === normalizedName || model.model_name === normalizedName,
  )?.model_name ?? null;
}

const FINAL_EVENT_DUPLICATE_WINDOW_MS = 60_000;

function normalizeExecutionContent(content?: string): string {
  return (content || '').replace(/\s+/g, ' ').trim();
}

function isDuplicateFinalExecutionEvent(
  existing: TeamMemberExecutionEvent,
  next: TeamMemberExecutionEvent
): boolean {
  if (existing.kind !== 'final' || next.kind !== 'final') {
    return false;
  }
  if (existing.member_id !== next.member_id) {
    return false;
  }
  if (!normalizeExecutionContent(existing.content)) {
    return false;
  }
  if (normalizeExecutionContent(existing.content) !== normalizeExecutionContent(next.content)) {
    return false;
  }
  return Math.abs((existing.timestamp || 0) - (next.timestamp || 0)) <= FINAL_EVENT_DUPLICATE_WINDOW_MS;
}

function dedupeTeamMemberExecutionEvents(
  events: TeamMemberExecutionEvent[]
): TeamMemberExecutionEvent[] {
  const deduped: TeamMemberExecutionEvent[] = [];
  for (const event of events) {
    const duplicateIndex = deduped.findIndex((item) => isDuplicateFinalExecutionEvent(item, event));
    if (duplicateIndex >= 0) {
      deduped[duplicateIndex] = {
        ...deduped[duplicateIndex],
        ...event,
        id: deduped[duplicateIndex].id,
        timestamp: Math.min(deduped[duplicateIndex].timestamp || event.timestamp, event.timestamp),
      };
      continue;
    }
    deduped.push(event);
  }
  return deduped;
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

export interface TeamTaskEvent {
  id: string;
  type: string;
  team_id: string;
  task_id: string;
  status: string;
  timestamp: number;
  member_id?: string;
  assignee?: string;
  team_name?: string;
  title?: string;
  content?: string;
  // Truncation observability flags — backend may set these on team.task.created/
  // updated events when the title/content exceeded the wire limit. Purely
  // passthrough: the store does not render a badge; the inline marker
  // `…(truncated, total N chars)` already surfaces truncation to the user.
  title_truncated?: boolean;
  title_original_size?: number;
  content_truncated?: boolean;
  content_original_size?: number;
  updated_at?: number | string | null;
}

export type TeamTaskStatus =
  | 'pending'
  | 'blocked'
  | 'planning'
  | 'in_progress'
  | 'in_review'
  | 'completed'
  | 'cancelled';

export interface TeamTask {
  task_id: string;
  title?: string;
  content?: string;
  status: TeamTaskStatus;
  assignee?: string;
  team_id?: string;
  timestamp?: number;
  skills?: string[];
  files?: string[];
  // Truncation observability flags — set by the backend on team.task.created/
  // updated events when title/content exceeded the wire limit. Carried through
  // the normalize/upsert pipeline; a status-only event MUST NOT reset these
  // (upsertTeamTask uses `?? existing`). Not rendered as a badge — the inline
  // marker `…(truncated, total N chars)` already shows truncation.
  title_truncated?: boolean;
  title_original_size?: number;
  content_truncated?: boolean;
  content_original_size?: number;
}

// Upsert input: a task event may omit status (e.g. a content-only update).
// The store then preserves the task's existing status instead of resetting it.
export type TeamTaskUpsert = Omit<TeamTask, 'status'> & { status?: TeamTaskStatus };

interface TeamMember {
  id: string;
  member_id: string;
  status: string;
  timestamp: number;
  name?: string;
  execution_status?: string | null;
  mode?: string;
  /** TeamRole 值：leader / teammate / human_agent / bridge_agent / worker */
  role?: string;
  /** 外部 CLI 后端名（claude / codex / ...），普通成员为空 */
  cli_agent?: string | null;
}

/** 增量成员事件里的空字段不得覆盖已知值：返回 next，空则回退 prev。 */
function keepKnownMemberField(
  next: string | null | undefined,
  prev: string | null | undefined
): string | undefined {
  if (typeof next === 'string' && next.trim() !== '') return next;
  return typeof prev === 'string' && prev.trim() !== '' ? prev : undefined;
}

export type HumanShareStatus = 'pending' | 'joined' | 'left';

export interface HumanShareCommand {
  memberName: string;
  displayName?: string;
  sessionId: string;
  teamName: string;
  sessionRef: string;
  joinCommand: string;
  exitCommand: string;
  status: HumanShareStatus;
  sourceChannel?: string;
  userId?: string;
  updatedAt: number;
}

export type TeamMemberExecutionEventKind =
  | 'final'
  | 'tool_call'
  | 'tool_result'
  | 'file';

export interface TeamMemberExecutionEvent {
  id: string;
  member_id: string;
  kind: TeamMemberExecutionEventKind;
  timestamp: number;
  title: string;
  content?: string;
  tool_name?: string;
  tool_call_id?: string;
  files?: Array<{
    name: string;
    size?: number;
    mime_type?: string;
    download_url?: string;
    path?: string;
  }>;
}

/**
 * 单个 session 的运行态。
 * 原 B 类全局字段全部迁移到这里，按 session 隔离。
 */
export interface SessionRuntime {
  mode: AgentMode;
  selectedModelName: string | null;
  projectDirectory: string | null;
  /** 新会话草稿值；真实 Session 创建后由后端 metadata 的权威值覆盖。 */
  persistSession: boolean;
  contextCompressionRate: number;
  contextCompressionBefore: number | null;
  contextCompressionAfter: number | null;
  teamTaskEvents: TeamTaskEvent[];
  teamTasks: TeamTask[];
  teamTaskProgressBaseline: TaskProgressBaseline;
  teamMembers: TeamMember[];
  teamLeaderMemberIds: string[];
  teamHumanShareCommands: HumanShareCommand[];
  teamMemberExecutionEvents: TeamMemberExecutionEvent[];
  teamMemberContextCompression: Record<string, TeamMemberContextCompressionState>;
  teamHistoryMessages: Message[];
  /** 当前会话输入栏已选中的技能名（用于随消息发送，发送后清空——一次性语义） */
  selectedSkills: string[];
  /** skill-creator 统一入口等场景的会话级元数据，随 chat.send 发送后清除 */
  metadata?: Record<string, unknown>;
  /**
   * 本会话期间持续启用的插件id/MCP名，由输入框"+"菜单"扩展"面板的开关控制。与
   * selectedSkills 不同：这两个字段发 chat.send 后不清空，会一直带在每条消息里，直到用户在
   * 面板里手动关闭开关。插件字段名 plugin_names 后端尚未定义（backend-requests.md 需求11，
   * 前端乐观发送，后端目前忽略）；mcp 字段名是 MCP 接口文档 v2 §6.2 的权威定义。
   */
  enabledPlugins: string[];
  enabledMcps: string[];
}

function createEmptyRuntime(): SessionRuntime {
  return {
    mode: loadModeFromStorage(),
    selectedModelName: (() => {
      if (typeof localStorage === 'undefined') return null;
      try { return localStorage.getItem(MODEL_STORAGE_KEY); } catch { return null; }
    })(),
    projectDirectory: null,
    persistSession: false,
    contextCompressionRate: 0,
    contextCompressionBefore: null,
    contextCompressionAfter: null,
    teamTaskEvents: [],
    teamTasks: [],
    teamTaskProgressBaseline: createTaskProgressBaseline(),
    teamMembers: [],
    teamLeaderMemberIds: [],
    teamHumanShareCommands: [],
    teamMemberExecutionEvents: [],
    teamMemberContextCompression: {},
    teamHistoryMessages: [],
    selectedSkills: [],
    metadata: undefined,
    enabledPlugins: [],
    enabledMcps: [],
  };
}

interface SessionState {
  // A 类全局字段
  currentSession: Session | null;
  sessions: Session[];
  isConnected: boolean;
  availableTools: string[];
  memoryUsage: MemoryUsage;
  availableModels: ModelEntry[];
  /** 过滤 is_default=true 的模型，供聊天窗口 ModelSelector 使用 */
  chatAvailableModels: ModelEntry[];
  /** 后端配置的默认模型 ID，供集群模式和新建会话取用，不受任何会话手动切换模型影响 */
  defaultModelName: string | null;

  // B 类 session 级字段
  runtimes: Record<string, SessionRuntime>;

  // Runtime 管理方法
  ensureRuntime: (sessionId: string) => SessionRuntime;
  getRuntime: (sessionId: string | null) => SessionRuntime | undefined;
  getEffectiveModelName: (sessionId: string | null) => string | null;
  removeRuntime: (sessionId: string) => void;

  // A 类 actions（不加 sessionId）
  setCurrentSession: (session: Session | null) => void;
  setSessions: (sessions: Session[]) => void;
  addSession: (session: Session) => void;
  updateSession: (sessionId: string, updates: Partial<Session>) => void;
  removeSession: (sessionId: string) => void;
  setConnected: (connected: boolean) => void;
  setAvailableTools: (tools: string[]) => void;
  setContextCompressionStats: (sessionId: string, stats: Partial<ContextCompressionStats> | null) => void;
  setMemoryUsage: (memoryUsage: Partial<MemoryUsage> | null) => void;
  setAvailableModels: (models: ModelEntry[], activeModel?: string) => void;
  setSelectedModelName: (sessionId: string, name: string) => void;

  // B 类 actions（加 sessionId）
  setMode: (sessionId: string, mode: AgentMode) => void;
  setProjectDirectory: (sessionId: string, directory: string | null) => void;
  setPersistSession: (sessionId: string, enabled: boolean) => void;
  setTeamTaskEvents: (sessionId: string, events: TeamTaskEvent[]) => void;
  addTeamTaskEvent: (sessionId: string, event: TeamTaskEvent) => void;
  setTeamTasks: (sessionId: string, tasks: TeamTask[]) => void;
  registerConfirmedTeamTaskCreation: (sessionId: string, taskId: string) => void;
  mergeTeamTaskProgressBaseline: (sessionId: string, baseline: TaskProgressBaseline) => void;
  upsertTeamTask: (sessionId: string, task: TeamTaskUpsert) => void;
  updateTeamTask: (sessionId: string, taskId: string, patch: Partial<TeamTask>) => void;
  setTeamMembers: (sessionId: string, members: TeamMember[]) => void;
  setTeamLeaderMemberIds: (sessionId: string, memberIds: string[]) => void;
  addTeamLeaderMemberId: (sessionId: string, memberId: string) => void;
  /** 输入栏已选技能：追加（去重） */
  addSelectedSkill: (sessionId: string, skill: string) => void;
  /** 输入栏已选技能：移除指定项 */
  removeSelectedSkill: (sessionId: string, skill: string) => void;
  /** 输入栏已选技能：清空 */
  clearSelectedSkills: (sessionId: string) => void;
  /** 设置/清除会话级元数据（skill-creator 统一入口等场景） */
  setSessionMetadata: (sessionId: string, metadata: Record<string, unknown> | null) => void;
  /** 本会话启用插件：追加（去重） */
  addEnabledPlugin: (sessionId: string, pluginId: string) => void;
  /** 本会话启用插件：移除指定项 */
  removeEnabledPlugin: (sessionId: string, pluginId: string) => void;
  /** 本会话启用插件：清空 */
  clearEnabledPlugins: (sessionId: string) => void;
  /** 本会话启用MCP：追加（去重） */
  addEnabledMcp: (sessionId: string, mcpName: string) => void;
  /** 本会话启用MCP：移除指定项 */
  removeEnabledMcp: (sessionId: string, mcpName: string) => void;
  /** 本会话启用MCP：清空 */
  clearEnabledMcps: (sessionId: string) => void;
  addTeamMember: (sessionId: string, member: TeamMember) => void;
  updateTeamMemberStatus: (sessionId: string, memberId: string, newStatus: string, timestamp?: number) => void;
  setTeamHumanShareCommands: (sessionId: string, commands: HumanShareCommand[]) => void;
  upsertTeamHumanShareCommand: (sessionId: string, command: HumanShareCommand) => void;
  updateTeamHumanShareStatus: (
    sessionId: string,
    memberName: string,
    status: HumanShareStatus,
    patch?: Partial<HumanShareCommand>
  ) => void;
  setTeamMemberExecutionEvents: (sessionId: string, events: TeamMemberExecutionEvent[]) => void;
  addTeamMemberExecutionEvent: (sessionId: string, event: TeamMemberExecutionEvent) => void;
  setTeamMemberContextCompressionStatus: (
    sessionId: string,
    memberId: string,
    runtime?: ContextCompressionRuntime,
    summary?: ContextCompressionSummary
  ) => void;
  clearTeamMemberContextCompressionStatus: (sessionId: string, memberId: string) => void;
  clearAllTeamMemberContextCompressionStatus: (sessionId: string) => void;
  setTeamHistoryMessages: (sessionId: string, messages: Message[]) => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  currentSession: null,
  sessions: [],
  isConnected: false,
  availableTools: [],
  memoryUsage: {
    rssMb: null,
    usedPercent: null,
  },
  availableModels: [],
  chatAvailableModels: [],
  defaultModelName: null,
  runtimes: {},

  ensureRuntime: (sessionId) => {
    const existing = get().runtimes[sessionId];
    if (existing) return existing;
    const runtime = createEmptyRuntime();
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: runtime },
    }));
    return runtime;
  },

  getRuntime: (sessionId) => {
    if (!sessionId) return undefined;
    return get().runtimes[sessionId];
  },

  getEffectiveModelName: (sessionId) => {
    if (!sessionId) return null;
    const state = get();
    const runtime = state.runtimes[sessionId];
    if (!runtime) return null;
    // 不再原样吐出 runtime.selectedModelName（可能是模型改名后失配的陈旧字符串），
    // 而是走与 UI 显示（ModelSelector）相同的解析逻辑，确保发给后端的 model_name
    // 参数与界面上显示的模型永远指向同一个 entry（bug003）。
    // 单 Agent 与集群（team）模式统一走同一套解析——集群模式下用户同样可以自选模型，
    // 后端 team_helpers 会把它透传给未显式配置 per-agent model 的团队成员。
    //
    // 注意：这里返回的是 model_name 而非 alias。后端 _model_cache 以 model_name 为
    // key 查找（包括 Zen 免费模型如 "laguna-s-2.1-free"）；alias 只是展示名（如
    // "Laguna S 2.1"），后端无法据此解析，会回退到默认模型。
    const resolved = resolveEffectiveModel(
      state.chatAvailableModels,
      runtime.selectedModelName,
      state.defaultModelName,
    );
    return resolved ? resolved.model_name : runtime.selectedModelName;
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      const next = { ...state.runtimes };
      delete next[sessionId];
      return { runtimes: next };
    });
  },

  setCurrentSession: (session) => {
    const normalizedSession = session ? normalizeSession(session) : null;
    set((state) => {
      if (!normalizedSession) {
        return { currentSession: null };
      }
      const sessionId = normalizedSession.session_id;
      const existingRuntime = state.runtimes[sessionId];
      const baseRuntime = existingRuntime || createEmptyRuntime();
      const nextRuntime: SessionRuntime = {
        ...baseRuntime,
        mode: normalizedSession.mode || baseRuntime.mode,
        persistSession: normalizedSession.persist_session === true,
        teamHistoryMessages: baseRuntime.teamHistoryMessages,
      };
      return {
        currentSession: normalizedSession,
        runtimes: { ...state.runtimes, [sessionId]: nextRuntime },
      };
    });
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

  setMode: (sessionId, mode) => {
    const normalizedMode = normalizeAgentMode(mode);
    saveModeToStorage(normalizedMode);
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, mode: normalizedMode },
        },
      };
    });
  },

  setProjectDirectory: (sessionId, directory) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, projectDirectory: directory },
        },
      };
    });
  },

  setPersistSession: (sessionId, enabled) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, persistSession: Boolean(enabled) },
        },
      };
    });
  },

  setConnected: (connected) => {
    set({ isConnected: connected });
  },

  setAvailableTools: (tools) => {
    set({ availableTools: tools });
  },

  setContextCompressionStats: (sessionId, stats) => {
    if (!stats) {
      set((state) => {
        const runtime = state.runtimes[sessionId];
        if (!runtime) return state;
        return { runtimes: { ...state.runtimes, [sessionId]: {
          ...runtime, contextCompressionRate: 0, contextCompressionBefore: null, contextCompressionAfter: null,
        } } };
      });
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

    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return { runtimes: { ...state.runtimes, [sessionId]: {
        ...runtime,
        contextCompressionRate: normalizedRate,
        contextCompressionBefore: normalizedBefore,
        contextCompressionAfter: normalizedAfter,
      } } };
    });
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

  setTeamTaskEvents: (sessionId, events) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamTaskEvents: events },
        },
      };
    });
  },

  addTeamTaskEvent: (sessionId, event) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamTaskEvents.findIndex(
        (e) => e.task_id === event.task_id
      );
      if (existingIndex >= 0) {
        const updatedEvents = [...runtime.teamTaskEvents];
        updatedEvents[existingIndex] = {
          ...updatedEvents[existingIndex],
          ...event,
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamTaskEvents: updatedEvents },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamTaskEvents: [event, ...runtime.teamTaskEvents] },
        },
      };
    });
  },

  setTeamTasks: (sessionId, tasks) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            teamTasks: tasks,
            teamTaskProgressBaseline: tasks.length === 0
              ? createTaskProgressBaseline()
              : runtime.teamTaskProgressBaseline,
          },
        },
      };
    });
  },

  registerConfirmedTeamTaskCreation: (sessionId, taskId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const baseline = registerConfirmedTaskCreation(
        runtime.teamTasks,
        runtime.teamTaskProgressBaseline,
        taskId
      );
      if (baseline === runtime.teamTaskProgressBaseline) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamTaskProgressBaseline: baseline },
        },
      };
    });
  },

  mergeTeamTaskProgressBaseline: (sessionId, restoredBaseline) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            teamTaskProgressBaseline: mergeTaskProgressBaseline(
              runtime.teamTaskProgressBaseline,
              restoredBaseline
            ),
          },
        },
      };
    });
  },

  upsertTeamTask: (sessionId, task) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamTasks.findIndex(
        (item) => item.task_id === task.task_id
      );
      if (existingIndex >= 0) {
        const existing = runtime.teamTasks[existingIndex];
        const updatedTasks = [...runtime.teamTasks];
        updatedTasks[existingIndex] = {
          ...existing,
          ...task,
          // An event without an explicit status (e.g. a content-only update)
          // must not reset the task; keep the existing status.
          status: task.status ?? existing.status,
          title: task.title ?? existing.title,
          content: task.content ?? existing.content,
          assignee: task.assignee ?? existing.assignee,
          team_id: task.team_id ?? existing.team_id,
          skills: task.skills ?? existing.skills,
          files: task.files ?? existing.files,
          // Truncation flags: a status-only event carries none, so `?? existing`
          // preserves whatever a prior created/updated event set. NEVER reset
          // these to false/undefined on a status-only upsert.
          title_truncated: task.title_truncated ?? existing.title_truncated,
          title_original_size: task.title_original_size ?? existing.title_original_size,
          content_truncated: task.content_truncated ?? existing.content_truncated,
          content_original_size: task.content_original_size ?? existing.content_original_size,
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamTasks: updatedTasks },
          },
        };
      }
      // New card: a status-only event may arrive before the created event,
      // leaving an empty title. Fall back to a placeholder built from the
      // task_id tail so the card is not rendered with a bare empty title
      // (matches the precedent in features/teamHistoryPanelRestore.ts upsertTask).
      return {
       runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamTasks: [{
            ...task,
            status: task.status ?? 'pending',
            title: task.title ?? `任务 ${String(task.task_id || '').slice(-6)}`,
          }, ...runtime.teamTasks],
      },
        },
      };
    });
  },

  updateTeamTask: (sessionId, taskId, patch) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamTasks.findIndex(
        (task) => task.task_id === taskId
      );
      if (existingIndex < 0) {
        return state;
      }
      const updatedTasks = [...runtime.teamTasks];
      updatedTasks[existingIndex] = {
        ...updatedTasks[existingIndex],
        ...patch,
        title: patch.title ?? updatedTasks[existingIndex].title,
        content: patch.content ?? updatedTasks[existingIndex].content,
        assignee: patch.assignee ?? updatedTasks[existingIndex].assignee,
        team_id: patch.team_id ?? updatedTasks[existingIndex].team_id,
        skills: patch.skills ?? updatedTasks[existingIndex].skills,
        files: patch.files ?? updatedTasks[existingIndex].files,
      };
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamTasks: updatedTasks },
        },
      };
    });
  },

  setTeamMembers: (sessionId, members) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const memberIds = new Set(members.map((member) => member.member_id));
      const nextCompression = Object.fromEntries(
        Object.entries(runtime.teamMemberContextCompression).filter(([memberId]) => memberIds.has(memberId))
      );
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            teamMembers: members,
            teamMemberContextCompression: nextCompression,
          },
        },
      };
    });
  },

  setTeamLeaderMemberIds: (sessionId, memberIds) => {
    const normalized = Array.from(
      new Set(memberIds.map((memberId) => memberId.trim()).filter(Boolean))
    );
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamLeaderMemberIds: normalized },
        },
      };
    });
  },

  addTeamLeaderMemberId: (sessionId, memberId) => {
    const normalized = memberId.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.teamLeaderMemberIds.includes(normalized)) {
        return state;
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamLeaderMemberIds: [...runtime.teamLeaderMemberIds, normalized] },
        },
      };
    });
  },

  addSelectedSkill: (sessionId, skill) => {
    const normalized = skill.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      if (runtime.selectedSkills.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, selectedSkills: [...runtime.selectedSkills, normalized] },
        },
      };
    });
  },

  removeSelectedSkill: (sessionId, skill) => {
    const normalized = skill.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!runtime.selectedSkills.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, selectedSkills: runtime.selectedSkills.filter((s) => s !== normalized) },
        },
      };
    });
  },

  clearSelectedSkills: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.selectedSkills.length === 0) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, selectedSkills: [] },
        },
      };
    });
  },

  setSessionMetadata: (sessionId, metadata) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, metadata: metadata ?? undefined },
        },
      };
    });
  },

  addEnabledPlugin: (sessionId, pluginId) => {
    const normalized = pluginId.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      if (runtime.enabledPlugins.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledPlugins: [...runtime.enabledPlugins, normalized] },
        },
      };
    });
  },

  removeEnabledPlugin: (sessionId, pluginId) => {
    const normalized = pluginId.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!runtime.enabledPlugins.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledPlugins: runtime.enabledPlugins.filter((s) => s !== normalized) },
        },
      };
    });
  },

  clearEnabledPlugins: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.enabledPlugins.length === 0) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledPlugins: [] },
        },
      };
    });
  },

  addEnabledMcp: (sessionId, mcpName) => {
    const normalized = mcpName.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      if (runtime.enabledMcps.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledMcps: [...runtime.enabledMcps, normalized] },
        },
      };
    });
  },

  removeEnabledMcp: (sessionId, mcpName) => {
    const normalized = mcpName.trim();
    if (!normalized) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!runtime.enabledMcps.includes(normalized)) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledMcps: runtime.enabledMcps.filter((s) => s !== normalized) },
        },
      };
    });
  },

  clearEnabledMcps: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (runtime.enabledMcps.length === 0) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, enabledMcps: [] },
        },
      };
    });
  },

  addTeamMember: (sessionId, member) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamMembers.findIndex(
        (m) => m.member_id === member.member_id
      );
      if (existingIndex >= 0) {
        const updatedMembers = [...runtime.teamMembers];
        const existingMember = updatedMembers[existingIndex];
        // 每类成员事件只带自己关心的字段（如 team.member.spawned 不带 name），
        // 直接展开覆盖会把已知的展示名/模式抹成 undefined，界面就退回显示
        // member_id。空值一律不覆盖已有值，规则同 ToolPanel 的 mergeById。
        updatedMembers[existingIndex] = {
          ...existingMember,
          ...member,
          name: keepKnownMemberField(member.name, existingMember.name),
          status: keepKnownMemberField(member.status, existingMember.status) ?? '',
          execution_status: keepKnownMemberField(
            member.execution_status,
            existingMember.execution_status
          ),
          mode: keepKnownMemberField(member.mode, existingMember.mode),
          role: keepKnownMemberField(member.role, existingMember.role),
          cli_agent: keepKnownMemberField(member.cli_agent, existingMember.cli_agent),
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamMembers: updatedMembers },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMembers: [member, ...runtime.teamMembers] },
        },
      };
    });
  },

  updateTeamMemberStatus: (sessionId, memberId, newStatus, timestamp) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamMembers.findIndex(
        (m) => m.member_id === memberId
      );
      if (existingIndex >= 0) {
        const updatedMembers = [...runtime.teamMembers];
        updatedMembers[existingIndex] = {
          ...updatedMembers[existingIndex],
          status: newStatus,
          timestamp: timestamp || Date.now(),
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamMembers: updatedMembers },
          },
        };
      }
      return state;
    });
  },

  setTeamHumanShareCommands: (sessionId, commands) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamHumanShareCommands: commands },
        },
      };
    });
  },

  upsertTeamHumanShareCommand: (sessionId, command) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existingIndex = runtime.teamHumanShareCommands.findIndex(
        (item) => item.memberName === command.memberName && item.sessionId === command.sessionId
      );
      if (existingIndex >= 0) {
        const updated = [...runtime.teamHumanShareCommands];
        const existing = updated[existingIndex];
        updated[existingIndex] = {
          ...existing,
          ...command,
          displayName: command.displayName || existing.displayName,
          teamName: command.teamName || existing.teamName,
          sessionRef: command.sessionRef || existing.sessionRef,
          joinCommand: command.joinCommand || existing.joinCommand,
          exitCommand: command.exitCommand || existing.exitCommand,
          status:
            command.status === 'pending' && existing.status !== 'pending'
              ? existing.status
              : command.status,
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamHumanShareCommands: updated },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            teamHumanShareCommands: [...runtime.teamHumanShareCommands, command],
          },
        },
      };
    });
  },

  updateTeamHumanShareStatus: (sessionId, memberName, status, patch = {}) => {
    const normalizedMemberName = memberName.trim();
    if (!normalizedMemberName) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            teamHumanShareCommands: runtime.teamHumanShareCommands.map((command) =>
              command.memberName === normalizedMemberName
                ? {
                    ...command,
                    ...patch,
                    status,
                    updatedAt: Date.now(),
                  }
                : command
            ),
          },
        },
      };
    });
  },

  setTeamMemberExecutionEvents: (sessionId, events) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMemberExecutionEvents: dedupeTeamMemberExecutionEvents(events).slice(0, 300) },
        },
      };
    });
  },

  addTeamMemberExecutionEvent: (sessionId, event) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const eventPatch = Object.fromEntries(
        Object.entries(event).filter(([, value]) => value !== undefined)
      ) as TeamMemberExecutionEvent;
      const duplicateIndex = runtime.teamMemberExecutionEvents.findIndex(
        (item) => isDuplicateFinalExecutionEvent(item, eventPatch)
      );
      if (duplicateIndex >= 0) {
        const updatedEvents = [...runtime.teamMemberExecutionEvents];
        updatedEvents[duplicateIndex] = {
          ...updatedEvents[duplicateIndex],
          ...eventPatch,
          id: updatedEvents[duplicateIndex].id,
          timestamp: Math.min(updatedEvents[duplicateIndex].timestamp || eventPatch.timestamp, eventPatch.timestamp),
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamMemberExecutionEvents: updatedEvents },
          },
        };
      }
      const existingIndex = runtime.teamMemberExecutionEvents.findIndex(
        (item) => item.id === event.id
      );
      if (existingIndex >= 0) {
        const updatedEvents = [...runtime.teamMemberExecutionEvents];
        updatedEvents[existingIndex] = {
          ...updatedEvents[existingIndex],
          ...eventPatch,
        };
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, teamMemberExecutionEvents: updatedEvents },
          },
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMemberExecutionEvents: [eventPatch, ...runtime.teamMemberExecutionEvents].slice(0, 300) },
        },
      };
    });
  },

  setTeamMemberContextCompressionStatus: (sessionId, memberId, runtimeState, summary) => {
    const normalizedMemberId = memberId.trim();
    if (!normalizedMemberId) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const next = { ...runtime.teamMemberContextCompression };
      if (!runtimeState && !summary) {
        delete next[normalizedMemberId];
      } else {
        const existing = next[normalizedMemberId];
        next[normalizedMemberId] = { runtime: runtimeState, summary: summary ?? existing?.summary };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMemberContextCompression: next },
        },
      };
    });
  },

  clearTeamMemberContextCompressionStatus: (sessionId, memberId) => {
    const normalizedMemberId = memberId.trim();
    if (!normalizedMemberId) return;
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime?.teamMemberContextCompression[normalizedMemberId]) {
        return state;
      }
      const next = { ...runtime.teamMemberContextCompression };
      delete next[normalizedMemberId];
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMemberContextCompression: next },
        },
      };
    });
  },

  clearAllTeamMemberContextCompressionStatus: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamMemberContextCompression: {} },
        },
      };
    });
  },

  setTeamHistoryMessages: (sessionId, messages) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, teamHistoryMessages: messages },
        },
      };
    });
  },

  setAvailableModels: (models, activeModel) => {
    set((state) => {
      const defaultModels = models.filter((m) => m.is_default !== false);
      // 过滤为空时回退到全量列表，保证聊天下拉框始终有可选项（例如用户自配模型
      // 均未设为 is_default、且关闭了 Opencode Zen 免费模型时，不至于无模型可选）。
      const chatModels = defaultModels.length > 0 ? defaultModels : models;
      // 优先使用后端返回的 activeModel（默认模型），其次取第一个；状态统一保存真实
      // model_name，alias 只用于界面展示。各会话 runtime 的 selectedModelName 不在这里
      // 重置——单 Agent 和集群会话都是用户自选状态，模型列表刷新（models.updated）不应
      // 冲掉；陈旧失配的名字由 getEffectiveModelName 走 resolveEffectiveModel 兜底解析。
      const matchedModel = activeModel ? chatModels.find((m) => m.model_name === activeModel) : null;
      const selected = (matchedModel ?? chatModels[0])?.model_name ?? null;
      if (selected) {
        try { localStorage.setItem(MODEL_STORAGE_KEY, selected); } catch { /* noop */ }
      }
      // 默认模型变化时，同步未发送的新建会话（'new'，见 newConversationLifecycle 的
      // NEW_CONVERSATION_ID；此处用字面量避免循环依赖）的模型选择：仅当其当前选择
      // 恰为旧默认模型（说明来自默认而非用户手动选择）时才替换为新默认，用户手动
      // 选择的模型不受影响。已创建的真实会话 runtime 依然不在此处重置。
      const runtimes = { ...state.runtimes };
      const pendingNewRuntime = runtimes['new'];
      if (
        pendingNewRuntime
        && selected
        && state.defaultModelName
        && pendingNewRuntime.selectedModelName === state.defaultModelName
      ) {
        runtimes['new'] = { ...pendingNewRuntime, selectedModelName: selected };
      }
      return { availableModels: models, chatAvailableModels: chatModels, defaultModelName: selected, runtimes };
    });
  },

  setSelectedModelName: (sessionId, name) => {
    // 注意：这里只更新当次会话的内存态，不再写 MODEL_STORAGE_KEY——
    // 该 key 专门保存后端配置的默认模型（见 setAvailableModels），
    // 用户手动切模型不应污染"默认模型"这个标记，否则新建会话会继承到"最后用过的模型"。
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return { runtimes: { ...state.runtimes, [sessionId]: { ...runtime, selectedModelName: name } } };
    });
  },
}));
