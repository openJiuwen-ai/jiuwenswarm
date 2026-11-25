import { webRequest } from '../../services/webClient';

import type { MarketResources, SystemResources } from '../../types/appMarketResources';

type ResourceSnapshot = MarketResources & { serviceApps: MarketResources['apps'] };

let snapshot: ResourceSnapshot | null = null;
const listeners = new Set<() => void>();
let cardSubscribers = 0;
let pageReading: MarketResources | null = null;
let serviceReading: MarketResources | null = null;
let highUsage = false;

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
const pressure = createPressureTracker();

function readServiceResources() {
  return webRequest<MarketResources>('app_market.resources', {}, { timeoutMs: 5000 });
}

function createResourcePoll(
  read: () => Promise<MarketResources | undefined>,
  publish: (reading: MarketResources | null) => void,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending = false;
  let active = false;
  let generation = 0;

  async function poll() {
    if (!active || pending) return;
    const requestGeneration = generation;
    pending = true;
    let reading: MarketResources | null = null;
    try {
      const result = await read();
      if (Array.isArray(result?.apps)) reading = result;
    } catch {
      // This source is unavailable until its next successful sample.
    }
    pending = false;
    if (!active) return;
    const isCurrent = requestGeneration === generation;
    timer = setTimeout(poll, isCurrent ? 3000 : 0);
    if (isCurrent) publish(reading);
  }

  return {
    start() {
      active = true;
      void poll();
    },
    stop() {
      active = false;
      clearTimeout(timer);
      generation++;
    },
  };
}

function publishSnapshot() {
  const latest = pageReading ?? serviceReading;
  const cardReading = window.jiuwenDesktop?.isElectron ? serviceReading : pageReading;
  snapshot = latest ? {
    apps: pageReading?.apps ?? [],
    system: pageReading?.system,
    sampledAt: latest.sampledAt,
    serviceApps: cardReading?.apps ?? [],
    highUsage,
  } : null;
  listeners.forEach(listener => listener());
}

// Desktop card memory and native totals refresh independently. Web uses one RPC for both.
const pagePoll = createResourcePoll(async () => {
  const desktop = window.jiuwenDesktop;
  return desktop?.isElectron ? desktop.getMarketAppResources?.() : readServiceResources();
}, reading => {
  pageReading = reading;
  highUsage = pressure(reading?.system);
  publishSnapshot();
});
const servicePoll = createResourcePoll(readServiceResources, reading => {
  serviceReading = reading;
  publishSnapshot();
});

export const marketResources = {
  getSnapshot: () => snapshot,
  subscribeCards(listener: () => void) {
    cardSubscribers++;
    const unsubscribe = marketResources.subscribe(listener);
    if (cardSubscribers === 1 && window.jiuwenDesktop?.isElectron) servicePoll.start();
    return () => {
      cardSubscribers--;
      unsubscribe();
      if (!cardSubscribers) {
        servicePoll.stop();
        if (serviceReading) {
          serviceReading = null;
          publishSnapshot();
        }
      }
    };
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    if (listeners.size === 1) pagePoll.start();
    return () => {
      listeners.delete(listener);
      if (!listeners.size) {
        pagePoll.stop();
        pageReading = null;
        snapshot = null;
        highUsage = pressure();
      }
    };
  },
};
