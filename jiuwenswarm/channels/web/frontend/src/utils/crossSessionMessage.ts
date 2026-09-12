export const CROSS_SESSION_MESSAGE_ORIGIN = 'cross_session_agent';

export interface CrossSessionMessageMetadata {
  messageId: string;
  sourceSessionId: string;
  sourceTitle?: string;
  content?: string;
}

function readString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function extractCrossSessionMessage(
  payload: Record<string, unknown>
): CrossSessionMessageMetadata | null {
  const raw = payload.cross_session;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return null;
  }
  const crossSession = raw as Record<string, unknown>;
  const messageId =
    readString(crossSession.message_id) || readString(payload.session_message_id);
  const sourceSessionId = readString(crossSession.source_session_id);
  const origin = readString(payload.message_origin);
  if (
    !messageId ||
    !sourceSessionId ||
    (origin && origin !== CROSS_SESSION_MESSAGE_ORIGIN)
  ) {
    return null;
  }
  const sourceTitle = readString(crossSession.source_title);
  const content = readString(crossSession.content);
  return {
    messageId,
    sourceSessionId,
    ...(sourceTitle ? { sourceTitle } : {}),
    ...(content ? { content } : {}),
  };
}

export function crossSessionUserMessageId(messageId: string): string {
  return `cross-session-user-${messageId}`;
}

export function crossSessionAssistantMessageId(
  requestId: unknown,
  messageId: string
): string {
  const normalizedRequestId = readString(requestId);
  return `cross-session-assistant-${normalizedRequestId || messageId}`;
}
