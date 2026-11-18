import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = new URL('..', import.meta.url);
let useConnectorStore;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/connector-store-silent-refresh/', root));
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
    list: async () => {
      throw new Error('connectorApi.list not initialized');
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

function summary(overrides = {}) {
  return {
    id: 'amap',
    name: 'amap',
    runtimePackageName: 'amap',
    displayName: '高德地图',
    description: '地图服务',
    category: '出行',
    integrationType: 'remote-mcp',
    connectionState: 'connected',
    hasBundledSkills: false,
    icon: '/file-api/download?token=abc&inline=1',
    source: 'built_in',
    installed: true,
    version: '1.0.0',
    ...overrides,
  };
}

test('silent refresh keeps the previous arrays when the catalog is unchanged', async () => {
  useConnectorStore.setState({
    builtinConnectors: [],
    myConnectors: [],
    connectors: [],
    detailCache: {},
    busyMap: {},
    isLoading: false,
    error: null,
    successMessage: null,
    noticeMessage: null,
  });
  globalThis.__connectorApiStub.list = async () => [summary()];

  await useConnectorStore.getState().loadList('builtin');
  const firstList = useConnectorStore.getState().builtinConnectors;
  const firstUnion = useConnectorStore.getState().connectors;
  assert.equal(firstList.length, 1);

  // Same payload on a silent poll: keep array identity so the grid does not re-render.
  await useConnectorStore.getState().loadList('builtin', { silent: true });
  assert.equal(useConnectorStore.getState().builtinConnectors, firstList);
  assert.equal(useConnectorStore.getState().connectors, firstUnion);

  // Changed payload still replaces the list.
  globalThis.__connectorApiStub.list = async () => [summary({ connectionState: 'disconnected' })];
  await useConnectorStore.getState().loadList('builtin', { silent: true });
  const replaced = useConnectorStore.getState().builtinConnectors;
  assert.notEqual(replaced, firstList);
  assert.equal(replaced[0].connectionState, 'disconnected');
  assert.equal(useConnectorStore.getState().isLoading, false);
});

test('silent refresh replaces the list when only the cache metadata changes', async () => {
  useConnectorStore.setState({
    builtinConnectors: [],
    myConnectors: [],
    connectors: [],
    detailCache: {},
    busyMap: {},
    isLoading: false,
    error: null,
    successMessage: null,
    noticeMessage: null,
  });
  const withCache = (cache) => Object.assign([summary()], { cache });

  globalThis.__connectorApiStub.list = async () => withCache({ state: 'stale', refreshing: true });
  await useConnectorStore.getState().loadList('builtin', { silent: true });
  const staleList = useConnectorStore.getState().builtinConnectors;
  assert.equal(staleList.cache.state, 'stale');

  // Cards are identical, but the refresh finished: the notice needs the new metadata.
  globalThis.__connectorApiStub.list = async () => withCache({ state: 'fresh', refreshing: false });
  await useConnectorStore.getState().loadList('builtin', { silent: true });
  const freshList = useConnectorStore.getState().builtinConnectors;
  assert.notEqual(freshList, staleList);
  assert.equal(freshList.cache.state, 'fresh');
});
