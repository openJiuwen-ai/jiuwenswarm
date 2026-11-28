const STARTUP_WINDOW_MS = 15_000;

/** Bound fast retries to the first connection to a local service. */
export function connectionRetryDelay(
  attempt: number,
  url: string,
  connectedOnce: boolean,
  startupElapsedMs: number,
  random: number = Math.random(),
): number {
  let local = false;
  try {
    local = ['localhost', '127.0.0.1', '[::1]'].includes(new URL(url).hostname.toLowerCase());
  } catch {
    // Invalid addresses keep the established retry policy.
  }
  if (local && !connectedOnce && startupElapsedMs >= 0 && startupElapsedMs < STARTUP_WINDOW_MS) {
    const base = attempt === 1 ? 250 : 500;
    return Math.round(base * (0.9 + Math.max(0, Math.min(random, 1)) * 0.2));
  }
  return attempt <= 5 ? Math.min(1000 * 2 ** (attempt - 1), 30000) : 2000;
}
