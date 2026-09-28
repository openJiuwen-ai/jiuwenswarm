import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const service = {
  service_id: 'notes',
  provider: 'local_files',
  enabled: false,
  interval_seconds: 3600,
};
const oldRun = { run_id: 'old', run_state: 'succeeded' };
const newRun = { run_id: 'new', run_state: 'partial_succeeded' };
globalThis.__pcApi = {
  runOne: async () => {},
  listServices: async () => ({ services: [service] }),
  getStatus: async () => ({ fetch_run_progress: { notes: newRun } }),
  getRunStatus: async () => ({ services: [{ service_id: 'notes', runs: [newRun] }] }),
};

const bundle = await build({
  entryPoints: [fileURLToPath(new URL('../src/stores/personalContextStore.ts', import.meta.url))],
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

test('manual run refreshes terminal history before clearing pending state', async () => {
  usePersonalContextStore.setState({
    config: { fetch_services: [service] },
    runHistories: { notes: [oldRun] },
    pendingWrites: {},
  });

  await usePersonalContextStore.getState().runOne('notes');

  assert.deepEqual(usePersonalContextStore.getState().runHistories.notes, [newRun]);
  assert.equal(usePersonalContextStore.getState().pendingWrites['run:notes'], undefined);
});
