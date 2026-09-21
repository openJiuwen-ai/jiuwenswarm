// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * 进行中诊断的 SSE 连接控制器（module-level 单例）。
 *
 * 关键修复：诊断的 SSE 流式连接由本模块持有，与 React 组件生命周期完全解耦。
 * 之前连接在组件内由 AbortController 持有，面板因「界面切换」卸载/再挂载（或更轻量的
 * 视图切换，如点开历史报告、切换在线/离线 tab）会触发断连，Gateway（diagnose_http.py）
 * 侦测到客户端断开即发 chat.interrupt 取消服务端诊断任务——表现为「切个页面诊断就失败」。
 *
 * 现在：
 *  - 连接常驻内存，界面切换（SPA 路由卸载再挂载）/ 视图切换不会断连，Gateway 不会取消任务；
 *  - 组件只通过 diagnosisLive 仓库订阅/镜像状态，点击「诊断中」任务即可继续看到流式输出；
 *  - 仅在以下情况主动断连：① 用户显式点「取消」；② 对同一 session 再次发起诊断。
 *    这两种情况本就应中止旧流，Gateway 随之取消服务端任务符合预期。
 */

import { runDiagnosis, type DiagnosisEvent, type DiagnosisRequest } from './diagnosisClient';
import { getLive, upsertLive, clearLive } from './diagnosisLive';

interface RunningTask {
  controller: AbortController;
  promise: Promise<void>;
}

const running = new Map<string, RunningTask>();

function applyEvent(sessionId: string, event: DiagnosisEvent): void {
  switch (event.type) {
    case 'progress': {
      const stage = event.detail ? `${event.stage} · ${event.detail}` : event.stage;
      upsertLive(sessionId, { stage });
      return;
    }
    case 'token': {
      const cur = getLive(sessionId);
      const accumulated = (cur?.accumulated ?? '') + event.text;
      upsertLive(sessionId, { accumulated });
      return;
    }
    case 'final': {
      // chat.final 兜底：仅当尚无任何 token 时才用 final 补一次（与后端语义一致）
      const cur = getLive(sessionId);
      if (!cur || !cur.accumulated) {
        upsertLive(sessionId, { accumulated: event.text });
      }
      return;
    }
    case 'done': {
      upsertLive(sessionId, {
        status: 'done',
        reportPath: event.report_path,
        evidencePath: event.evidence_path,
      });
      return;
    }
    case 'error': {
      upsertLive(sessionId, { status: 'error', error: event.message });
      return;
    }
  }
}

/** 若仍停留在 running（无 done/error 事件，如零 token 裸断），标记为 error，避免「诊断中」残留。 */
function finishAbnormally(sessionId: string, message: string): void {
  const cur = getLive(sessionId);
  if (cur && cur.status === 'running') {
    upsertLive(sessionId, { status: 'error', error: message });
  }
}

/** 该 session 是否仍有运行中的诊断连接。 */
export function isDiagnosisRunning(sessionId: string): boolean {
  return running.has(sessionId);
}

/**
 * 启动某 session 的诊断。若该 session 已有运行中的任务，先中止旧任务再启动
 * （避免同一会话并发两条流）。连接由模块持有，跨组件卸载存活。
 */
export function startDiagnosis(sessionId: string, request: DiagnosisRequest): void {
  const existing = running.get(sessionId);
  if (existing) {
    existing.controller.abort();
    running.delete(sessionId);
  }
  const controller = new AbortController();
  upsertLive(sessionId, {
    sessionId,
    status: 'running',
    accumulated: '',
    stage: 'signal_detect',
    startedAt: Date.now(),
  });
  const task: RunningTask = {
    controller,
    promise: Promise.resolve(),
  };
  task.promise = (async () => {
    try {
      await runDiagnosis(
        sessionId,
        request,
        (event) => applyEvent(sessionId, event),
        controller.signal,
      );
      // 流正常结束但无 done/error 事件（如零 token 裸断）：标记异常，避免「诊断中」残留
      finishAbnormally(sessionId, '诊断流异常中断（未收到任何报告内容），请重试');
    } catch (err) {
      if (controller.signal.aborted) {
        // 用户显式取消：仅当本任务仍是当前运行任务时才清仓库，避免清掉新任务的视图
        if (running.get(sessionId) === task) {
          clearLive(sessionId);
        }
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      finishAbnormally(sessionId, message);
    } finally {
      // 仅当本任务仍是当前运行任务时才摘除登记，避免二次诊断时旧任务清掉新任务的登记
      if (running.get(sessionId) === task) {
        running.delete(sessionId);
      }
    }
  })();
  running.set(sessionId, task);
}

/**
 * 用户显式取消某 session 的诊断：中止 SSE 连接。Gateway 侦测到断开会随之取消
 * 服务端诊断任务（符合预期——这是用户主动行为）。
 */
export function cancelDiagnosis(sessionId: string): void {
  const task = running.get(sessionId);
  if (task) {
    task.controller.abort();
    running.delete(sessionId);
  }
  clearLive(sessionId);
}

/**
 * 取消「所有」运行中的诊断连接（用户点「取消」的兜底口径）。
 *
 * 以「实际在跑」为准，不依赖调用方传入的 sessionId——避免组件 UI 状态
 * （diagMode / sessionId / offlineSessionId）在诊断进行中被改动导致 key 对不上、
 * 单点 cancel 找不到任务而「点了取消却停不下来」的问题。正常场景同一时刻仅一条诊断，
 * 取消全部即等价于取消当前这条。
 */
export function cancelAllDiagnoses(): void {
  const sids = Array.from(running.keys());
  for (const task of running.values()) {
    task.controller.abort();
  }
  running.clear();
  for (const sid of sids) clearLive(sid);
}
