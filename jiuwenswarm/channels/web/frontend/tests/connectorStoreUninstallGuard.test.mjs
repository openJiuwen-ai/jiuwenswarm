import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = new URL('..', import.meta.url);
let useConnectorStore;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/connector-store-uninstall-guard/', root));
  await mkdir(cacheDir, { recursive: true });
  const stubApi = {
    name: 'stub-connector-api',
    setup(pluginBuild) {
      pluginBuild.onResolve({ filter: /(^|[\\/])services[\\/]connectorApi$/ }, () => ({
        path: 'stub-connector-api',
        namespace: 'stub',
      }));
      pluginBuild.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
        contents: 'export const connectorApi = globalThis.__connectorApiStub;',
        loader: 'js',
      }));
    },
  };
  globalThis.__connectorApiStub = {
    uninstall: async () => {
      throw new Error('connectorApi.uninstall not initialized');
    },
  };
  await build({
    entryPoints: [
      { in: fileURLToPath(new URL('src/stores/connectorStore.ts', root)), out: 'connectorStore' },
    ],
    absWorkingDir: fileURLToPath(root),
    bundle: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    define: { 'import.meta.env.DEV': 'false' },
    outdir: cacheDir,
    plugins: [stubApi],
  });
  const store = await import(pathToFileURL(join(cacheDir, 'connectorStore.js')).href);
  useConnectorStore = store.useConnectorStore;
});

test('uninstallPackage ignores a second call while the first is in flight', async () => {
  const calls = [];
  let resolveFirst;
  useConnectorStore.setState({
    busyMap: {},
    connectors: [],
    builtinConnectors: [],
    myConnectors: [],
    detailCache: {},
    error: null,
    successMessage: null,
    noticeMessage: null,
    loadList: async () => {},
  });
  globalThis.__connectorApiStub.uninstall = (id) => {
    calls.push(id);
    return new Promise((resolve) => {
      resolveFirst = resolve;
    });
  };

  const first = useConnectorStore.getState().uninstallPackage('amap');
  const second = await useConnectorStore.getState().uninstallPackage('amap');

  assert.equal(second, false);
  assert.deepEqual(calls, ['amap']);
  assert.equal(useConnectorStore.getState().busyMap.amap, 'uninstall');

  resolveFirst({ type: 'uninstalled', item: { id: 'amap', name: 'amap', removed: true }, applied: true });
  assert.equal(await first, true);
  assert.equal(useConnectorStore.getState().busyMap.amap, undefined);
});
