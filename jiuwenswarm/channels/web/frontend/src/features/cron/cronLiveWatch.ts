/**
 * 企业版 HTTP 没有网关 WebSocket。正在看某个 cron_* 窗口时，订一条监听流，
 * 把思考、正文和审批卡送进现有 chat.* 处理。点允许之后的续跑走这次点击自己的
 * chat.send，不从这里再画一遍。
 *
 * 同一 session 稳态只保留一条 live SSE。冷刷新 / 等审批时 history 已就绪则
 * **首连** skip_replay。出字中、本轮 processing、或挂着权限卡时禁止
 * full-replay → skip_replay 中途升级（切连会丢字级 CJK delta）。
 *
 * 连点多个「立即执行」会切走当前 cron_*：切走时不能拆掉仍在跑的 live，
 * 否则收不到 processing_status=false，UI 会永远转圈；跑完后 gateway 还会
 * clear replay，晚订阅也补不回收尾帧。
 *
 * 刷新竞态：live 若在 history 完成前订上，replay 里的 processing=true 会打开
 * acceptContent，把上一轮思考/审批整段灌进空 runtime——表现为「又在验证
 * read_file」+ 权限条。故 idle 冷启动等 history；history 已就绪时 skip_replay。
 *
 * 收尾：processing=false 后短暂保活等 chat.final 覆盖 delta 拼接；有权限卡时不拆。
 */
import { isEnterprise } from '../../edition';
import {
  hasManagerSessionCredentials,
  managerAuthenticatedFetch,
} from '../../auth/manager/authSession';
import { hitlRequestIdsMatch } from '../historyRestore';
import { webClient } from '../../services/webClient';
import { buildRuntimeIdentityHeaders } from '../../services/runtimeScope';
import { useChatStore, type ChatRuntime } from '../../stores/chatStore';
import type { WsEvent } from '../../types';
import { getGatewayHttpBase, getWebTransport } from '../../utils/env';
import { shouldDeferCronLiveSkipReplayUpgrade } from './cronLiveSkipReplayPolicy';

export { shouldDeferCronLiveSkipReplayUpgrade } from './cronLiveSkipReplayPolicy';

type ChatStoreSnapshot = ReturnType<typeof useChatStore.getState>;
/** Content frames that must not overwrite history on an idle refresh. */
const CRON_LIVE_CONTENT_EVENTS = new Set([
  'chat.delta',
  'chat.final',
  'chat.reasoning',
  'chat.tool_call',
  'chat.tool_update',
  'chat.tool_result',
  // 与正文同属本轮内容：idle 刷新勿用 stale replay 把 usage 贴到错气泡上。
  'chat.usage_summary',
]);

type LiveWatchHandle = {
  stop: () => void;
  /** Whether this watch prefers skip_replay=1 once history is ready. */
  skipReplay: boolean;
};
function authorizedFetch(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  if (isEnterprise() && hasManagerSessionCredentials()) {
    return managerAuthenticatedFetch(input, init);
  }
  return fetch(input, init);
}
function parseSseBlock(raw: string): WsEvent | null {
  let eventName = '';
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      // SSE: strip at most one leading space after the colon (do not trim JSON).
      const value = line.slice(5);
      dataLines.push(value.startsWith(' ') ? value.slice(1) : value);
    }
  }
  if (!dataLines.length) return null;
  try {
    const parsed = JSON.parse(dataLines.join('\n')) as {
      event?: string;
      payload?: Record<string, unknown>;
    };
    const payload = parsed.payload;
    if (!payload || typeof payload !== 'object') return null;
    const event = String(parsed.event || eventName || '').trim();
    if (!event) return null;
    const requestId = typeof payload.request_id === 'string' ? payload.request_id : undefined;
    return { type: 'event', event, payload, request_id: requestId };
  } catch {
    return null;
  }
}
function readLiveSeq(payload: Record<string, unknown>): number | null {
  const raw = payload.live_seq;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return raw;
  }
  if (typeof raw === 'string' && raw.trim()) {
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** session_id → current (newest) live watch. */
const activeLiveWatches = new Map<string, LiveWatchHandle>();

/** session_id → last applied live_seq (drop stale/dup frames from overlapping SSE). */
const lastLiveSeqBySession = new Map<string, number>();

/** session_id → 已答过的 ask request_id（防 live replay 把权限卡复活）. */
const dismissedCronAskIds = new Map<string, Set<string>>();

/**
 * processing=false 之后、权威 chat.final 到达之前，短暂保持 shouldWatch，
 * 避免 zustand 订阅立刻拆掉 live、终稿盖不回 delta 错拼。
 */
const CRON_LIVE_FINAL_GRACE_MS = 2500;
const awaitingFinalDeadlineBySession = new Map<string, number>();
const awaitingFinalTimerBySession = new Map<string, ReturnType<typeof setTimeout>>();

export function isCronSessionLiveWatchActive(sessionId: string): boolean {
  return activeLiveWatches.has(sessionId.trim());
}

function isCronLiveAwaitingFinal(sessionId: string): boolean {
  const sid = sessionId.trim();
  const deadline = awaitingFinalDeadlineBySession.get(sid);
  if (deadline == null) return false;
  if (Date.now() >= deadline) {
    awaitingFinalDeadlineBySession.delete(sid);
    return false;
  }
  return true;
}

function clearCronLiveAwaitingFinalTimer(sessionId: string): void {
  const sid = sessionId.trim();
  const timer = awaitingFinalTimerBySession.get(sid);
  if (timer != null) {
    clearTimeout(timer);
    awaitingFinalTimerBySession.delete(sid);
  }
}

/** processing=false 且无权限卡时调用：保活至 final 或宽限到期。 */
export function markCronLiveAwaitingFinal(
  sessionId: string,
  graceMs: number = CRON_LIVE_FINAL_GRACE_MS,
): void {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return;
  const runtime = useChatStore.getState().getRuntime(sid);
  if (runtime?.pendingQuestion) return;
  clearCronLiveAwaitingFinalTimer(sid);
  const deadline = Date.now() + Math.max(0, graceMs);
  awaitingFinalDeadlineBySession.set(sid, deadline);
  const timer = setTimeout(() => {
    awaitingFinalTimerBySession.delete(sid);
    const currentDeadline = awaitingFinalDeadlineBySession.get(sid);
    if (currentDeadline !== deadline) return;
    awaitingFinalDeadlineBySession.delete(sid);
    if (!shouldWatchCronSession(sid)) {
      stopCronSessionLiveWatch(sid);
    }
  }, Math.max(0, graceMs) + 25);
  awaitingFinalTimerBySession.set(sid, timer);
}

/**
 * 权威 chat.final 已写入气泡后调用：结束保活并拆 live。
 * 仅在 markCronLiveAwaitingFinal 窗口内生效——避免 HITL 前铺垫 final 误拆 live。
 * 仍挂权限卡时不拆。
 */
export function noteCronLiveContentFinal(sessionId: string): void {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return;
  if (!isCronLiveAwaitingFinal(sid)) return;
  const runtime = useChatStore.getState().getRuntime(sid);
  if (runtime?.pendingQuestion) return;
  clearCronLiveAwaitingFinalTimer(sid);
  awaitingFinalDeadlineBySession.delete(sid);
  stopCronSessionLiveWatch(sid);
}

export function stopAllCronSessionLiveWatches(): void {
  for (const sid of [...activeLiveWatches.keys()]) {
    stopCronSessionLiveWatch(sid);
  }
}

/** 用户已提交权限/审批后调用，后续 live/history 回放同 request_id 不再挂卡. */
export function dismissCronAskUser(sessionId: string, requestId: string): void {
  const sid = sessionId.trim();
  const rid = requestId.trim();
  if (!sid.startsWith('cron_') || !rid) return;
  let bucket = dismissedCronAskIds.get(sid);
  if (!bucket) {
    bucket = new Set();
    dismissedCronAskIds.set(sid, bucket);
  }
  bucket.add(rid);
}

export function isCronAskUserDismissed(sessionId: string, requestId: string): boolean {
  const rid = requestId.trim();
  if (!rid) return false;
  const bucket = dismissedCronAskIds.get(sessionId.trim());
  if (!bucket || bucket.size === 0) return false;
  if (bucket.has(rid)) return true;
  for (const dismissed of bucket) {
    if (hitlRequestIdsMatch(dismissed, rid)) return true;
  }
  return false;
}
function cronRuntimeHasAssistantContent(sessionId: string): boolean {
  const messages = useChatStore.getState().getRuntime(sessionId)?.messages ?? [];
  return messages.some(
    (m: { role?: string; content?: string }) =>
      m.role === 'assistant' && String(m.content || '').trim(),
  );
}

/** 末条非 system 是 user → 本轮续聊尚未收到助手终态，不能当 stale 收口。 */
function cronRuntimeHasOpenUserTurn(sessionId: string): boolean {
  const messages = useChatStore.getState().getRuntime(sessionId)?.messages ?? [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const role = messages[i]?.role;
    if (role === 'user') return true;
    if (role === 'assistant') return false;
  }
  return false;
}
function isCronPermissionPromptSource(source: string | undefined): boolean {
  const src = String(source || '').trim();
  // 无 source 时授权条仍可能按选项形态展示；收口时一并清掉更稳妥。
  return !src || src === 'permission_interrupt' || src === 'confirm_interrupt';
}

/**
 * Gateway 流以 HITL 暂停结束时仍可能误补 processing=false。
 * 若 cron 上仍挂着权限/确认卡，应保留进行中，不能当「已完成」清卡。
 */
export function shouldRetainCronPermissionOnProcessingFalse(
  sessionId: string,
  pending: { source?: string } | null | undefined,
): boolean {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return false;
  if (!pending) return false;
  return isCronPermissionPromptSource(pending.source);
}

/**
 * cron 等审批时调度器不会推 processing=false。
 * 收到 false / 终态 final 后若仍挂着权限卡，视为过期残留并清掉。
 * 例外：见 shouldRetainCronPermissionOnProcessingFalse（续跑二次审批）。
 */
export function clearStaleCronPermissionPrompt(sessionId: string): boolean {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return false;
  const chat = useChatStore.getState();
  const pending = chat.getRuntime(sid)?.pendingQuestion;
  if (!pending || !isCronPermissionPromptSource(pending.source)) {
    return false;
  }
  const rid = String(pending.request_id || '').trim();
  if (rid) {
    dismissCronAskUser(sid, rid);
  }
  chat.setPendingQuestion(sid, null);
  return true;
}

/**
 * live / chat.send SSE 共用：已结束会话上的 stale permission ask 不再写入 pending。
 * 真正等审批时 isProcessing 仍为 true，会放行。
 */
export function shouldAcceptCronAskUserQuestion(
  sessionId: string,
  question: { request_id?: string; source?: string },
): boolean {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return true;
  const rid = String(question.request_id || '').trim();
  if (rid && isCronAskUserDismissed(sid, rid)) {
    return false;
  }
  if (!isCronPermissionPromptSource(question.source)) {
    return true;
  }
  const runtime = useChatStore.getState().getRuntime(sid);
  if (
    !runtime?.isProcessing &&
    !runtime?.pendingQuestion &&
    cronRuntimeHasAssistantContent(sid)
  ) {
    if (rid) {
      dismissCronAskUser(sid, rid);
    }
    return false;
  }
  return true;
}

export function stopCronSessionLiveWatch(sessionId: string): void {
  const sid = sessionId.trim();
  if (!sid) return;
  clearCronLiveAwaitingFinalTimer(sid);
  awaitingFinalDeadlineBySession.delete(sid);
  activeLiveWatches.get(sid)?.stop();
}

/**
 * 立即执行切走后再切回、或 job sync 发现任务已终态时，
 * 清掉残留的 isProcessing / 子任务转圈，并拆掉 live。
 *
 * 不能因「会话里曾经有过助手消息」就收口正在跑的续聊：否则会停掉
 * chat.send 流并触发 history 整表覆盖，把内存里尚未落盘的正文抹掉。
 */
export function settleCronSessionRuntime(sessionId: string): boolean {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return false;
  const chat = useChatStore.getState();
  const runtime = chat.getRuntime(sid);
  if (!runtime) {
    stopCronSessionLiveWatch(sid);
    return false;
  }
  if (runtime.pendingQuestion) return false;
  // 本轮仍在出字 / 等 final：绝不强制收口。
  if (runtime.currentStreamId) return false;
  if (isCronLiveAwaitingFinal(sid)) return false;
  if (runtime.isProcessing && isCronSessionLiveWatchActive(sid)) return false;
  // 续聊已发 user、尚无对应 assistant：processing 也可能短暂无 streamId。
  if (runtime.isProcessing && cronRuntimeHasOpenUserTurn(sid)) return false;
  // 首轮尚无助手正文：仍可能在等 ask 帧或 live replay。
  if (
    !cronRuntimeHasAssistantContent(sid) &&
    (runtime.isProcessing || isCronSessionLiveWatchActive(sid))
  ) {
    return false;
  }
  const hasSubtasks = (runtime.activeSubtasks?.size ?? 0) > 0;
  const busy = Boolean(runtime.isProcessing || runtime.currentStreamId || hasSubtasks);
  if (!busy) {
    stopCronSessionLiveWatch(sid);
    return false;
  }
  chat.setProcessing(sid, false);
  chat.setThinking(sid, false);
  chat.clearSubtasks(sid);
  chat.stopStreaming(sid);
  stopCronSessionLiveWatch(sid);
  return true;
}

/**
 * 是否应订 live：
 * - 本页已在跑 / 已挂审批 → 订（run-now 可带 replay）
 * - 冷刷新：等 history 完成；仅当 history 回放出仍在等审批时再订，且 skip_replay
 * - processing=false 后短暂等 chat.final → 仍订（见 markCronLiveAwaitingFinal）
 * - 已结束且空闲 → 不订，避免 stale replay 重建上一轮
 */
export function shouldWatchCronSession(
  sessionId: string,
  runtime?: ChatRuntime | null,
): boolean {
  const sid = sessionId.trim();
  if (!sid.startsWith('cron_')) return false;
  const rt = runtime ?? useChatStore.getState().getRuntime(sid);
  if (!rt) return false;
  if (rt.isProcessing || rt.pendingQuestion) return true;
  if (isCronLiveAwaitingFinal(sid)) return true;
  return false;
}

/**
 * history 已就绪且当前 watch 仍是 full-replay：先开 skip_replay 新连，再关旧。
 * 出字中 / processing / 权限卡期间禁止升级（A'）；冷刷新等审批应走首连 skip_replay。
 */
export function upgradeCronLiveWatchToSkipReplay(sessionId: string): void {
  const sid = sessionId.trim();
  if (getWebTransport() !== 'http' || !sid.startsWith('cron_')) return;
  if (!shouldWatchCronSession(sid)) return;
  const runtime = useChatStore.getState().getRuntime(sid);
  if (shouldDeferCronLiveSkipReplayUpgrade(runtime)) return;
  const current = activeLiveWatches.get(sid);
  if (!current || current.skipReplay) return;
  startCronSessionLiveWatch(sid, {
    skipReplay: true,
    previousStop: current.stop,
  });
}

/** 已有同 session 的 live 则复用，避免切会话时重启 SSE 丢帧. */
export function ensureCronSessionLiveWatch(sessionId: string): () => void {
  const sid = sessionId.trim();
  if (getWebTransport() !== 'http' || !sid.startsWith('cron_')) {
    return () => undefined;
  }
  if (!shouldWatchCronSession(sid)) {
    stopCronSessionLiveWatch(sid);
    return () => undefined;
  }
  const existing = activeLiveWatches.get(sid);
  if (existing) {
    const wantSkip = Boolean(useChatStore.getState().getRuntime(sid)?.historyPagerMeta);
    const runtime = useChatStore.getState().getRuntime(sid);
    if (
      wantSkip &&
      !existing.skipReplay &&
      !shouldDeferCronLiveSkipReplayUpgrade(runtime)
    ) {
      upgradeCronLiveWatchToSkipReplay(sid);
      return activeLiveWatches.get(sid)?.stop ?? existing.stop;
    }
    return existing.stop;
  }
  return startCronSessionLiveWatch(sid).stop;
}

export function startCronSessionLiveWatch(
  sessionId: string,
  options?: {
    skipReplay?: boolean;
    /** Open-new-then-close-old: keep this connection until the new one is ready. */
    previousStop?: () => void;
  },
): LiveWatchHandle {
  const sid = sessionId.trim();
  const noop: LiveWatchHandle = {
    stop: () => undefined,
    skipReplay: Boolean(options?.skipReplay),
  };
  if (getWebTransport() !== 'http' || !sid.startsWith('cron_')) {
    return noop;
  }
  let previousStop = options?.previousStop;
  // Immediate replace (no overlap): tear down any prior watch first.
  if (!previousStop) {
    activeLiveWatches.get(sid)?.stop();
    activeLiveWatches.delete(sid);
  }
  const controller = new AbortController();
  let stopped = false;
  // Only accept content after processing_status=true on this connection.
  // Stale replay on an idle follow-up session must not rebuild the first run.
  let acceptContent = false;
  const preferSkipReplay =
    options?.skipReplay ?? Boolean(useChatStore.getState().getRuntime(sid)?.historyPagerMeta);
  const stop = () => {
    if (stopped) return;
    stopped = true;
    controller.abort();
    const current = activeLiveWatches.get(sid);
    if (current?.stop === stop) {
      activeLiveWatches.delete(sid);
    }
    // Full teardown only when no active watch remains (overlap keeps seq).
    if (!activeLiveWatches.has(sid)) {
      lastLiveSeqBySession.delete(sid);
    }
  };
  const handle: LiveWatchHandle = { stop, skipReplay: preferSkipReplay };
  activeLiveWatches.set(sid, handle);
  const shouldInject = (event: WsEvent): boolean => {
    const runtime = useChatStore.getState().getRuntime(sid);
    // 冷刷新：history 未就绪且本页未在跑 → 丢掉一切 live（含 processing=true），
    // 否则 replay 会先把 isProcessing/acceptContent 打开，整段上一轮灌进来。
    const coldIdle =
      !runtime?.isProcessing &&
      !runtime?.pendingQuestion &&
      (Boolean(runtime?.isLoadingHistory) || !runtime?.historyPagerMeta);
    if (coldIdle) {
      return false;
    }
    if (event.event === 'chat.processing_status') {
      const on = Boolean(event.payload.is_processing);
      // 已结束会话上的 stale processing=true，勿重新打开 acceptContent / 转圈。
      if (
        on &&
        !runtime?.isProcessing &&
        !runtime?.pendingQuestion &&
        cronRuntimeHasAssistantContent(sid)
      ) {
        return false;
      }
      acceptContent = on;
      return true;
    }
    if (event.event === 'chat.ask_user_question') {
      if (
        !shouldAcceptCronAskUserQuestion(sid, {
          request_id: String(event.payload.request_id || ''),
          source: typeof event.payload.source === 'string' ? event.payload.source : undefined,
        })
      ) {
        return false;
      }
      acceptContent = true;
      return true;
    }
    if (!CRON_LIVE_CONTENT_EVENTS.has(event.event)) {
      return true;
    }
    // Active run (this connection saw processing=true, or UI already in-flight).
    if (acceptContent || runtime?.isProcessing || runtime?.pendingQuestion) {
      return true;
    }
    // Idle follow-up window: drop leftover bus replay so refresh cannot rebuild
    // the first cron-run answer on top of history.
    return false;
  };
  const maybeStopAfterSettled = () => {
    queueMicrotask(() => {
      if (stopped) return;
      const current = activeLiveWatches.get(sid);
      if (current?.stop !== stop) return;
      const runtime = useChatStore.getState().getRuntime(sid);
      // 护栏：有权限卡绝不拆。
      if (runtime?.pendingQuestion) return;
      if (runtime?.isProcessing) return;
      // B'：仍在等 chat.final 覆盖正文时不拆；宽限到期或 noteCronLiveContentFinal 再拆。
      if (isCronLiveAwaitingFinal(sid)) return;
      stop();
    });
  };
  const run = async () => {
    while (!stopped) {
      try {
        acceptContent = false;
        // history 已落盘后再订：跳过 bus 缓冲，只收实时帧。
        const rtAtConnect = useChatStore.getState().getRuntime(sid);
        const skipReplay =
          preferSkipReplay || Boolean(rtAtConnect?.historyPagerMeta);
        // Keep handle metadata in sync if history landed mid-reconnect loop.
        handle.skipReplay = skipReplay;
        const liveUrl =
          `${getGatewayHttpBase()}/sessions/${encodeURIComponent(sid)}/live` +
          (skipReplay ? '?skip_replay=1' : '');
        const response = await authorizedFetch(liveUrl, {
          method: 'GET',
          headers: {
            ...buildRuntimeIdentityHeaders(`cron-live-${sid}`, { session_id: sid }),
            Accept: 'text/event-stream',
          },
          cache: 'no-store',
          signal: controller.signal,
        });
        if (stopped) return;
        // Superseded by a newer watch (overlap upgrade) before we connected.
        if (activeLiveWatches.get(sid)?.stop !== stop) {
          stop();
          return;
        }
        if (!response.ok || !response.body) {
          throw new Error(`cron live watch failed (${response.status})`);
        }
        // New SSE ready: close the previous full-replay connection (open-new-then-close-old).
        if (previousStop) {
          const retiring = previousStop;
          previousStop = undefined;
          retiring();
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        // Use own `stopped` flag only — map entry may briefly point at us while
        // an overlap peer also injects; live_seq dedupes. When we are retired,
        // stop() sets stopped=true.
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() ?? '';
          for (const block of blocks) {
            if (stopped) return;
            const event = parseSseBlock(block);
            if (!event) continue;
            const eventSession = String(
              event.payload.session_id || event.payload.exec_session_id || '',
            );
            if (eventSession && eventSession !== sid) continue;
            const liveSeq = readLiveSeq(event.payload);
            if (liveSeq != null) {
              const last = lastLiveSeqBySession.get(sid) ?? 0;
              // Overlapping SSE (reconnect / StrictMode / skip_replay upgrade)
              // can deliver older frames after newer ones; drop to avoid CJK swap.
              if (liveSeq <= last) continue;
              lastLiveSeqBySession.set(sid, liveSeq);
            }
            if (!shouldInject(event)) continue;
            // 先于 inject 打 awaiting-final：否则 setProcessing(false) 的同步订阅会立刻拆 live。
            if (
              event.event === 'chat.processing_status' &&
              !Boolean(event.payload.is_processing)
            ) {
              const rtBefore = useChatStore.getState().getRuntime(sid);
              if (!rtBefore?.pendingQuestion) {
                markCronLiveAwaitingFinal(sid);
              }
            }
            webClient.injectEvent(event);
            if (
              event.event === 'chat.processing_status' &&
              !Boolean(event.payload.is_processing)
            ) {
              // final 常在 false 之前已写入；气泡已非 streaming 时可立刻收口。
              const rtAfter = useChatStore.getState().getRuntime(sid);
              if (
                !rtAfter?.pendingQuestion &&
                !rtAfter?.isProcessing &&
                !rtAfter?.currentStreamId
              ) {
                noteCronLiveContentFinal(sid);
              } else {
                maybeStopAfterSettled();
              }
            }
            if (event.event === 'chat.final') {
              // 仅当已进入 awaiting-final（false 已到）时收口；铺垫 final 不拆线。
              noteCronLiveContentFinal(sid);
            }
          }
        }
      } catch {
        if (stopped || controller.signal.aborted) {
          return;
        }
      }
      if (stopped) return;
      // Only the newest watch may reconnect after drop.
      if (activeLiveWatches.get(sid)?.stop !== stop) {
        stop();
        return;
      }
      await new Promise((resolve) => {
        window.setTimeout(resolve, 2000);
      });
    }
  };
  void run();
  return handle;
}

/**
 * 企业版 HTTP：仅为「仍在跑 / 仍挂审批」的 cron 维持 live。
 * 已结束的空闲 cron 不订，避免刷新时 stale replay 重建上一轮。
 * history 回放出 pendingQuestion 后会再 ensure（skip_replay）。
 */
export function bindCronLiveWatchLifecycle(): () => void {
  if (getWebTransport() !== 'http') {
    return () => undefined;
  }
  const syncWatches = () => {
    const state = useChatStore.getState();
    for (const sid of Object.keys(state.runtimes)) {
      if (!sid.startsWith('cron_')) continue;
      if (shouldWatchCronSession(sid, state.runtimes[sid])) {
        ensureCronSessionLiveWatch(sid);
      } else {
        stopCronSessionLiveWatch(sid);
      }
    }
    const active = (state.activeSessionId || '').trim();
    if (active.startsWith('cron_') && shouldWatchCronSession(active)) {
      ensureCronSessionLiveWatch(active);
    }
  };
  syncWatches();
  const unsub = useChatStore.subscribe((state: ChatStoreSnapshot, prev: ChatStoreSnapshot) => {
    const active = (state.activeSessionId || '').trim();
    const prevActive = (prev.activeSessionId || '').trim();
    if (active.startsWith('cron_') && active !== prevActive) {
      if (shouldWatchCronSession(active)) {
        ensureCronSessionLiveWatch(active);
      } else {
        stopCronSessionLiveWatch(active);
      }
      if (prevActive.startsWith('cron_') && !shouldWatchCronSession(prevActive)) {
        stopCronSessionLiveWatch(prevActive);
      }
    }
    for (const sid of Object.keys(state.runtimes)) {
      if (!sid.startsWith('cron_')) continue;
      const runtime: ChatRuntime | undefined = state.runtimes[sid];
      const prevRt: ChatRuntime | undefined = prev.runtimes[sid];
      const nowWatch = shouldWatchCronSession(sid, runtime);
      const wasWatch = shouldWatchCronSession(sid, prevRt);
      if (nowWatch && !wasWatch) {
        ensureCronSessionLiveWatch(sid);
      }
      if (wasWatch && !nowWatch) {
        stopCronSessionLiveWatch(sid);
      }
      // history 刚落盘：full-replay → skip_replay（先开新再关旧）。
      if (
        nowWatch &&
        wasWatch &&
        !prevRt?.historyPagerMeta &&
        runtime?.historyPagerMeta
      ) {
        upgradeCronLiveWatchToSkipReplay(sid);
        if (!isCronSessionLiveWatchActive(sid)) {
          ensureCronSessionLiveWatch(sid);
        }
      }
    }
  });
  return () => {
    unsub();
    stopAllCronSessionLiveWatches();
  };
}
