import type { ChatSendOptions } from '../types';

const remembered = new Set<string>();
const key = (sessionId: string) => `group-chat-input:${sessionId}`;

/** Remember only conversations whose structured group input was accepted. */
export function rememberGroupChatInput(sessionId: string): void {
  remembered.add(sessionId);
  try {
    window.sessionStorage.setItem(key(sessionId), '1');
  } catch {
    /* Storage may be unavailable. */
  }
}

export function defaultGroupChatInput(sessionId: string, leader: string): ChatSendOptions['groupChat'] {
  let enabled = remembered.has(sessionId);
  try {
    enabled ||= window.sessionStorage.getItem(key(sessionId)) === '1';
  } catch {
    /* Use this page's state. */
  }
  return enabled ? { sessionId, clientMessageId: crypto.randomUUID(), mentions: [leader] } : undefined;
}
