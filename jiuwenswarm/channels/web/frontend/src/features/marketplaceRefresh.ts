type RefreshOptions = { silent?: boolean };

interface RefreshScheduler {
  schedule: (callback: () => void, delayMs: number) => unknown;
  cancel: (handle: unknown) => void;
}

const defaultScheduler: RefreshScheduler = {
  schedule: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  cancel: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function startSequentialRefresh(
  refresh: (options?: RefreshOptions) => Promise<void>,
  intervalMs: number,
  scheduler: RefreshScheduler = defaultScheduler,
): () => void {
  let stopped = false;
  let timer: unknown;

  const run = async (options?: RefreshOptions) => {
    try {
      await refresh(options);
    } finally {
      if (!stopped) timer = scheduler.schedule(() => void run({ silent: true }), intervalMs);
    }
  };

  void run();
  return () => {
    stopped = true;
    if (timer !== undefined) scheduler.cancel(timer);
  };
}
