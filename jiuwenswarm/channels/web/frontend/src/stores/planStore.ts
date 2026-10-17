/**
 * Plan 模式开关（多 session 版本）。
 *
 * 与 Goal 的 armed 不同：Plan 是一个**持续**开关。打开后该会话的每条消息都用
 * `agent.plan` 发送，直到用户主动关闭，或后端在计划执行后推送
 * `plan.mode_exited`。Plan 只对单 agent 开放，集群不提供入口。
 *
 * 这里刻意保持最小：只有开关本身，不做 GoalBar 式的常驻进度、计时与历史恢复。
 */

import { create } from 'zustand';

interface PlanRuntime {
  /** Plan 开关是否打开。打开后持续生效。 */
  active: boolean;
  /**
   * 用户刚手动打开开关，下一条出站的 Plan 消息要带 `plan_entry_source`。
   *
   * 后端有两道闸门防止"计划已执行完、开关却没复位"把会话重新拖回 Plan，只有带
   * 这个一次性标记的请求才放行。开关关闭时清零；成功发出一条 Plan 消息后消费。
   */
  pendingExplicitEntry: boolean;
  pendingEntrySource: 'plan_toggle' | 'slash_command' | null;
}

function createEmptyRuntime(): PlanRuntime {
  return { active: false, pendingExplicitEntry: false, pendingEntrySource: null };
}

interface SetActiveOptions {
  /** 是否来自用户手动打开开关（而不是后端事件 / 内部同步）。 */
  explicitEntry?: boolean;
  entrySource?: 'plan_toggle' | 'slash_command';
}

interface PlanState {
  runtimes: Record<string, PlanRuntime>;

  ensureRuntime: (sessionId: string) => void;
  removeRuntime: (sessionId: string) => void;
  getRuntime: (sessionId: string) => PlanRuntime | undefined;
  isActive: (sessionId: string) => boolean;
  setActive: (sessionId: string, active: boolean, options?: SetActiveOptions) => void;
  toggle: (sessionId: string) => void;
  /** 是否有待发送的"显式进入 Plan"标记（不消费）。 */
  hasPendingExplicitEntry: (sessionId: string) => boolean;
  getPendingEntrySource: (sessionId: string) => 'plan_toggle' | 'slash_command' | null;
  /** 消费掉该标记。请求成功发出后调用，失败时保留以便重试。 */
  consumeExplicitEntry: (sessionId: string) => void;
}

export const usePlanStore = create<PlanState>((set, get) => ({
  runtimes: {},

  ensureRuntime: (sessionId) => {
    if (!sessionId) return;
    if (get().runtimes[sessionId]) return;
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: createEmptyRuntime() },
    }));
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      if (!state.runtimes[sessionId]) return state;
      const runtimes = { ...state.runtimes };
      delete runtimes[sessionId];
      return { runtimes };
    });
  },

  getRuntime: (sessionId) => get().runtimes[sessionId],

  isActive: (sessionId) => Boolean(sessionId && get().runtimes[sessionId]?.active),

  setActive: (sessionId, active, options) => {
    if (!sessionId) return;
    set((state) => {
      const current = state.runtimes[sessionId] ?? createEmptyRuntime();
      const pendingExplicitEntry = active
        ? current.pendingExplicitEntry || Boolean(options?.explicitEntry)
        : false;
      const pendingEntrySource = active
        ? current.pendingEntrySource || (
          options?.explicitEntry ? options.entrySource ?? 'plan_toggle' : null
        )
        : null;
      if (
        current.active === active &&
        current.pendingExplicitEntry === pendingExplicitEntry &&
        current.pendingEntrySource === pendingEntrySource
      ) {
        return state;
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...current, active, pendingExplicitEntry, pendingEntrySource },
        },
      };
    });
  },

  toggle: (sessionId) => {
    if (!sessionId) return;
    const current = get().runtimes[sessionId]?.active ?? false;
    get().setActive(sessionId, !current, { explicitEntry: true });
  },

  hasPendingExplicitEntry: (sessionId) =>
    Boolean(sessionId && get().runtimes[sessionId]?.pendingExplicitEntry),

  getPendingEntrySource: (sessionId) =>
    (sessionId && get().runtimes[sessionId]?.pendingEntrySource) || null,

  consumeExplicitEntry: (sessionId) => {
    if (!sessionId) return;
    set((state) => {
      const current = state.runtimes[sessionId];
      if (!current?.pendingExplicitEntry) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...current,
            pendingExplicitEntry: false,
            pendingEntrySource: null,
          },
        },
      };
    });
  },
}));
