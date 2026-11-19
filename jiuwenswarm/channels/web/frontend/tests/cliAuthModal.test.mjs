import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement, StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { createInstance } from 'i18next';
import { JSDOM, VirtualConsole } from 'jsdom';

const frontendRoot = new URL('..', import.meta.url);
let CliAuthModal;
let useConnectorStore;
let connectorApi;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/cli-auth-modal/', frontendRoot));
  await mkdir(cacheDir, { recursive: true });
  connectorApi = {};
  globalThis.__cliAuthConnectorApi = connectorApi;
  await build({
    entryPoints: [
      { in: fileURLToPath(new URL('src/components/ConnectorMarket/CliAuthModal.tsx', frontendRoot)), out: 'CliAuthModal' },
      { in: fileURLToPath(new URL('src/stores/connectorStore.ts', frontendRoot)), out: 'connectorStore' },
    ],
    absWorkingDir: fileURLToPath(frontendRoot),
    bundle: true,
    splitting: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    define: { 'import.meta.env.DEV': 'false' },
    outExtension: { '.js': '.mjs' },
    outdir: cacheDir,
    plugins: [{
      name: 'controlled-connector-api',
      setup(pluginBuild) {
        pluginBuild.onResolve({ filter: /services[\\/]connectorApi$/ }, () => ({
          path: 'controlled-connector-api',
          namespace: 'stub',
        }));
        pluginBuild.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
          contents: 'export const connectorApi = globalThis.__cliAuthConnectorApi;',
          loader: 'js',
        }));
      },
    }],
  });
  ({ CliAuthModal } = await import(pathToFileURL(join(cacheDir, 'CliAuthModal.mjs')).href));
  ({ useConnectorStore } = await import(pathToFileURL(join(cacheDir, 'connectorStore.mjs')).href));
});

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function mountModal({ strict = false, initial = {}, name = 'feishu' } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {
    url: 'http://localhost/',
    virtualConsole: new VirtualConsole(),
  });
  const previousGlobals = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const i18n = createInstance();
  await i18n.init({
    lng: 'en',
    fallbackLng: 'en',
    resources: {
      en: { translation: { connectorMarket: { cliAuth: { error: 'Authorization failed' } } } },
      zh: { translation: { connectorMarket: { cliAuth: { error: '授权失败' } } } },
    },
  });
  const calls = { opened: [], waits: [], starts: [], cancelled: [], connected: [], renders: 0 };
  const waits = [];
  const starts = [];
  window.open = (...args) => {
    calls.opened.push(args);
    return null;
  };
  connectorApi.waitAuth = (...args) => {
    calls.waits.push(args);
    const request = deferred();
    waits.push(request);
    return request.promise;
  };
  connectorApi.startOAuth = (...args) => {
    calls.starts.push(args);
    const request = deferred();
    starts.push(request);
    return request.promise;
  };
  connectorApi.cancelConnect = async (...args) => {
    calls.cancelled.push(args);
    return { type: 'cancelled' };
  };
  const connector = {
    id: name,
    name,
    runtimePackageName: name,
    displayName: name,
    description: '',
    category: '',
    integrationType: 'cli',
    connectionState: 'disconnected',
    hasBundledSkills: false,
    source: 'built_in',
    installed: true,
  };
  useConnectorStore.setState({
    connectors: [connector],
    builtinConnectors: [connector],
    myConnectors: [],
    busyMap: {},
    detailCache: {},
    error: null,
    loadList: async () => {},
  });
  const response = { type: 'auth_required', authUrl: 'https://example.com/cli-auth', stepIndex: 0, ...initial };
  const root = createRoot(document.getElementById('root'));
  let callbackVersion = 'initial';
  let unmounted = false;

  function Parent() {
    useConnectorStore((state) => state.connectors);
    calls.renders += 1;
    const currentVersion = callbackVersion;
    return createElement(CliAuthModal, {
      name,
      initial: response,
      onCancel: () => {},
      onConnected: () => calls.connected.push(currentVersion),
    });
  }

  async function close() {
    if (!unmounted) {
      await act(async () => root.unmount());
      unmounted = true;
    }
    dom.window.close();
    for (const [key, descriptor] of previousGlobals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }

  try {
    await act(async () => root.render(createElement(
      I18nextProvider,
      { i18n },
      strict ? createElement(StrictMode, null, createElement(Parent)) : createElement(Parent),
    )));
  } catch (error) {
    await close();
    throw error;
  }

  return {
    calls,
    waits,
    starts,
    i18n,
    close,
    async refresh(version = 'latest') {
      callbackVersion = version;
      await act(async () => useConnectorStore.setState((state) => ({ connectors: [...state.connectors] })));
    },
    async settle(request, result) {
      await act(async () => request.resolve(result));
    },
    async click(testId) {
      const button = document.querySelector(`[data-testid="${testId}"]`);
      assert.ok(button, `Expected ${testId}`);
      await act(async () => button.click());
    },
    async unmount() {
      await act(async () => root.unmount());
      unmounted = true;
    },
  };
}

test('CLI waiting survives subscribed parent renders with one popup and one real store wait', async () => {
  const view = await mountModal();
  try {
    for (let index = 0; index < 5; index += 1) await view.refresh(`render-${index}`);
    assert.ok(view.calls.renders >= 6);
    assert.deepEqual(view.calls.waits, [['feishu', 0]]);
    assert.deepEqual(view.calls.opened, [['https://example.com/cli-auth', '_blank', 'noopener,noreferrer']]);
    assert.equal(useConnectorStore.getState().busyMap.feishu, 'connect');
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['render-4']);
    assert.equal(useConnectorStore.getState().connectors[0].connectionState, 'connected');
  } finally {
    await view.close();
  }
});

test('StrictMode reuses the CLI request and still delivers completion once', async () => {
  const view = await mountModal({ strict: true });
  try {
    assert.equal(view.calls.opened.length, 1);
    assert.equal(view.calls.waits.length, 1);
    await view.refresh();
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['latest']);
  } finally {
    await view.close();
  }
});

test('CLI without an auth URL waits without automatically opening a window', async () => {
  const view = await mountModal({ initial: { authUrl: null } });
  try {
    await view.refresh();
    assert.equal(view.calls.opened.length, 0);
    assert.equal(view.calls.waits.length, 1);
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['latest']);
  } finally {
    await view.close();
  }
});

test('multi-step CLI opens and waits once per step, including a reused URL', async () => {
  const view = await mountModal({ initial: { stepsTotal: 2 } });
  try {
    await view.settle(view.waits[0], {
      type: 'auth_required',
      authUrl: 'https://example.com/cli-auth',
      stepIndex: 1,
      stepsTotal: 2,
    });
    await view.refresh();
    assert.deepEqual(view.calls.waits, [['feishu', 0], ['feishu', 1]]);
    assert.equal(view.calls.opened.length, 2);
    assert.ok(document.querySelector('[data-testid="connector-market-cli-auth-modal-step-done"]'));
    await view.settle(view.waits[1], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['latest']);
  } finally {
    await view.close();
  }
});

for (const strict of [false, true]) {
  for (const authUrl of ['https://example.com/cli-auth', 'https://example.com/refreshed-auth', null]) {
    test(`same-step CLI responses continue waiting with URL ${authUrl} and StrictMode ${strict}`, async () => {
      const view = await mountModal({ strict });
      try {
        const response = { type: 'auth_required', stepIndex: 0, authUrl };
        // Reuse the exact response object to verify each completed wait starts a new round.
        for (let round = 0; round < 2; round += 1) {
          await view.settle(view.waits[round], response);
          await view.refresh();
          assert.equal(view.calls.waits.length, round + 2);
          assert.deepEqual(view.calls.waits[round + 1], ['feishu', 0]);
          assert.equal(useConnectorStore.getState().busyMap.feishu, 'connect');
        }
        assert.equal(view.calls.opened.length, authUrl === 'https://example.com/refreshed-auth' ? 2 : 1);
        assert.equal(document.querySelector('[data-testid="connector-market-cli-auth-modal-step-done"]'), null);
        await view.settle(view.waits[2], { type: 'connected' });
        assert.deepEqual(view.calls.connected, ['latest']);
      } finally {
        await view.close();
      }
    });
  }
}

test('retry starts a fresh CLI wait and reopens the URL while manual opening remains available', async () => {
  const view = await mountModal();
  try {
    await view.settle(view.waits[0], { type: 'auth_failed' });
    await view.refresh();
    assert.equal(view.calls.waits.length, 1);
    await view.click('connector-market-cli-auth-modal-retry');
    assert.equal(view.calls.waits.length, 2);
    assert.equal(view.calls.opened.length, 2);
    await view.refresh();
    assert.equal(view.calls.opened.length, 2);
    await view.click('connector-market-cli-auth-modal-open-link');
    assert.equal(view.calls.opened.length, 3);
    await view.settle(view.waits[1], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['latest']);
  } finally {
    await view.close();
  }
});

test('language changes preserve the pending wait and use the latest error translation', async () => {
  const view = await mountModal();
  try {
    await act(async () => view.i18n.changeLanguage('zh'));
    assert.equal(view.calls.waits.length, 1);
    assert.equal(view.calls.opened.length, 1);
    await view.settle(view.waits[0], { type: 'auth_failed' });
    assert.equal(document.querySelector('[data-testid="connector-market-cli-auth-modal-error"]').textContent, '授权失败');
  } finally {
    await view.close();
  }
});

test('a rejected CLI RPC shows an error and allows an explicit retry', async () => {
  const view = await mountModal();
  try {
    // Resolve with a rejected promise to exercise the real store error path.
    await view.settle(view.waits[0], Promise.reject(new Error('CLI unavailable')));
    assert.ok(document.querySelector('[data-testid="connector-market-cli-auth-modal-error"]'));
    assert.equal(useConnectorStore.getState().connectors[0].connectionState, 'error');
    assert.equal(useConnectorStore.getState().busyMap.feishu, undefined);
    await view.click('connector-market-cli-auth-modal-retry');
    assert.equal(view.calls.waits.length, 2);
    assert.equal(view.calls.opened.length, 2);
    await view.settle(view.waits[1], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['initial']);
  } finally {
    await view.close();
  }
});

test('cancelling invalidates a late successful response before the parent unmounts', async () => {
  const view = await mountModal();
  try {
    await view.click('connector-market-cli-auth-modal-cancel');
    assert.deepEqual(view.calls.cancelled, [['feishu']]);
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, []);
  } finally {
    await view.close();
  }
});

test('unmounting ignores a late CLI success', async () => {
  const view = await mountModal();
  try {
    await view.unmount();
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, []);
  } finally {
    await view.close();
  }
});

test('StrictMode host OAuth starts once and completes with the latest callback', async () => {
  const view = await mountModal({ strict: true, name: 'canva', initial: { hostOAuth: true, authUrl: null } });
  try {
    assert.deepEqual(view.calls.starts, [['canva']]);
    await view.refresh();
    await view.settle(view.starts[0], { flow: 'flow-1', authUrl: 'https://example.com/host-auth', stepIndex: 0 });
    await view.refresh('connected');
    assert.deepEqual(view.calls.waits, [['canva', 0]]);
    assert.deepEqual(view.calls.opened, [['https://example.com/host-auth', '_blank', 'noopener,noreferrer']]);
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['connected']);
  } finally {
    await view.close();
  }
});

test('host OAuth startup failure can retry with a fresh flow', async () => {
  const view = await mountModal({ name: 'canva', initial: { hostOAuth: true, authUrl: null } });
  try {
    await view.settle(view.starts[0], null);
    await view.click('connector-market-cli-auth-modal-retry');
    assert.equal(view.calls.starts.length, 2);
    await view.settle(view.starts[1], { flow: 'flow-2', authUrl: 'https://example.com/host-auth', stepIndex: 0 });
    await view.settle(view.waits[0], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['initial']);
  } finally {
    await view.close();
  }
});

test('host OAuth treats auth_required from wait as an error and retries a fresh flow', async () => {
  const view = await mountModal({ strict: true, name: 'canva', initial: { hostOAuth: true, authUrl: null } });
  try {
    const authUrl = 'https://example.com/host-auth';
    await view.settle(view.starts[0], { flow: 'flow-1', authUrl, stepIndex: 0 });
    await view.settle(view.waits[0], { type: 'auth_required', authUrl, stepIndex: 0 });
    await view.refresh();
    assert.ok(document.querySelector('[data-testid="connector-market-cli-auth-modal-error"]'));
    assert.equal(view.calls.starts.length, 1);
    assert.equal(view.calls.waits.length, 1);
    assert.equal(view.calls.opened.length, 1);
    assert.deepEqual(view.calls.connected, []);

    await view.click('connector-market-cli-auth-modal-retry');
    assert.equal(view.calls.starts.length, 2);
    await view.settle(view.starts[1], { flow: 'flow-2', authUrl, stepIndex: 0 });
    assert.deepEqual(view.calls.waits, [['canva', 0], ['canva', 0]]);
    assert.equal(view.calls.opened.length, 2);
    await view.refresh('retried');
    await view.settle(view.waits[1], { type: 'connected' });
    assert.deepEqual(view.calls.connected, ['retried']);
  } finally {
    await view.close();
  }
});
