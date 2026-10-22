/**
 * WebUI 渲染实例 store：按 session × slot × package × instance 保存最近一帧。
 */

import { create } from 'zustand';
import {
  WEBUI_SLOT_MODES,
  WebUIRenderErrorFrame,
  WebUIRenderClearFrame,
  WebUIRenderFrame,
  WebUIRenderNode,
  WebUIRenderTarget,
  WebUISlotId,
  webUIInstanceKey,
} from './types';

export interface WebUIInstanceState {
  target: WebUIRenderTarget;
  revision: number;
  mode: 'replace' | 'clear';
  root: WebUIRenderNode | null;
  error: { code: string; message?: string } | null;
}

interface WebUIState {
  instances: Record<string, WebUIInstanceState>;
  applyFrame: (frame: WebUIRenderFrame) => void;
  applyClear: (frame: WebUIRenderClearFrame) => void;
  applyError: (frame: WebUIRenderErrorFrame) => void;
  clearSession: (sessionId: string) => void;
}

export const useWebUIStore = create<WebUIState>((set) => ({
  instances: {},

  applyFrame: (frame) =>
    set((state) => {
      const key = webUIInstanceKey(frame.target);
      const current = state.instances[key];
      if (current && frame.revision <= current.revision) {
        return state;
      }
      const next: WebUIInstanceState = {
        target: frame.target,
        revision: frame.revision,
        mode: frame.mode,
        root: frame.mode === 'clear' ? null : frame.root,
        error: null,
      };
      return { instances: { ...state.instances, [key]: next } };
    }),

  applyError: (frame) =>
    set((state) => {
      const key = webUIInstanceKey(frame.target);
      const current = state.instances[key];
      const error = { code: frame.code, message: frame.message };
      const next: WebUIInstanceState = current
        ? { ...current, error }
        : { target: frame.target, revision: frame.revision, mode: 'replace', root: null, error };
      return { instances: { ...state.instances, [key]: next } };
    }),

  applyClear: (frame) =>
    set((state) => {
      const key = webUIInstanceKey(frame.target);
      const current = state.instances[key];
      if (current && frame.revision < current.revision) {
        return state;
      }
      const next = { ...state.instances };
      delete next[key];
      return { instances: next };
    }),

  clearSession: (sessionId) =>
    set((state) => {
      const remaining: Record<string, WebUIInstanceState> = {};
      Object.entries(state.instances).forEach(([key, instance]) => {
        if (instance.target.session_id !== sessionId) {
          remaining[key] = instance;
        }
      });
      return { instances: remaining };
    }),
}));

/** 取某会话某槽位的可见实例：list 全展示，single 只保留最新一个。 */
export function selectSlotInstances(
  instances: Record<string, WebUIInstanceState>,
  sessionId: string | null,
  slotId: WebUISlotId,
): WebUIInstanceState[] {
  if (!sessionId) {
    return [];
  }
  const matched = Object.values(instances).filter(
    (instance) =>
      instance.target.session_id === sessionId &&
      instance.target.slot_id === slotId &&
      (instance.root !== null || instance.error !== null),
  );
  if (matched.length === 0) {
    return [];
  }
  if (WEBUI_SLOT_MODES[slotId] === 'single') {
    return [matched.reduce((latest, item) => (item.revision >= latest.revision ? item : latest))];
  }
  return matched;
}

/** 取消息级槽位里属于该 request 的单个实例；引用稳定，避免历史消息重复渲染。 */
export function findSlotInstance(
  instances: Record<string, WebUIInstanceState>,
  sessionId: string | null,
  slotId: WebUISlotId,
  instanceKey: string,
): WebUIInstanceState | null {
  if (!sessionId || !instanceKey) {
    return null;
  }
  for (const instance of Object.values(instances)) {
    if (
      instance.target.session_id === sessionId &&
      instance.target.slot_id === slotId &&
      instance.target.instance_key === instanceKey &&
      (instance.root !== null || instance.error !== null)
    ) {
      return instance;
    }
  }
  return null;
}

