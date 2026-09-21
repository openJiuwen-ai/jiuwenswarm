import { useChatStore } from '../stores/chatStore';
import { useSessionStore } from '../stores/sessionStore';
import { readOutputOrder } from './sessionOutput';
import type { WebError, WebRequestOptions } from '../types/websocket';

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
  const startedWhileIdle = !useChatStore.getState().getRuntime(sessionId)?.isProcessing;
  if (startedWhileIdle) {
    useChatStore.getState().setProcessing(sessionId, true);
    useChatStore.getState().setThinking(sessionId, true);
  }
  let requestId: string | undefined;
  try {
    const executionId = useChatStore.getState().getRuntime(sessionId)?.activeExecutionId;
    await request(
      'chat.send',
      {
        ...context,
        session_id: sessionId,
        content: task.content,
        input_mode: 'steer',
        ...(executionId ? { expected_execution_id: executionId } : {}),
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
    // Restore an idle send failure only if no actual execution has arrived in the meantime.
    const store = useChatStore.getState();
    const runtime = store.getRuntime(sessionId);
    if (startedWhileIdle && runtime?.taskInputReceipts[taskId]?.status === 'failed' && !runtime.activeExecutionId) {
      store.setProcessing(sessionId, false);
      store.setThinking(sessionId, false);
    }
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
  const { taskId, delivery } = runtime.taskInputRequests[requestId];
  // Runtime admitted an unbound input as ordinary chat while idle. Its output owns a new turn.
  if (delivery === 'chat') return event === 'runtime.accepted';
  if (event === 'runtime.accepted') {
    let acceptedDelivery: 'chat' | 'stream' | undefined;
    if (payload.input_delivery === 'chat') {
      acceptedDelivery = 'chat';
    } else if (payload.input_boundary === 'stream') {
      acceptedDelivery = 'stream';
    }
    store.settleTaskInput(sessionId, taskId, requestId, 'accepted', undefined, undefined, acceptedDelivery);
    const current = store.getRuntime(sessionId);
    const receipt = current?.taskInputReceipts[taskId];
    if (
      receipt?.requestId === requestId && receipt.status === 'accepted' && current?.isProcessing &&
      !current.activeExecutionId && typeof payload.execution_id === 'string'
    ) {
      store.setActiveExecutionId(sessionId, payload.execution_id);
    }
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

/** Ordered SDK markers are independent of the supplemental request's ACK. */
export function handleSessionOutputBoundary(event: string, payload: Record<string, unknown>): void {
  const sessionId = payload.session_id;
  if (typeof sessionId !== 'string') return;
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  if (event === 'chat.output_phase') {
    if (typeof payload.output_phase_id !== 'string') return;
    store.setOutputPhase(sessionId, payload.output_phase_id);
    return;
  }
  const requestId = payload.input_request_id;
  if (typeof requestId !== 'string' || typeof payload.content !== 'string') return;
  const runtime = store.getRuntime(sessionId)!;
  if (runtime.messages.some((message) => message.supplementalInput?.requestId === requestId)) return;
  const request = runtime.taskInputRequests[requestId];
  if (request) store.settleTaskInput(sessionId, request.taskId, requestId, 'accepted', undefined, undefined, 'stream');
  const boundaryTimestamp = new Date(Number(payload.timestamp)).toISOString();
  if (runtime.currentStreamId) {
    store.updateMessage(sessionId, runtime.currentStreamId, { completedAt: boundaryTimestamp });
  }
  store.stopStreaming(sessionId);
  store.closeReasoning(sessionId, { atMs: Number(payload.timestamp) });
  store.addMessage(sessionId, {
    id: `user-input-${requestId}`,
    role: 'user',
    content: payload.content,
    outputOrder: readOutputOrder(payload),
    timestamp: boundaryTimestamp,
    supplementalInput: {
      executionId: typeof payload.execution_id === 'string' ? payload.execution_id : '',
      requestId,
      streamOffset: 0,
    },
  });
  store.setThinking(sessionId, true);
}

/** An old phase may finish after the user boundary; it never owns the new bubble. */
export function shouldIgnoreSessionOutput(payload: Record<string, unknown>): boolean {
  if (payload.output_suppressed === true) return true;
  const phase = payload.output_phase_id;
  const sessionId = payload.session_id;
  if (typeof phase !== 'string' || typeof sessionId !== 'string') return false;
  const current = useChatStore.getState().getRuntime(sessionId)?.outputPhaseId;
  return Boolean(current && current !== phase);
}
