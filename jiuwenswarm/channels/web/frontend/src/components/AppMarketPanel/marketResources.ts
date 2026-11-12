import { webRequest } from '../../services/webClient';

import type { MarketResources, SystemResources } from '../../types/appMarketResources';

// One request stream per renderer, shared by market and preview components.
let snapshot: MarketResources | null = null;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let pending = false;
let generation = 0;

// Require sustained high usage, with a lower recovery threshold to avoid flicker.
export function createPressureTracker() {
  let count = 0;
  let high = false;
  let previousTime: number | undefined;
  return (system?: SystemResources) => {
    if (!system || system.cpuPercent == null || system.memoryPercent == null) {
      count = 0; high = false; previousTime = undefined;
      return false;
    }
    if (system.sampledAt === previousTime) return high;
    if (previousTime !== undefined && system.sampledAt - previousTime > 10000) { count = 0; high = false; }
    previousTime = system.sampledAt;
    if (system.cpuPercent >= 85 || system.memoryPercent >= 85) {
      high = high || ++count >= 3;
    } else {
      count = 0;
      if (system.cpuPercent < 75 && system.memoryPercent < 75) high = false;
    }
    return high;
  };
}
let pressure = createPressureTracker();

async function poll() {
  if (pending || !listeners.size) return;
  pending = true;
  const requestGeneration = generation;
  try {
    const desktop = window.jiuwenDesktop;
    const next = desktop?.isElectron
      ? await desktop.getMarketAppResources?.() ?? null
      : await webRequest<MarketResources>('app_market.resources', {}, { timeoutMs: 5000 });
    if (requestGeneration === generation) {
      snapshot = next && Array.isArray(next.apps)
        ? { ...next, highUsage: pressure(next.system) } : null;
      if (!snapshot) pressure = createPressureTracker();
    }
  } catch { if (requestGeneration === generation) { snapshot = null; pressure = createPressureTracker(); } }
  finally {
    pending = false;
    if (listeners.size) {
      listeners.forEach(listener => listener());
      timer = setTimeout(poll, requestGeneration === generation ? 3000 : 0);
    } else snapshot = null;
  }
}

export const marketResources = {
  getSnapshot: () => snapshot,
  subscribe(listener: () => void) {
    listeners.add(listener);
    if (listeners.size === 1) void poll();
    return () => {
      listeners.delete(listener);
      if (!listeners.size) { clearTimeout(timer); snapshot = null; pressure = createPressureTracker(); generation++; }
    };
  },
};
