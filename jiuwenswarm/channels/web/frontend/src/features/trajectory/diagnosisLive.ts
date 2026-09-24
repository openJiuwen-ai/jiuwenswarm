// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * 进行中诊断的跨组件挂载存活仓库（module-level 单例）。
 *
 * 诊断流在 DiagnosisHistoryPanel 卸载后仍可在后台继续跑，SSE 的 token 持续写入本仓库；
 * 面板重挂载时再订阅回来，从而实现「切换页面后，进行中的诊断任务仍在左侧列表、
 * 点开继续看到流式输出」。仓库与 React 组件生命周期解耦，仅做纯数据存储 + 订阅通知。
 */

export type LiveStatus = 'running' | 'done' | 'error';

export interface LiveDiagnosis {
  sessionId: string;
  status: LiveStatus;
  /** 已累计的流式报告正文 */
  accumulated: string;
  /** 当前阶段提示（含 detail） */
  stage: string;
  /** status === 'error' 时的错误信息 */
  error?: string;
  /** 诊断启动时间戳（ms），用于跨组件卸载连续计时 */
  startedAt?: number;
  /** status === 'done' 时由后端回传的报告/证据落盘路径 */
  reportPath?: string;
  evidencePath?: string;
}

type Listener = (live: LiveDiagnosis) => void;

const registry = new Map<string, LiveDiagnosis>();
const listeners = new Map<string, Set<Listener>>();

export function getLive(sessionId: string): LiveDiagnosis | undefined {
  return registry.get(sessionId);
}

/** 写入/更新某会话的进行中诊断状态，并通知订阅者。 */
export function upsertLive(sessionId: string, patch: Partial<LiveDiagnosis>): LiveDiagnosis {
  const prev: LiveDiagnosis =
    registry.get(sessionId) ?? { sessionId, status: 'running', accumulated: '', stage: '' };
  const next: LiveDiagnosis = { ...prev, ...patch, sessionId };
  registry.set(sessionId, next);
  const ls = listeners.get(sessionId);
  if (ls) ls.forEach((cb) => cb(next));
  return next;
}

/** 清除某会话的进行中诊断状态（诊断完成/出错后调用，避免游离条目常驻）。
 *
 * 注意：只清注册表条目，**保留 listeners**。订阅者（组件）需在 done/cancel 后
 * 仍能接收「同一 session 的后续诊断」事件——若此处一并删除 listeners，下一次
 * 同 session 诊断的 upsertLive 将找不到订阅者，UI 不再刷新。组件卸载时由
 * subscribeLive 返回的取消函数负责退订。 */
export function clearLive(sessionId: string): void {
  registry.delete(sessionId);
}

/** 订阅某会话的进行中诊断状态变化；返回取消订阅函数。 */
export function subscribeLive(sessionId: string, cb: Listener): () => void {
  let set = listeners.get(sessionId);
  if (!set) {
    set = new Set();
    listeners.set(sessionId, set);
  }
  set.add(cb);
  return () => {
    set!.delete(cb);
    if (set!.size === 0) {
      listeners.delete(sessionId);
    }
  };
}
