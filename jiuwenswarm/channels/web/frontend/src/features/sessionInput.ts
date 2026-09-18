import { useChatStore } from '../stores/chatStore';
import { useSessionStore } from '../stores/sessionStore';
import type { WebError, WebRequestOptions } from '../types/websocket';
import i18n from '../i18n';

type SendRequest = (method: string, params: Record<string, unknown>, options: WebRequestOptions) => Promise<unknown>;

function deliveryFailedStatus(error: WebError, sent: boolean): 'failed' | 'unknown' {
  const payload = error.payload as Record<string, unknown> | undefined;
  const code = error.code ?? payload?.code;
  if (
    code === 'SESSION_INPUT_DELIVERY_UNKNOWN' ||
    (sent && ['REQUEST_TIMEOUT', 'WS_DISCONNECTED', 'WS_CLOSED', 'REQUEST_ABORTED'].includes(String(code))) ||
    error.message.includes('supplemental delivery is unknown') ||
    (sent && error.message.includes('WebSocket connection closed')) ||
    (sent && !code && !payload)
  ) {
    return 'unknown';
  }
  return 'failed';
}

/** Claim synchronously before any await; ordinary draining and double clicks cannot send this item again. */
export async function sendQueuedTaskInput(
  sessionId: string,
  taskId: string,
  request: SendRequest,
  context: Record<string, unknown>,
): Promise<void> {
  if (useSessionStore.getState().getRuntime(sessionId)?.mode !== 'agent') return;
  const task = useChatStore.getState().claimTaskInput(sessionId, taskId);
  if (!task) return;
  let requestId: string | undefined;
  try {
    const executionId = useChatStore.getState().getRuntime(sessionId)?.activeExecutionId;
    if (!executionId) throw new Error(i18n.t('network.supplementTargetUnavailable'));
    await request(
      'chat.send',
      {
        ...context,
        session_id: sessionId,
        content: task.content,
        input_mode: 'steer',
        expected_execution_id: executionId,
      },
      {
        awaitRuntimeAccepted: true,
        onRequestId: (id) => {
          requestId = id;
          useChatStore.getState().bindTaskInputRequest(sessionId, taskId, id);
        },
      },
    );
    useChatStore.getState().settleTaskInput(sessionId, taskId, requestId, 'accepted');
  } catch (error) {
    const failure: WebError = error instanceof Error ? error : new Error(String(error));
    useChatStore
      .getState()
      .settleTaskInput(
        sessionId,
        taskId,
        requestId,
        deliveryFailedStatus(failure, Boolean(requestId)),
        failure.message,
        failure.code,
      );
  }
}

/** Route receipt events before normal chat/Goal handlers, including ACKs arriving after a timeout. */
export function handleTaskInputReceipt(event: string, payload: Record<string, unknown>): boolean {
  const requestId = payload.request_id;
  if (typeof requestId !== 'string') return false;
  const store = useChatStore.getState();
  const owner = Object.entries(store.runtimes).find(([, runtime]) => runtime.taskInputRequests?.[requestId]);
  if (!owner) return false;
  const [sessionId, runtime] = owner;
  if (payload.session_id && payload.session_id !== sessionId) return true;
  const { taskId } = runtime.taskInputRequests[requestId];
  if (event === 'runtime.accepted') {
    store.settleTaskInput(sessionId, taskId, requestId, 'accepted');
  } else if (event === 'chat.error') {
    const error = new Error(
      typeof payload.error === 'string' ? payload.error : 'Supplemental input failed',
    ) as WebError;
    error.payload = payload;
    error.code = typeof payload.code === 'string' ? payload.code : undefined;
    store.settleTaskInput(sessionId, taskId, requestId, deliveryFailedStatus(error, true), error.message, error.code);
  }
  return true;
}
