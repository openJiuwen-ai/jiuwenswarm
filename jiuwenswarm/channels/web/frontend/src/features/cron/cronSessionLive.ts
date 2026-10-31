/**
 * 企业版「立即执行」会话页的结果监听。
 *
 * 个人版靠长连接收结果；企业版是普通 HTTP，没有这条长连接。
 * 这里盯着 Gateway 的 /sessions/{id}/live，把过程写进当前会话。
 * 加载历史不代表执行已结束，后续直播帧继续交给现有聊天处理；
 * 历史（或已完成的回复）已经覆盖的回放帧不再重复上屏。
 *
 * 切走页面时不能停订：后台继续盯 lastRunSessionId，直到 processing 结束，
 * 否则侧边栏会一直转圈，蓝点也标不上。
 */
import { isEnterprise } from '../../edition';
import { consumeSseBuffer, sseFrameToWsEvent } from '../../services/webHttpClient';
import {
  getManagerAccessToken,
  hasManagerSessionCredentials,
  managerAuthenticatedFetch,
} from '../../auth/manager/authSession';
import { buildRuntimeIdentityHeaders } from '../../services/runtimeScope';
import { webClient } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { useCronStore } from '../../stores/cronStore';
import { normalizeFinalContent } from '../../utils/finalContent';
import { getGatewayHttpBase, getWebTransport } from '../../utils/env';

/** 执行过程里要画到会话页上的帧。和网关放进直播的那一组一致。 */
const CRON_LIVE_EVENTS = new Set([
  'chat.reasoning',
  'chat.delta',
  'chat.tool_call',
  'chat.tool_update',
  'chat.tool_result',
  'chat.processing_status',
  'chat.ask_user_question',
  'chat.usage_summary',
  'chat.final',
  'chat.error',
  'cron.run.updated',
]);

function isCronExecutionSession(sessionId: string): boolean {
  return sessionId.startsWith('cron_') || sessionId.startsWith('__cron__');
}

/** 历史（或已完成的回复）已覆盖回放时，这些帧仍然要画：提问卡片、错误、执行状态更新。 */
const CRON_LIVE_KEEP_AFTER_HISTORY = new Set(['chat.ask_user_question', 'chat.error', 'cron.run.updated']);

function authenticatedFetch(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  if (isEnterprise() && hasManagerSessionCredentials()) {
    const headers = new Headers(init.headers);
    const authorization = headers.get('Authorization');
    const accessToken = getManagerAccessToken();
    if (authorization && authorization !== (accessToken ? `Bearer ${accessToken}` : null)) {
      return fetch(input, init);
    }
    return managerAuthenticatedFetch(input, init);
  }
  return fetch(input, init);
}

/** Receive online run hints even when no execution session is open. */
export function watchCronRunUpdates(onDisconnect: () => void): () => void {
  if (getWebTransport() !== 'http') return () => undefined;
  const controller = new AbortController();
  const run = async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await authenticatedFetch(`${getGatewayHttpBase()}/cron/runs/live`, {
          method: 'GET', cache: 'no-store', signal: controller.signal,
          headers: { ...buildRuntimeIdentityHeaders('cron-runs-live', {}), Accept: 'text/event-stream' },
        });
        if (!response.ok || !response.body) throw new Error('cron notification subscription failed');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        try {
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const consumed = consumeSseBuffer(buffer);
            buffer = consumed.rest;
            for (const frame of consumed.frames) {
              const event = sseFrameToWsEvent(frame);
              if (event?.event === 'cron.run.updated') webClient.injectEvent(event);
            }
          }
        } finally {
          await reader.cancel().catch(() => undefined);
        }
      } catch {
        if (controller.signal.aborted) return;
      }
      if (controller.signal.aborted) return;
      onDisconnect();
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
    }
  };
  void run();
  return () => controller.abort();
}

function lookupJobIdForSession(sessionId: string): string {
  const map = useCronStore.getState().lastRunSessionId;
  for (const [jobId, sid] of Object.entries(map)) {
    if (sid === sessionId) return jobId;
  }
  return '';
}

/**
 * 只在「整趟收尾」帧上补 job_id（processing=false / error）。
 * 不要补到中间段 chat.final 上——企业版一段回复结束也会发 final，
 * 补上 job_id 会被误标成未读蓝点，而会话其实还在跑。
 */
function ensureCronMetaOnSettle(
  sessionId: string,
  eventName: string,
  payload: Record<string, unknown>,
): void {
  const isSettle =
    eventName === 'chat.error' ||
    (eventName === 'chat.processing_status' && payload.is_processing === false);
  if (!isSettle) return;
  const existing = payload.cron;
  if (
    existing &&
    typeof existing === 'object' &&
    typeof (existing as { job_id?: unknown }).job_id === 'string' &&
    String((existing as { job_id: string }).job_id).trim()
  ) {
    return;
  }
  const jobId = lookupJobIdForSession(sessionId);
  if (!jobId) return;
  const base =
    existing && typeof existing === 'object'
      ? { ...(existing as Record<string, unknown>) }
      : {};
  payload.cron = {
    ...base,
    job_id: jobId,
    exec_session_id: sessionId,
    is_placeholder: false,
  };
}

function isCronLiveSettledEvent(eventName: string, payload: Record<string, unknown>): boolean {
  if (eventName === 'chat.error') return true;
  return eventName === 'chat.processing_status' && payload.is_processing === false;
}

export type WatchCronExecutionOptions = {
  /**
   * 后台盯梢：收到 processing=false / error 后停订，避免切走页面后无限重连。
   * 当前正在看的会话不要开：人还在页上时保持重连。
   */
  stopOnSettled?: boolean;
  /** develop：统一会话未必是 cron_ 前缀，靠 cron_id 元数据放行。 */
  hasCronMetadata?: boolean;
};

/**
 * 企业版 HTTP 下，盯住定时任务执行会话。
 * 思考、逐字正文、工具调用和终稿都交给现有的聊天处理去画。
 * 历史只抑制已覆盖的首次回放；新输出、提问和执行结束状态继续转发。
 * 其它传输、其它会话直接返回空的停止函数。
 */
export function watchCronExecutionSession(
  sessionId: string,
  options?: WatchCronExecutionOptions,
): () => void {
  const hasCronMetadata = Boolean(options?.hasCronMetadata);
  if (getWebTransport() !== 'http' || (!hasCronMetadata && !isCronExecutionSession(sessionId))) {
    return () => undefined;
  }
  const stopOnSettled = Boolean(options?.stopOnSettled);
  const controller = new AbortController();
  let stopped = false;
  const seenSequences = new Set<number>();
  let lastLiveSequence = 0;

  const run = async () => {
    while (!stopped) {
      try {
        const resuming = seenSequences.size > 0;
        const liveUrl = `${getGatewayHttpBase()}/sessions/${encodeURIComponent(sessionId)}/live`;
        const response = await authenticatedFetch(liveUrl, {
          method: 'GET',
          headers: {
            ...buildRuntimeIdentityHeaders(`cron-live-${sessionId}`, { session_id: sessionId }),
            Accept: 'text/event-stream',
          },
          cache: 'no-store',
          signal: controller.signal,
        });
        if (stopped) return;
        if (!response.ok || !response.body) {
          throw new Error(`cron live watch failed (${response.status})`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const consumed = consumeSseBuffer(buffer);
          buffer = consumed.rest;
          let settled = false;
          for (const frame of consumed.frames) {
            const event = sseFrameToWsEvent(frame);
            if (!event || (
              !CRON_LIVE_EVENTS.has(event.event) && !event.event.startsWith('team.') && !event.event.startsWith('workflow.')
            )) continue;
            const cron = event.payload.cron;
            const isPlaceholder = Boolean(
              cron && typeof cron === 'object' && (cron as { is_placeholder?: boolean }).is_placeholder,
            );
            if (isPlaceholder) continue;
            const sequence = event.payload.live_seq;
            if (typeof sequence === 'number') {
              if (sequence <= lastLiveSequence) continue;
              lastLiveSequence = sequence;
            }
            if (typeof sequence === 'number' && Number.isSafeInteger(sequence) && sequence > 0) {
              if (seenSequences.has(sequence)) continue;
              seenSequences.add(sequence);
              if (seenSequences.size > 4096) seenSequences.delete(seenSequences.values().next().value!);
            }
            const runtime = useChatStore.getState().getRuntime(sessionId);
            // A restored timeline already contains the initial backlog. New
            // frames and unseen reconnect frames must still reach the renderer.
            const replayingInitialBacklog = event.payload.live_replay === true
              && Boolean(runtime?.historyPagerMeta) && !resuming;
            // Only frames that identify themselves with the completed reply's
            // request (or output phase) are treated as duplicates; untagged
            // frames are new output and must pass.
            const requestKey = event.payload.request_id || event.payload.output_phase_id || "";
            const hasCompletedReply = Boolean(requestKey) && Boolean(runtime?.historyPagerMeta)
              && Boolean(runtime?.messages.some((message) =>
                message.role === 'assistant' && !message.isStreaming && !message.toolCall && !message.toolResult
                && Boolean(message.content.trim())
                && (event.payload.request_id
                  ? message.outputOrder?.requestId === event.payload.request_id
                  : message.outputPhaseId === event.payload.output_phase_id),
              ));
            if (replayingInitialBacklog || hasCompletedReply) {
              if (event.event === 'chat.final' && replayingInitialBacklog) {
                const content = normalizeFinalContent(event.payload).trim();
                if (content && runtime?.messages.some(message =>
                  message.role === 'assistant' && message.content.trim() === content,
                )) continue;
              } else if (event.event === 'chat.processing_status') {
                if (event.payload.is_processing !== false) continue;
              } else if (!CRON_LIVE_KEEP_AFTER_HISTORY.has(event.event)) {
                continue;
              }
            }
            if (!event.payload.session_id) {
              event.payload.session_id = sessionId;
            }
            ensureCronMetaOnSettle(sessionId, event.event, event.payload);
            webClient.injectEvent(event);
            if (stopOnSettled && isCronLiveSettledEvent(event.event, event.payload)) {
              settled = true;
            }
          }
          if (settled) {
            stopped = true;
            controller.abort();
            return;
          }
        }
      } catch {
        if (stopped || controller.signal.aborted) return;
      }
      if (stopped) return;
      await new Promise((resolve) => {
        window.setTimeout(resolve, 2000);
      });
    }
  };

  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}

/**
 * 同时盯多个 cron 执行会话：当前打开的 + 立即执行记下的后台会话。
 * 后台会话在收尾后停订；前台会话保持原样（人还在看就继续重连）。
 */
export function watchCronExecutionSessions(
  sessionIds: string[],
  foregroundSessionId?: string,
  foregroundHasCronMetadata = false,
): () => void {
  const unique = [...new Set(sessionIds.map((id) => String(id || '').trim()).filter(Boolean))];
  const stoppers = unique.map((id) =>
    watchCronExecutionSession(id, {
      stopOnSettled: id !== foregroundSessionId,
      hasCronMetadata: id === foregroundSessionId ? foregroundHasCronMetadata : false,
    }),
  );
  return () => {
    for (const stop of stoppers) stop();
  };
}
