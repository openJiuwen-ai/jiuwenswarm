import type { AgentMode } from '../../types';

export interface PendingPreviousSession {
  sessionId: string;
  mode: AgentMode;
}

interface ResolvePendingPreviousSessionOptions {
  currentSessionId: string;
  currentMode: AgentMode;
  pending: PendingPreviousSession | null;
  newConversationId: string;
  clear?: boolean;
}

/**
 * Keep the real previous Session across repeated clicks on the draft page.
 * A completed deletion explicitly clears it because session.delete already
 * owns the KVC eviction for that Session.
 */
export function resolvePendingPreviousSession({
  currentSessionId,
  currentMode,
  pending,
  newConversationId,
  clear = false,
}: ResolvePendingPreviousSessionOptions): PendingPreviousSession | null {
  if (clear) return null;
  if (currentSessionId && currentSessionId !== newConversationId) {
    return { sessionId: currentSessionId, mode: currentMode };
  }
  return pending;
}
