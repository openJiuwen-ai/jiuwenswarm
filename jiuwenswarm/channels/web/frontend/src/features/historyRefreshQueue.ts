/** Coalesce recovery notifications without replacing a live turn or history load. */
export class HistoryRefreshQueue {
  private pending = new Set<string>();

  request(sessionId: string): void {
    this.pending.add(sessionId);
  }

  take(sessionId: string, busy: boolean): boolean {
    if (busy || !this.pending.has(sessionId)) return false;
    this.pending.delete(sessionId);
    return true;
  }
}
