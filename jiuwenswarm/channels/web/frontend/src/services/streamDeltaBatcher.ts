type TimerHandle = ReturnType<typeof globalThis.setTimeout>;
type FlushCallback = (content: string) => void;

interface StreamDeltaBatcherOptions {
  delayMs?: number;
  schedule?: (callback: () => void, delayMs: number) => TimerHandle;
  cancel?: (handle: TimerHandle) => void;
}

interface PendingDelta {
  content: string;
  flush: FlushCallback;
  timer: TimerHandle;
}

export function createStreamDeltaBatcher(options: StreamDeltaBatcherOptions = {}) {
  const delayMs = options.delayMs ?? 16;
  const schedule = options.schedule ?? ((callback: () => void, delay: number) => globalThis.setTimeout(callback, delay));
  const cancel = options.cancel ?? ((handle: TimerHandle) => globalThis.clearTimeout(handle));
  const pending = new Map<string, PendingDelta>();

  const flush = (key: string): void => {
    const item = pending.get(key);
    if (!item) return;
    pending.delete(key);
    cancel(item.timer);
    item.flush(item.content);
  };

  const clear = (key: string): void => {
    const item = pending.get(key);
    if (!item) return;
    pending.delete(key);
    cancel(item.timer);
  };

  return {
    enqueue(key: string, content: string, onFlush: FlushCallback): void {
      if (content === '') return;
      const current = pending.get(key);
      if (current) {
        current.content += content;
        current.flush = onFlush;
        return;
      }
      const timer = schedule(() => flush(key), delayMs);
      pending.set(key, { content, flush: onFlush, timer });
    },
    flush,
    flushBefore(key: string, action: () => void): void {
      flush(key);
      action();
    },
    clear,
    flushAll(): void {
      [...pending.keys()].forEach(flush);
    },
    clearAll(): void {
      [...pending.keys()].forEach(clear);
    },
  };
}
