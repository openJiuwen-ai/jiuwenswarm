import test from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
await build({
  entryPoints: ['src/stores/pluginPackageStore.ts'], bundle: true, packages: 'external',
  platform: 'node', format: 'esm', outfile: 'node_modules/.cache/plugin-install-test.mjs',
  plugins: [{ name: 'install-test-api', setup(builder) {
    builder.onResolve({ filter: /services\/pluginPackagesApi$/ }, () => ({ path: 'api', namespace: 'mock' }));
    builder.onResolve({ filter: /features\/catalogCache$/ }, () => ({ path: 'cache', namespace: 'mock' }));
    builder.onLoad({ filter: /.*/, namespace: 'mock' }, ({ path }) => ({ contents: path === 'api'
      ? `export class PluginInstallPendingError extends Error {}; export const pluginPackagesApi = { install: id => globalThis.installRequests(id) };`
      : `export const scheduleCatalogRefresh = () => {}; export const catalogScope = () => '';`, loader: 'js' }));
  }}],
});
globalThis.window = { setTimeout: () => 1, clearTimeout: () => {} };
const { usePluginPackageStore: store } = await import('../node_modules/.cache/plugin-install-test.mjs');
test('concurrent plugins settle independently and duplicate clicks are ignored', async () => {
  const pending = new Map();
  globalThis.installRequests = id => new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
  const first = store.getState().install('first');
  const second = store.getState().install('second');
  await store.getState().install('first');
  assert.equal(pending.size, 2);
  assert.equal(store.getState().installingIds.first, true);
  assert.equal(store.getState().installingIds.second, true);
  pending.get('first').reject(new Error('安装超时'));
  await first;
  assert.equal(store.getState().installingIds.first, false);
  assert.equal(store.getState().installingIds.second, true);
  pending.get('second').resolve();
  await second;
  assert.equal(store.getState().installingIds.second, false);
  assert.equal(store.getState().installed.second, true);
});
