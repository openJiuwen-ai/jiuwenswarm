/**
 * 企业版「立即执行」会话页的结果监听。
 *
 * 个人版靠长连接收结果；企业版是普通 HTTP，没有这条长连接。
 * 这里盯着 Gateway 的 /sessions/{id}/live，把过程写进当前会话。
 * 加载历史不代表执行已结束，后续直播帧继续交给现有聊天处理。
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
]);

function isCronExecutionSession(sessionId: string): boolean {
  return sessionId.startsWith('cron_') || sessionId.startsWith('__cron__');
}

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

/**
 * 企业版 HTTP 下，盯住定时任务执行会话。
 * 思考、逐字正文、工具调用和终稿都交给现有的聊天处理去画。
 * 历史只抑制已覆盖的首次回放；新输出、提问和执行结束状态继续转发。
 * 其它传输、其它会话直接返回空的停止函数。
 */
export function watchCronExecutionSession(sessionId: string): () => void {
  if (getWebTransport() !== 'http' || !isCronExecutionSession(sessionId)) {
    return () => undefined;
  }
  const controller = new AbortController();
  let stopped = false;
  const seenSequences = new Set<number>();

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
          for (const frame of consumed.frames) {
            const event = sseFrameToWsEvent(frame);
            if (!event || !CRON_LIVE_EVENTS.has(event.event)) continue;
            const cron = event.payload.cron;
            const isPlaceholder = Boolean(
              cron && typeof cron === 'object' && (cron as { is_placeholder?: boolean }).is_placeholder,
            );
            if (isPlaceholder) continue;
            const sequence = event.payload.live_seq;
            if (typeof sequence === 'number' && Number.isSafeInteger(sequence) && sequence > 0) {
              if (seenSequences.has(sequence)) continue;
              seenSequences.add(sequence);
              if (seenSequences.size > 4096) seenSequences.delete(seenSequences.values().next().value!);
            }
            const runtime = useChatStore.getState().getRuntime(sessionId);
            // A restored timeline already contains the initial backlog. New
            // frames and unseen reconnect frames must still reach the renderer.
            if (event.payload.live_replay === true && runtime?.historyPagerMeta && !resuming) {
              if (event.event === 'chat.final') {
                const content = normalizeFinalContent(event.payload).trim();
                if (content && runtime.messages.some(message =>
                  message.role === 'assistant' && message.content.trim() === content,
                )) continue;
              } else if (event.event === 'chat.processing_status') {
                if (event.payload.is_processing !== false) continue;
              } else if (event.event !== 'chat.ask_user_question' && event.event !== 'chat.error') {
                continue;
              }
            }
            if (!event.payload.session_id) {
              event.payload.session_id = sessionId;
            }
            webClient.injectEvent(event);
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
