import type { WsEvent } from '../types/websocket';

/** Transport cursors only; accepted inputs are never retried on reconnect. */
export class RuntimeResume {
  private sessions = new Map<string, { instance: string; after: number; seen: Set<number> }>();

  observe(event: WsEvent): boolean {
    const sessionId = event.payload.session_id;
    const marker = event.payload.runtime_event as Record<string, unknown> | undefined;
    if (
      typeof sessionId !== 'string' ||
      !marker ||
      typeof marker.worker_instance !== 'string' ||
      typeof marker.sequence !== 'number'
    )
      return true;
    let cursor = this.sessions.get(sessionId);
    if (!cursor || cursor.instance !== marker.worker_instance) {
      cursor = { instance: marker.worker_instance, after: 0, seen: new Set() };
      this.sessions.set(sessionId, cursor);
    }
    if (cursor.seen.has(marker.sequence)) return false;
    cursor.seen.add(marker.sequence);
    cursor.after = Math.max(cursor.after, marker.sequence);
    if (cursor.seen.size > 4096) cursor.seen.delete(cursor.seen.values().next().value!);
    this.sessions.delete(sessionId);
    this.sessions.set(sessionId, cursor);
    while (this.sessions.size > 256) this.sessions.delete(this.sessions.keys().next().value!);
    return true;
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
