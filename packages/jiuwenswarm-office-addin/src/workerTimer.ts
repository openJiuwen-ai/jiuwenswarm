/** Calls ``callback`` after ``ms``; the returned function cancels it. */
export type Timer = (callback: () => void, ms: number) => () => void;

/**
 * A timer that keeps time while the task pane is hidden. A hidden pane's own
 * timers are throttled (a wait of about 940 s was measured on macOS); a
 * dedicated Worker's are not, so the wait runs there and the Worker posts back.
 */
export function workerTimer(): Timer {
  const worker = new Worker(new URL('./timerWorker.ts', import.meta.url), { type: 'module' });
  const callbacks = new Map<number, () => void>();
  let lastId = 0;
  worker.onmessage = ({ data: id }: MessageEvent<number>) => {
    const callback = callbacks.get(id);
    callbacks.delete(id);
    callback?.();
  };
  return (callback, ms) => {
    lastId += 1;
    const id = lastId;
    callbacks.set(id, callback);
    worker.postMessage({ id, ms });
    return () => callbacks.delete(id);
  };
}
