const test = require('node:test');
const assert = require('node:assert/strict');
const { createMarketResourceMonitor, createSystemResourceSampler } = require('../../jiuwenswarm/channels/desktop/electron/app_market_resources.cjs');

function entry(pid) {
  return { title: 'Game', view: { webContents: { getOSProcessId: () => pid } },
    window: { isDestroyed: () => false, setTitle(value) { this.title = value; } } };
}

test('market metrics exclude host/GPU, identify shared renderers, and update native titles', t => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const windows = new Map([['one', entry(12)], ['two', entry(12)]]);
  const metrics = [
    { pid: 12, creationTime: 1, cpu: { percentCPUUsage: 7 }, memory: { workingSetSize: 2048 } },
    { pid: 99, creationTime: 2, cpu: { percentCPUUsage: 70 }, memory: { workingSetSize: 999999 } },
  ];
  const monitor = createMarketResourceMonitor(windows, () => metrics, () => 'zh-CN');
  monitor.start();
  assert.equal(monitor.read().apps[0].cpuPercent, null);
  t.mock.timers.tick(3000);
  assert.deepEqual(monitor.read().apps[0], { appId: "one", cpuPercent: 7, memoryBytes: 2097152, shared: true });
  assert.equal("total" in monitor.read(), false);
  assert.equal(monitor.read().apps[0].shared, true);
  assert.match(windows.get('one').window.title, /页面 CPU 7.0% · 内存 2 MiB（共享进程）/);
  windows.delete('two');
  assert.equal(monitor.read().apps.length, 1);
  assert.equal(monitor.read().apps[0].shared, false);
  windows.clear();
  monitor.stopIfEmpty();
  assert.deepEqual(monitor.read().apps, []);
});

test('failed samples clear old numbers; renderer replacements warm up; timer stops with last window', t => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const windows = new Map([['one', entry(12)]]);
  let calls = 0;
  let fail = false;
  let creationTime = 1;
  const monitor = createMarketResourceMonitor(windows, () => {
    calls++;
    if (fail) throw new Error('unavailable');
    return [{ pid: 12, creationTime, cpu: { percentCPUUsage: 3 }, memory: { workingSetSize: 1024 } }];
  });
  monitor.start(); monitor.start();
  assert.equal(calls, 1);
  t.mock.timers.tick(3000);
  assert.equal(monitor.read().apps[0].cpuPercent, 3);
  creationTime++;
  t.mock.timers.tick(3000);
  assert.equal(monitor.read().apps[0].cpuPercent, null);
  fail = true;
  t.mock.timers.tick(3000);
  assert.deepEqual(monitor.read().apps[0], { appId: "one", cpuPercent: null, memoryBytes: null, shared: false });
  assert.match(windows.get('one').window.title, /CPU — · RAM —/);
  windows.clear(); monitor.stopIfEmpty();
  const before = calls;
  t.mock.timers.tick(9000);
  assert.equal(calls, before);
});


for (const platform of ['win32', 'darwin', 'linux']) {
  test(`whole-machine samples on ${platform} exclude reclaimable memory and warm CPU`, () => {
    let clock = 1000;
    let idle = 80;
    let user = 20;
    const read = createSystemResourceSampler({ platform, now: () => clock,
      cpus: () => [{ times: { idle, user, sys: 0, nice: 0, irq: 0 } }],
      memory: () => ({ total: 1000, free: 100, fileBacked: 300, available: platform === 'linux' ? 400 : undefined }),
    });
    const first = read();
    assert.equal(first.cpuPercent, null);
    assert.equal(first.memoryPercent, platform === 'win32' ? 90 : 60);
    assert.equal(read(), first);
    clock += 3000; idle += 40; user += 60;
    assert.equal(read().cpuPercent, 60);
  });
}

test('system metrics work without windows and report unavailable after errors', () => {
  let fail = false;
  let clock = 1000;
  const read = createSystemResourceSampler({ now: () => clock,
    cpus: () => { if (fail) throw new Error('unavailable'); return [{ times: { idle: clock, user: clock } }]; },
    memory: () => { if (fail) throw new Error('unavailable'); return { total: 100, free: 20 }; },
    platform: 'win32',
  });
  assert.equal(read().memoryPercent, 80);
  fail = true; clock += 3000;
  assert.deepEqual(read(), { cpuPercent: null, memoryPercent: null, sampledAt: clock });
  assert.ok(createMarketResourceMonitor(new Map(), () => []).read().system);
});
