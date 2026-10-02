import assert from 'node:assert/strict';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const enabledConfig = {
  configured: true,
  master_enabled: true,
  collection_enabled: true,
  agent_use_enabled: true,
  strategy_profile: 'rules',
  max_pages_per_directory: 20,
  max_subdirectories_per_directory: 20,
  model_index: null,
  model_id: null,
  fetch_services: [],
};

let rejectStop;
let rejectMaster;
let storedConfig = { ...enabledConfig };
let configError = null;
let configReads = 0;
let deferredConfigRead = null;
let deferredStatusRead = null;
let runtimeState = 'STOPPED';
let statusReads = 0;
globalThis.__pcApi = {
  stopRuntime: () =>
    new Promise((_resolve, reject) => {
      rejectStop = reject;
    }),
  setMasterEnabled: () =>
    new Promise((_resolve, reject) => {
      rejectMaster = reject;
    }),
  getConfig: async () => {
    configReads += 1;
    if (configError) throw configError;
    if (deferredConfigRead) {
      const pending = deferredConfigRead;
      deferredConfigRead = null;
      return pending;
    }
    return { ...storedConfig };
  },
  listServices: async () => ({ services: [] }),
  getStatus: async () => {
    statusReads += 1;
    if (deferredStatusRead) {
      const pending = deferredStatusRead;
      deferredStatusRead = null;
      return pending;
    }
    return { state: runtimeState, collection_enabled: false };
  },
  getRunStatus: async () => ({ services: [] }),
};

const bundle = await build({
  entryPoints: [path.join(frontend, 'src/stores/personalContextStore.ts')],
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
  plugins: [
    {
      name: 'personal-context-api-boundary',
      setup(plugin) {
        plugin.onResolve({ filter: /personalContextApi$/ }, () => ({ path: 'pc-api', namespace: 'diagnostic' }));
        plugin.onLoad({ filter: /.*/, namespace: 'diagnostic' }, () => ({
          contents: 'export const pcApi = globalThis.__pcApi;',
        }));
      },
    },
  ],
});
const storeUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString('base64')}`;
const { usePersonalContextStore } = await import(storeUrl);

test('stop request timeout keeps intent until authoritative config is read', async () => {
  storedConfig = { ...enabledConfig, collection_enabled: false };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setEnabled(false);
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);

  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  await usePersonalContextStore.getState().batchRefresh();
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
});

test('settings switch starts reconciliation without opening the services page', async () => {
  storedConfig = { ...enabledConfig, collection_enabled: false };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });
  const readsBefore = configReads;

  const operation = usePersonalContextStore.getState().setEnabled(false);
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(configReads, readsBefore + 1);
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
});

test('authoritative rollback restores enabled after timeout', async () => {
  storedConfig = { ...enabledConfig };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setEnabled(false);
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));
  await usePersonalContextStore.getState().batchRefresh();
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, true);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
});

test('unavailable config leaves switch uncertain without blocking other refreshes', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  storedConfig = { ...enabledConfig, collection_enabled: false };
  configError = new Error('config read unavailable');
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setEnabled(false);
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await usePersonalContextStore.getState().batchRefresh();

  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
  assert.deepEqual(usePersonalContextStore.getState().runHistories, {});
  configError = null;
  await usePersonalContextStore.getState().batchRefresh();
  context.mock.timers.reset();
});

test('config verification retries after an initial read failure', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  storedConfig = { ...enabledConfig, collection_enabled: false };
  configError = new Error('config read unavailable');
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setEnabled(false);
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);

  configError = null;
  context.mock.timers.tick(5000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  context.mock.timers.reset();
});

test('polling cannot overwrite a switch write still in flight', async () => {
  storedConfig = { ...enabledConfig };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setEnabled(false);
  usePersonalContextStore.setState({ configNeedsReconciliation: true });
  const readsBefore = configReads;
  await usePersonalContextStore.getState().batchRefresh();
  assert.equal(configReads, readsBefore + 1);
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);

  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
});

test('a config read started before a timed-out switch cannot undo later confirmation', async () => {
  storedConfig = { ...enabledConfig };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  let resolveStaleRead;
  deferredConfigRead = new Promise((resolve) => {
    resolveStaleRead = resolve;
  });
  const staleRead = usePersonalContextStore.getState().loadConfig();

  const operation = usePersonalContextStore.getState().setEnabled(false);
  storedConfig = { ...enabledConfig, collection_enabled: false };
  rejectStop(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);

  resolveStaleRead({ ...enabledConfig });
  await staleRead;
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
});

test('a service refresh does not discard an in-flight settings config read', async () => {
  storedConfig = { ...enabledConfig, strategy_profile: 'balanced' };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  let resolveConfig;
  deferredConfigRead = new Promise((resolve) => {
    resolveConfig = resolve;
  });
  const read = usePersonalContextStore.getState().loadConfig();
  await usePersonalContextStore.getState().batchRefresh();
  resolveConfig({ ...storedConfig });
  await read;

  assert.equal(usePersonalContextStore.getState().config.strategy_profile, 'balanced');
});

test('master switch timeout confirms all three persisted switch states', async () => {
  storedConfig = {
    ...enabledConfig,
    master_enabled: false,
    collection_enabled: false,
    agent_use_enabled: false,
  };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setMasterEnabled(false);
  rejectMaster(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(usePersonalContextStore.getState().config.master_enabled, false);
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, false);
  assert.equal(usePersonalContextStore.getState().config.agent_use_enabled, false);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
});

test('master switch timeout restores all three states when Host rolled back', async () => {
  storedConfig = { ...enabledConfig };
  configError = null;
  usePersonalContextStore.setState({
    config: { ...enabledConfig },
    pendingWrites: {},
    configNeedsReconciliation: false,
  });

  const operation = usePersonalContextStore.getState().setMasterEnabled(false);
  rejectMaster(new Error('request timeout'));
  await assert.rejects(operation, /request timeout/);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(usePersonalContextStore.getState().config.master_enabled, true);
  assert.equal(usePersonalContextStore.getState().config.collection_enabled, true);
  assert.equal(usePersonalContextStore.getState().config.agent_use_enabled, true);
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
});

test('a failed stop checks actual runtime and keeps switches blocked until STOPPING settles', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  storedConfig = { ...enabledConfig, master_enabled: false, collection_enabled: false, agent_use_enabled: false };
  runtimeState = 'STOPPING';
  configError = null;
  const readsBefore = statusReads;
  usePersonalContextStore.setState({ config: { ...enabledConfig }, pendingWrites: {}, configNeedsReconciliation: false });
  const operation = usePersonalContextStore.getState().setMasterEnabled(false);
  rejectMaster(new Error('stop timeout'));
  await assert.rejects(operation, /stop timeout/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(statusReads > readsBefore, 'stop failures must read runtime, not only persisted config');
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPING');
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);
  runtimeState = 'STOPPED';
  context.mock.timers.tick(5000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  context.mock.timers.reset();
});

test('a single-run stop failure refreshes actual runtime before clearing pending', async () => {
  runtimeState = 'STOPPING';
  const readsBefore = statusReads;
  globalThis.__pcApi.stopRun = async () => { throw new Error('stop request lost'); };
  usePersonalContextStore.setState({ pendingWrites: {}, configNeedsReconciliation: false });
  await assert.rejects(usePersonalContextStore.getState().stopRun('notes'), /stop request lost/);
  assert.ok(statusReads > readsBefore);
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPING');
  assert.equal(usePersonalContextStore.getState().pendingWrites['stop:notes'], undefined);
  runtimeState = 'STOPPED';
});

test('a remount config response cannot resurrect reconciliation after the retry confirmed STOPPED', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  storedConfig = { ...enabledConfig, collection_enabled: false };
  runtimeState = 'STOPPING';
  configError = null;
  usePersonalContextStore.setState({ config: { ...storedConfig }, pendingWrites: {}, configNeedsReconciliation: false });
  usePersonalContextStore.getState().reconcileConfig();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);

  let resolveOlderConfig;
  deferredConfigRead = new Promise((resolve) => { resolveOlderConfig = resolve; });
  const remountRead = usePersonalContextStore.getState().loadConfig();
  runtimeState = 'STOPPED';
  context.mock.timers.tick(5000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');

  resolveOlderConfig({ ...storedConfig });
  await remountRead;
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  await usePersonalContextStore.getState().loadStatus();
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  const readsAfterRecovery = configReads;
  context.mock.timers.tick(5000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(configReads, readsAfterRecovery, 'settled reconciliation has no outstanding retry');
  context.mock.timers.reset();
});

test('a newer batch reconciliation supersedes an older config read without reviving STOPPING', async () => {
  storedConfig = { ...enabledConfig, collection_enabled: false, strategy_profile: 'balanced' };
  runtimeState = 'STOPPING';
  usePersonalContextStore.setState({ config: { ...enabledConfig }, pendingWrites: {}, configNeedsReconciliation: true });
  let resolveOlderConfig;
  deferredConfigRead = new Promise((resolve) => { resolveOlderConfig = resolve; });
  const olderRead = usePersonalContextStore.getState().loadConfig();
  runtimeState = 'STOPPED';
  await usePersonalContextStore.getState().batchRefresh();
  resolveOlderConfig({ ...enabledConfig, collection_enabled: false });
  await olderRead;
  assert.equal(usePersonalContextStore.getState().config.strategy_profile, 'balanced');
  assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
  assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  assert.equal(usePersonalContextStore.getState().loadingConfig, false);
});

for (const read of ['loadConfig', 'loadStatus', 'batchRefresh']) {
  test(`a delayed ${read} status response cannot overwrite a newer status poll`, async () => {
    storedConfig = { ...enabledConfig, collection_enabled: false };
    runtimeState = 'STOPPED';
    usePersonalContextStore.setState({ config: { ...storedConfig }, pendingWrites: {}, configNeedsReconciliation: true });
    let resolveOlderStatus;
    deferredStatusRead = new Promise((resolve) => { resolveOlderStatus = resolve; });
    const olderRead = usePersonalContextStore.getState()[read]();
    await usePersonalContextStore.getState().loadStatus();
    resolveOlderStatus({ state: 'STOPPING', collection_enabled: false });
    await olderRead;
    assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
    await usePersonalContextStore.getState().loadConfig();
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  });
}

for (const read of ['loadStatus', 'batchRefresh']) {
  test(`${read} keeps applying six-second replies while five-second polling continues`, async (context) => {
    context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    const originalGetStatus = globalThis.__pcApi.getStatus;
    let requestCount = 0;
    globalThis.__pcApi.getStatus = () => {
      const pipeline_queue_size = ++requestCount;
      return new Promise((resolve) => setTimeout(() => resolve({ state: 'STOPPED', pipeline_queue_size }), 6000));
    };
    usePersonalContextStore.setState({ status: { state: 'STOPPING' }, pendingWrites: {}, configNeedsReconciliation: false });
    const pending = [usePersonalContextStore.getState()[read]()];
    const interval = setInterval(() => pending.push(usePersonalContextStore.getState()[read]()), 5000);
    try {
      context.mock.timers.tick(5000);
      context.mock.timers.tick(1000);
      await new Promise((resolve) => setImmediate(resolve));
      for (let applied = 1; applied <= 3; applied += 1) {
        assert.ok(requestCount > applied, 'a newer request is still in flight');
        assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
        assert.equal(usePersonalContextStore.getState().status.pipeline_queue_size, applied);
        if (applied < 3) {
          context.mock.timers.tick(4000);
          context.mock.timers.tick(1000);
          await new Promise((resolve) => setImmediate(resolve));
        }
      }
    } finally {
      clearInterval(interval);
      context.mock.timers.tick(6000);
      await Promise.all(pending);
      globalThis.__pcApi.getStatus = originalGetStatus;
      context.mock.timers.reset();
    }
  });
}

test('reconciliation finishes during ongoing five-second polling when status replies take six seconds', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const originalGetStatus = globalThis.__pcApi.getStatus;
  globalThis.__pcApi.getStatus = () => new Promise((resolve) => setTimeout(() => resolve({ state: 'STOPPED' }), 6000));
  storedConfig = { ...enabledConfig, collection_enabled: false };
  usePersonalContextStore.setState({ config: { ...storedConfig }, status: { state: 'STOPPING' }, pendingWrites: {}, configNeedsReconciliation: false });
  const readsBefore = configReads;
  usePersonalContextStore.getState().reconcileConfig();
  const pending = [];
  const interval = setInterval(() => pending.push(usePersonalContextStore.getState().loadStatus()), 5000);
  try {
    context.mock.timers.tick(5000);
    context.mock.timers.tick(1000);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(pending.length, 1, 'the next status poll remains in flight');
    assert.equal(usePersonalContextStore.getState().status.state, 'STOPPED');
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
    context.mock.timers.tick(10000);
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(pending.length >= 3, 'polling continues after reconciliation');
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
    assert.equal(configReads, readsBefore + 1, 'no further reconciliation retry is needed');
  } finally {
    clearInterval(interval);
    context.mock.timers.tick(6000);
    await Promise.all(pending);
    await new Promise((resolve) => setImmediate(resolve));
    globalThis.__pcApi.getStatus = originalGetStatus;
    context.mock.timers.reset();
  }
});
