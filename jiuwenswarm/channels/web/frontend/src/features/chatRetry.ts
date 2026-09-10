import type { Message } from '../types/message';

export interface RetryRequest {
  payload: Record<string, unknown>;
  userMessageId: string;
  createdAt: number;
}

/** Ephemeral snapshots: one attempt per session, bounded across inactive sessions. */
export class ChatRetryRequests {
  private requests = new Map<string, RetryRequest>();

  constructor(
    private readonly limit = 40,
    private readonly ttlMs = 30 * 60 * 1000,
  ) {}

  remember(requestId: string, payload: Record<string, unknown>, userMessageId: string): void {
    this.clearSession(String(payload.session_id));
    this.requests.set(requestId, { payload: structuredClone(payload), userMessageId, createdAt: Date.now() });
    while (this.requests.size > this.limit) {
      const oldest = this.requests.keys().next().value;
      if (oldest === undefined) break;
      this.requests.delete(oldest);
    }
  }

  get(requestId: string): RetryRequest | undefined {
    const entry = this.requests.get(requestId);
    if (entry && Date.now() - entry.createdAt >= this.ttlMs) {
      this.requests.delete(requestId);
      return undefined;
    }
    return entry;
  }

  forget(requestId: string): void {
    this.requests.delete(requestId);
  }

  clearSession(sessionId: string): void {
    for (const [id, entry] of this.requests) {
      if (entry.payload.session_id === sessionId) this.requests.delete(id);
    }
  }

  pruneSessions(sessionIds: Set<string>): void {
    for (const [id, entry] of this.requests) {
      if (!sessionIds.has(String(entry.payload.session_id))) this.requests.delete(id);
    }
  }
}

/** Never attach an old error to a newer turn or automatically repeat tool work. */
export function canRetryRequest(entry: RetryRequest | undefined, sessionId: string, messages: Message[]): boolean {
  if (!entry || entry.payload.session_id !== sessionId) return false;
  // Steering and orchestrated workflows can resume work outside this chat turn.
  const mode = String(entry.payload.mode ?? 'agent');
  if (!mode.startsWith('agent') || entry.payload.input_mode || entry.payload.enable_swarmflow) return false;
  const index = messages.findIndex((message) => message.id === entry.userMessageId);
  if (index < 0 || messages[index].role !== 'user') return false;
  return !messages
    .slice(index + 1)
    .some((message) => message.role === 'user' || message.role === 'tool' || message.toolCall || message.toolResult);
}
