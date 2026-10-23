import type { WsEvent } from '../types/websocket';
import { normalizePendingQuestion, pendingQuestionIdentity } from '../stores/pendingQuestionQueue';

interface SessionCursor {
  instance: string;
  after: number;
  seen: Set<number>;
  snapshotCursor?: number;
  pending: Set<string>;
  resolved: Set<string>;
}

/** Recovery never resubmits accepted inputs or revives resolved interactions. */
export class RuntimeResume {
  private sessions = new Map<string, SessionCursor>();

  applySnapshot(sessionId: string, snapshot: Record<string, unknown>): boolean {
    const { worker_instance: instance, cursor: sequence, dispatches } = snapshot;
    if (
      snapshot.recovery_pending === true || typeof instance !== 'string' ||
      typeof sequence !== 'number' || !Number.isSafeInteger(sequence) || sequence < 0 || !Array.isArray(dispatches)
    ) return true;
    if (dispatches.some((item) => !item || !Array.isArray(item.pending_interactions))) return true;
    const cursor = this.cursor(sessionId, instance);
    if (cursor.snapshotCursor !== undefined && sequence < cursor.snapshotCursor) return false;
    cursor.snapshotCursor = sequence;
    cursor.pending.clear();
    for (const dispatch of dispatches) {
      for (const interaction of dispatch.pending_interactions) {
        if (interaction?.event_type !== 'chat.ask_user_question') continue;
        const identity = pendingQuestionIdentity(normalizePendingQuestion(interaction));
        if (identity && !cursor.resolved.has(identity)) cursor.pending.add(identity);
      }
    }
    return true;
  }

  shouldDispatch(event: WsEvent): boolean {
    const sessionId = event.payload.session_id;
    const current = typeof sessionId === 'string' ? this.sessions.get(sessionId) : undefined;
    if (event.event === 'runtime.snapshot' && current?.snapshotCursor !== undefined) {
      if (event.payload.recovery_pending === true) return true;
      if (typeof event.payload.cursor === 'number') {
        return current.instance === event.payload.worker_instance && event.payload.cursor >= current.snapshotCursor;
      }
    }
    const marker = event.payload.runtime_event as Record<string, unknown> | undefined;
    if (typeof sessionId !== 'string' || !marker || typeof marker.sequence !== 'number') return true;
    const cursor = this.sessions.get(sessionId);
    if (!cursor) return true;
    if (cursor.instance !== marker.worker_instance) {
      return !['chat.ask_user_question', 'chat.processing_status', 'chat.interrupt_result'].includes(event.event);
    }
    if (event.event === 'chat.ask_user_question') {
      const identity = pendingQuestionIdentity(normalizePendingQuestion(event.payload));
      if (identity && cursor.resolved.has(identity)) return false;
      if (cursor.snapshotCursor !== undefined && marker.sequence <= cursor.snapshotCursor) {
        return !!identity && cursor.pending.has(identity);
      }
    }
    if (['chat.processing_status', 'chat.interrupt_result'].includes(event.event) && cursor.snapshotCursor !== undefined) {
      return marker.sequence > cursor.snapshotCursor;
    }
    return true;
  }

  isHistoricalEvent(payload: Record<string, unknown>): boolean {
    const sessionId = payload.session_id;
    const marker = payload.runtime_event as Record<string, unknown> | undefined;
    const cursor = typeof sessionId === 'string' ? this.sessions.get(sessionId) : undefined;
    if (!cursor || typeof marker?.worker_instance !== 'string' || typeof marker.sequence !== 'number') return false;
    if (marker.worker_instance !== cursor.instance) return true;
    return cursor.snapshotCursor !== undefined && marker.sequence <= cursor.snapshotCursor;
  }

  observe(event: WsEvent): boolean {
    const sessionId = event.payload.session_id;
    const marker = event.payload.runtime_event as Record<string, unknown> | undefined;
    if (
      typeof sessionId !== 'string' ||
      !marker ||
      typeof marker.worker_instance !== 'string' ||
      typeof marker.sequence !== 'number'
    ) {
      this.retireCancelledInteractions(event);
      return true;
    }
    const previous = this.sessions.get(sessionId);
    if (previous && previous.instance !== marker.worker_instance && marker.replayed === true) return false;
    const cursor = this.cursor(sessionId, marker.worker_instance);
    if (cursor.seen.has(marker.sequence)) return false;
    cursor.seen.add(marker.sequence);
    cursor.after = Math.max(cursor.after, marker.sequence);
    if (cursor.seen.size > 4096) cursor.seen.delete(cursor.seen.values().next().value!);
    this.retireCancelledInteractions(event);
    if (event.event === 'chat.ask_user_question' && this.shouldDispatch(event)) {
      const identity = pendingQuestionIdentity(normalizePendingQuestion(event.payload));
      if (identity) cursor.pending.add(identity);
    }
    return true;
  }

  private retireCancelledInteractions(event: WsEvent): void {
    const { session_id: sessionId, intent, success } = event.payload;
    if (
      typeof sessionId !== 'string' || event.event !== 'chat.interrupt_result' ||
      intent !== 'cancel' || success !== true || !this.shouldDispatch(event)
    ) return;
    const current = this.sessions.get(sessionId);
    if (!current) return;
    for (const identity of current.pending) current.resolved.add(identity);
    current.pending.clear();
    const marker = event.payload.runtime_event as Record<string, unknown> | undefined;
    const beforeCancel = typeof marker?.sequence === 'number' ? marker.sequence - 1 : current.after;
    current.snapshotCursor = Math.max(current.snapshotCursor ?? 0, beforeCancel);
    while (current.resolved.size > 4096) current.resolved.delete(current.resolved.values().next().value!);
  }

  private cursor(sessionId: string, instance: string): SessionCursor {
    let cursor = this.sessions.get(sessionId);
    if (!cursor || cursor.instance !== instance) {
      cursor = { instance, after: 0, seen: new Set(), pending: new Set(), resolved: new Set() };
    }
    this.sessions.delete(sessionId);
    this.sessions.set(sessionId, cursor);
    while (this.sessions.size > 256) this.sessions.delete(this.sessions.keys().next().value!);
    return cursor;
  }

  queries(): Array<{ session_id: string; runtime_resume: { worker_instance: string; after: number } }> {
    return Array.from(this.sessions, ([session_id, cursor]) => ({
      session_id,
      runtime_resume: { worker_instance: cursor.instance, after: cursor.after },
    }));
  }

  clear(): void {
    this.sessions.clear();
  }
}
