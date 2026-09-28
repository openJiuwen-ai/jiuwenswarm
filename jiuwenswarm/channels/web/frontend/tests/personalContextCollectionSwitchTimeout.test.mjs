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
  getStatus: async () => ({ state: 'STOPPED', collection_enabled: false }),
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
