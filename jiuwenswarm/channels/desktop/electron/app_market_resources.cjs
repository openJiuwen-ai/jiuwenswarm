const os = require('node:os');

// Demand-driven host sampling also works when no application window is open.
function createSystemResourceSampler({ cpus = os.cpus, memory = () => process.getSystemMemoryInfo(),
  platform = process.platform, now = Date.now } = {}) {
  let previous;
  let snapshot;
  return () => {
    const sampledAt = now();
    if (snapshot && sampledAt - snapshot.sampledAt < 2500) return snapshot;
    const reading = { cpuPercent: null, memoryPercent: null, sampledAt };
    try {
      const cores = cpus();
      const total = cores.reduce((sum, cpu) => sum + Object.values(cpu.times).reduce((a, b) => a + b, 0), 0);
      const idle = cores.reduce((sum, cpu) => sum + cpu.times.idle, 0);
      if (previous && cores.length === previous.count && total > previous.total && idle >= previous.idle) {
        reading.cpuPercent = Math.max(0, Math.min(100, (1 - (idle - previous.idle) / (total - previous.total)) * 100));
      }
      previous = cores.length ? { total, idle, count: cores.length } : undefined;
    } catch { previous = undefined; }
    try {
      const info = memory();
      // macOS file-backed cache is reclaimable; do not count it as application pressure.
      const available = platform === 'darwin' ? info.free + info.fileBacked : (info.available ?? info.free);
      if (Number.isFinite(info.total) && info.total > 0 && Number.isFinite(available)) {
        reading.memoryPercent = Math.max(0, Math.min(100, (1 - available / info.total) * 100));
      }
    } catch { /* Unavailable metrics remain unknown. */ }
    snapshot = reading;
    return reading;
  };
}

// Only market-owned main renderers are measured; GPU/host/AI processes are excluded.
function createMarketResourceMonitor(windows, getMetrics, getLocale = () => 'en') {
  const readSystem = createSystemResourceSampler();
  let timer;
  let previous = new Set();
  let snapshot = { apps: [], sampledAt: Date.now() };
  const sample = () => {
    const apps = [];
    const processes = new Map();
    let metrics;
    try { metrics = new Map(getMetrics().map(metric => [metric.pid, metric])); } catch { metrics = new Map(); }
    for (const [appId, entry] of windows) {
      let pid;
      try { pid = entry.view.webContents.getOSProcessId(); } catch { /* Closed or crashed renderer. */ }
      const metric = metrics.get(pid);
      const identity = metric && `${pid}:${metric.creationTime}`;
      const reading = {
        appId,
        cpuPercent: identity && previous.has(identity) && Number.isFinite(metric.cpu?.percentCPUUsage)
          ? Math.max(0, metric.cpu.percentCPUUsage) : null,
        memoryBytes: Number.isFinite(metric?.memory?.workingSetSize) ? metric.memory.workingSetSize * 1024 : null,
        shared: false,
      };
      apps.push(reading);
      if (identity) {
        const group = processes.get(identity) || [];
        group.push(reading);
        processes.set(identity, group);
      }
    }
    for (const group of processes.values()) for (const item of group) item.shared = group.length > 1;
    snapshot = { apps, sampledAt: Date.now() };
    previous = new Set(processes.keys());
    const zh = getLocale().startsWith('zh');
    for (const item of apps) {
      const entry = windows.get(item.appId);
      if (!entry || entry.window.isDestroyed()) continue;
      const cpu = item.cpuPercent === null ? '—' : `${item.cpuPercent.toFixed(1)}%`;
      const ram = item.memoryBytes === null ? '—' : `${Math.round(item.memoryBytes / 1048576)} MiB`;
      const shared = item.shared ? (zh ? '（共享进程）' : ' (shared process)') : '';
      entry.window.setTitle(`${entry.title} · ${zh ? '页面' : 'Page'} CPU ${cpu} · ${zh ? '内存' : 'RAM'} ${ram}${shared}`);
    }
  };
  return {
    start() {
      if (!getMetrics || timer) return;
      sample();
      timer = setInterval(sample, 3000);
      timer.unref?.();
    },
    stopIfEmpty() {
      if (windows.size) return;
      clearInterval(timer);
      timer = undefined;
      previous.clear();
      snapshot = { apps: [], sampledAt: Date.now() };
    },
    read() {
      // Do not expose a closed app while waiting for the next sample.
      if (snapshot.apps.some(item => !windows.has(item.appId))
          || windows.size !== snapshot.apps.length) sample();
      return { ...snapshot, system: readSystem() };
    },
  };
}

module.exports = { createMarketResourceMonitor, createSystemResourceSampler };
