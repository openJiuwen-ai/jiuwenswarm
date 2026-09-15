/**
 * 集群模式下的 team 选择器状态。
 *
 * 背景：一个 session 可以同时挂着多个 team（openjiuwen 的 TeamRuntimePool 以
 * `team_name` 为键、按 `current_session_id` 归属），但前端原来的 SessionRuntime
 * 只按 sessionId 存一份 teamMembers / teamTasks，无法表达"当前在看哪个 team"。
 *
 * 这里不做 SessionRuntime 的结构改造（那会波及 ToolPanel / teamArea / 成员头像等
 * 所有读点），而是：
 *   1. 持有 session -> 可选 team 列表（来自 team.list RPC）；
 *   2. 记住每个 session 当前选中的 team_id；
 *   3. 选中变化时用 team.snapshot（带 team_name）把该 team 的成员/任务灌回
 *      SessionRuntime，于是所有既有读点自动展示所选 team 的数据。
 * WS 事件侧由 useWebSocket 按 team_id 过滤（见 teamSelectorStore.getState().selectedTeamId），
 * 避免别的 team 的事件污染当前视图。
 */

import { create } from 'zustand';
import { webClient } from '../services/webClient';
import { useChatStore } from './chatStore';

export type RuntimeTeamState = 'running' | 'paused' | 'pending' | 'configured';

export interface RuntimeTeamInfo {
  team_id: string;
  team_name: string;
  display_name?: string | null;
  state: RuntimeTeamState | string;
  leader_id?: string | null;
  organization_id?: string | null;
  is_leader?: boolean;
  is_owner?: boolean;
  capabilities?: string[];
  source?: string;
}

interface TeamListResponse {
  session_id: string;
  teams: RuntimeTeamInfo[];
  default_team_id?: string | null;
}

export interface TeamSnapshotMember {
  member_id: string;
  name?: string | null;
  status?: string;
  execution_status?: string | null;
  mode?: string;
  role?: string;
  cli_agent?: string | null;
}

export interface TeamSnapshotTask {
  task_id: string;
  team_name?: string;
  title?: string;
  content?: string;
  status?: string;
  assignee?: string | null;
  updated_at?: number | string | null;
}

export interface TeamSnapshotPayload {
  members?: TeamSnapshotMember[];
  tasks?: TeamSnapshotTask[];
  team_id?: string | null;
}

/** 一个 session 的选择器运行态。 */
interface TeamSelectorRuntime {
  teams: RuntimeTeamInfo[];
  /** 当前选中的 team_id；为空表示"跟随后端默认" */
  selectedTeamId: string | null;
  /** 后端给的默认选中项（通常是 running 的第一个） */
  defaultTeamId: string | null;
  loading: boolean;
  /** 最近一次拉取失败的原因，供 UI 提示；成功时清空 */
  error: string | null;
  /** 是否已经完成过一次 team.list，用于区分"还没拉"和"确实没有 team" */
  loaded: boolean;
}

function createEmptyRuntime(): TeamSelectorRuntime {
  return {
    teams: [],
    selectedTeamId: null,
    defaultTeamId: null,
    loading: false,
    error: null,
    loaded: false,
  };
}

/**
 * 刷新 team 列表后要落到哪个 team 上。
 *
 * 用户的显式选择优先：只要那个 team 还在列表里就保留，否则退回后端给的默认项。
 * 默认项同样必须在列表里——选中态指向一个列表里没有的 team_id 时，下拉的"当前值"
 * 和"勾选行"都会悬空，看起来像选择丢了。都不可用时退回列表第一项。
 */
export function resolveSelectedTeamId(
  previousSelectedTeamId: string | null | undefined,
  teams: RuntimeTeamInfo[],
  defaultTeamId: string | null | undefined,
): string | null {
  const listed = (teamId: string | null | undefined): string | null =>
    teamId && teams.some((team) => team.team_id === teamId) ? teamId : null;
  return listed(previousSelectedTeamId) ?? listed(defaultTeamId) ?? teams[0]?.team_id ?? null;
}

/**
 * 这个 team 的成员事件是否该进当前视图。
 *
 * `team_id` 缺失（老后端 / 非 team 事件）时一律放行：宁可多收不可漏收；只有明确
 * 知道当前选了谁、且事件确实属于别的 team 时才丢弃。
 */
export function isEventForSelectedTeam(
  selectedTeamId: string | null | undefined,
  eventTeamId: string | null | undefined,
): boolean {
  if (!eventTeamId || !selectedTeamId) return true;
  return selectedTeamId === eventTeamId;
}

interface TeamSelectorState {
  runtimes: Record<string, TeamSelectorRuntime>;

  getRuntime: (sessionId: string | null | undefined) => TeamSelectorRuntime | undefined;
  removeRuntime: (sessionId: string) => void;

  fetchTeams: (sessionId: string) => Promise<RuntimeTeamInfo[]>;
  selectTeam: (sessionId: string, teamId: string | null) => Promise<void>;
  resetRuntime: (sessionId: string) => void;

  /**
   * 该 team_id 是否属于"当前选中"的视图。
   *
   * 事件过滤用：`team_id` 为空（老后端 / 兼容路径）时一律放行，宁可多收不可漏收；
   * 只在明确知道当前选了谁、且事件确实属于别的 team 时才丢弃。
   */
  isEventForSelectedTeam: (
    sessionId: string | null | undefined,
    teamId: string | null | undefined,
  ) => boolean;
}

export const useTeamSelectorStore = create<TeamSelectorState>((set, get) => ({
  runtimes: {},

  getRuntime: (sessionId) => {
    if (!sessionId) return undefined;
    return get().runtimes[sessionId];
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      if (!(sessionId in state.runtimes)) return state;
      const next = { ...state.runtimes };
      delete next[sessionId];
      return { runtimes: next };
    });
  },

  resetRuntime: (sessionId) => {
    set((state) => {
      const existing = state.runtimes[sessionId];
      if (!existing) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...existing, selectedTeamId: null, defaultTeamId: null },
        },
      };
    });
  },

  fetchTeams: async (sessionId) => {
    if (!sessionId) return [];
    set((state) => ({
      runtimes: {
        ...state.runtimes,
        [sessionId]: { ...(state.runtimes[sessionId] ?? createEmptyRuntime()), loading: true },
      },
    }));

    try {
      const response = await webClient.request<TeamListResponse>(
        'team.list',
        { session_id: sessionId },
        { timeoutMs: 8000 }
      );
      const teams = Array.isArray(response?.teams) ? response.teams : [];
      const defaultTeamId = response?.default_team_id ?? null;

      set((state) => {
        const previous = state.runtimes[sessionId] ?? createEmptyRuntime();
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              teams,
              defaultTeamId,
              selectedTeamId: resolveSelectedTeamId(previous.selectedTeamId, teams, defaultTeamId),
              loading: false,
              error: null,
              loaded: true,
            },
          },
        };
      });
      return teams;
    } catch (error) {
      set((state) => ({
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...(state.runtimes[sessionId] ?? createEmptyRuntime()),
            loading: false,
            loaded: true,
            error: error instanceof Error ? error.message : String(error),
          },
        },
      }));
      return [];
    }
  },

  selectTeam: async (sessionId, teamId) => {
    if (!sessionId) return;
    const previousTeamId = get().runtimes[sessionId]?.selectedTeamId ?? null;
    set((state) => ({
      runtimes: {
        ...state.runtimes,
        [sessionId]: {
          ...(state.runtimes[sessionId] ?? createEmptyRuntime()),
          selectedTeamId: teamId,
        },
      },
    }));

    // 左栏跟着切：选中哪个 team，对话视图就换成哪个 team 的。
    // 这是这个下拉真正要做的事——右侧 Task Pool 只是顺带。
    if (useChatStore.getState().activeSessionId === sessionId) {
      useChatStore.getState().setActiveTeamId(teamId);
    }

    if (!teamId) return;

    try {
      const {
        applyTeamSnapshotToSession,
        clearTeamSnapshotProjection,
      } = await import('./teamSliceApply');
      if (previousTeamId !== teamId) {
        clearTeamSnapshotProjection(sessionId);
      }
      const snapshot = await webClient.request<TeamSnapshotPayload>(
        'team.snapshot',
        { session_id: sessionId, team_name: teamId },
        { timeoutMs: 8000 }
      );
      // A 的慢响应不能覆盖用户随后切换到的 B。
      if (get().runtimes[sessionId]?.selectedTeamId !== teamId) return;
      applyTeamSnapshotToSession(sessionId, teamId, snapshot);
    } catch {
      // 拉不到快照时保留当前 Team 的空投影；不恢复上一个 Team 的数据。
    }
  },

  isEventForSelectedTeam: (sessionId, teamId) => {
    if (!sessionId) return true;
    return isEventForSelectedTeam(get().runtimes[sessionId]?.selectedTeamId, teamId);
  },
}));

export { createEmptyRuntime as createEmptyTeamSelectorRuntime };
