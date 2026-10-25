/**
 * 企业版「立即执行」会话页的结果监听。
 *
 * 个人版靠长连接收结果；企业版是普通 HTTP，没有这条长连接。
 * 这里盯着 Gateway 的 /sessions/{id}/live，把过程写进当前会话。
 * 历史里已经有你的原话时，不再把直播里的旧正文盖上去。
 * 提问卡片只在直播里，历史不会把它画成可点的按钮，所以这张卡片仍然要画。
 */
import { isEnterprise } from '../../edition';
import { consumeSseBuffer, sseFrameToWsEvent } from '../../services/webHttpClient';
import {
  getManagerAccessToken,
  hasManagerSessionCredentials,
  managerAuthenticatedFetch,
} from '../../auth/manager/authSession';
import { buildRuntimeIdentityHeaders } from '../../services/runtimeScope';
import { useChatStore } from '../../stores/chatStore';
import { webClient } from '../../services/webClient';
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

/** 历史已经画过时，这些帧仍然要画。提问卡片不在历史的可点按钮里。 */
const CRON_LIVE_KEEP_AFTER_HISTORY = new Set(['chat.ask_user_question']);

function shouldPaintCronLiveEvent(eventName: string, historyAlreadyShown: boolean): boolean {
  if (!historyAlreadyShown) return true;
  return CRON_LIVE_KEEP_AFTER_HISTORY.has(eventName);
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
 * 历史已经画出来时，旧正文不再盖上去；还在等你点的提问卡片仍然画。
 * 其它传输、其它会话直接返回空的停止函数。
 */
export function watchCronExecutionSession(sessionId: string): () => void {
  if (getWebTransport() !== 'http' || !isCronExecutionSession(sessionId)) {
    return () => undefined;
  }
  const controller = new AbortController();
  let stopped = false;

  const run = async () => {
    while (!stopped) {
      try {
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
            const runtime = useChatStore.getState().getRuntime(sessionId);
            if (!shouldPaintCronLiveEvent(event.event, Boolean(runtime?.historyPagerMeta))) {
              continue;
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
