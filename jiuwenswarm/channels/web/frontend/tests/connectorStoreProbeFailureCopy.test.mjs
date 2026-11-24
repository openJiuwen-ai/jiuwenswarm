// JW-79 review P2 (PR !10905): the MCP_UNREACHABLE code is the backend's
// catch-all for "credentials not proven rejected" — it covers not only network
// failures but also local environment problems (stdio command missing from
// PATH, invalid URL). A fixed "service unreachable" message misreports those,
// so the store must append the backend reason detail when one is present.
import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const root = new URL('..', import.meta.url);
let useConnectorStore;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/connector-store-probe-failure-copy/', root));
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
    connect: async () => {
      throw new Error('connectorApi.connect not initialized');
    },
    saveCredentials: async () => {
      throw new Error('connectorApi.saveCredentials not initialized');
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

function resetState() {
  useConnectorStore.setState({
    busyMap: {},
    connectors: [],
    builtinConnectors: [],
    myConnectors: [],
    detailCache: {},
    error: null,
    successMessage: null,
    noticeMessage: null,
  });
}

// scheduleQuickRefresh uses window.setTimeout — stub the minimal surface for node.
globalThis.window = globalThis.window ?? { setTimeout: () => 0, clearTimeout: () => {} };

function webError(message, code) {
  const error = new Error(message);
  error.code = code;
  return error;
}

test('saveCredentialsAndConnect appends backend detail to the unreachable copy', async () => {
  resetState();
  globalThis.__connectorApiStub.saveCredentials = async () => ({});
  globalThis.__connectorApiStub.connect = async () => {
    throw webError("command 'npx' not found on PATH (install it or add to PATH)", 'MCP_UNREACHABLE');
  };
  await useConnectorStore.getState().saveCredentialsAndConnect('demo', { TOKEN: 'x' });
  const error = useConnectorStore.getState().error;
  assert.ok(error.includes('服务暂不可达'), `expected generic unreachable copy, got: ${error}`);
  assert.ok(error.includes("command 'npx' not found on PATH"), `expected backend detail, got: ${error}`);
});

test('saveCredentialsAndConnect keeps the plain copy when no detail is present', async () => {
  resetState();
  globalThis.__connectorApiStub.saveCredentials = async () => ({});
  globalThis.__connectorApiStub.connect = async () => {
    throw webError('MCP live-connect probe failed', 'MCP_UNREACHABLE');
  };
  await useConnectorStore.getState().saveCredentialsAndConnect('demo', { TOKEN: 'x' });
  const error = useConnectorStore.getState().error;
  assert.equal(error, '服务暂不可达，请稍后重试；已保存的凭据会保留');
});

test('auth-rejected code still gets the re-enter-token copy without detail', async () => {
  resetState();
  globalThis.__connectorApiStub.saveCredentials = async () => ({});
  globalThis.__connectorApiStub.connect = async () => {
    throw webError('auth rejected (HTTP 401)', 'MCP_CREDENTIALS_INVALID');
  };
  await useConnectorStore.getState().saveCredentialsAndConnect('demo', { TOKEN: 'x' });
  assert.equal(useConnectorStore.getState().error, '服务拒绝了已保存的凭据，请重新填写 Token 后重试');
});
