/**
 * 任务页「+」菜单「设计」待发送态（按 session 隔离）。
 * 与 Goal armed 同语义：选中后发一条消息即消费，不持续生效。
 */

import { create } from 'zustand';

interface DesignArmedRuntime {
  armed: boolean;
}

function createEmptyRuntime(): DesignArmedRuntime {
  return { armed: false };
}

type DesignArmedStore = {
  runtimes: Record<string, DesignArmedRuntime>;
  ensureRuntime: (sessionId: string) => void;
  isArmed: (sessionId: string) => boolean;
  setArmed: (sessionId: string, armed: boolean) => void;
  /** 若当前 armed 则关掉并返回 true（发送时一次性消费）。 */
  consumeArmed: (sessionId: string) => boolean;
};

export const useDesignArmedStore = create<DesignArmedStore>((set, get) => ({
  runtimes: {},

  ensureRuntime: (sessionId) => {
    if (!sessionId) return;
    if (get().runtimes[sessionId]) return;
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: createEmptyRuntime() },
    }));
  },

  isArmed: (sessionId) => Boolean(sessionId && get().runtimes[sessionId]?.armed),

  setArmed: (sessionId, armed) => {
    if (!sessionId) return;
    set((state) => ({
      runtimes: {
        ...state.runtimes,
        [sessionId]: { ...(state.runtimes[sessionId] ?? createEmptyRuntime()), armed },
      },
    }));
  },

  consumeArmed: (sessionId) => {
    if (!get().isArmed(sessionId)) return false;
    get().setArmed(sessionId, false);
    return true;
  },
}));
