import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

let ConnectTokenModal;
let CliAuthModal;
before(async () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const dir = `${root}node_modules/.cache/remote-oauth-modal`;
  await mkdir(dir, { recursive: true });
  await build({
    stdin: { contents: "export { ConnectTokenModal } from './src/components/ConnectorMarket/ConnectTokenModal'; export { CliAuthModal } from './src/components/ConnectorMarket/CliAuthModal';", resolveDir: root, loader: 'tsx' },
    outfile: `${dir}/modal.mjs`, bundle: true, packages: 'external', platform: 'node', format: 'esm',
    plugins: [{ name: 'test-boundaries', setup(b) {
      b.onResolve({ filter: /stores\/connectorStore$/ }, () => ({ path: 'store', namespace: 'mock' }));
      b.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'mock' }));
      b.onResolve({ filter: /^\/logo.svg$/ }, () => ({ path: 'logo', namespace: 'mock' }));
      b.onResolve({ filter: /EntityAvatar$/ }, () => ({ path: 'avatar', namespace: 'mock' }));
      b.onLoad({ filter: /.*/, namespace: 'mock' }, ({ path }) => ({ loader: 'js', contents: {
        store: 'export const useConnectorStore = selector => selector(globalThis.__oauthStore); useConnectorStore.getState = () => globalThis.__oauthStore;',
        i18n: 'export const useTranslation = () => ({t: key => key});',
        logo: 'export default "data:image/svg+xml,<svg/>";',
        avatar: 'export const EntityAvatar = () => null;',
      }[path] }));
    }}],
  });
  ({ ConnectTokenModal, CliAuthModal } = await import(pathToFileURL(`${dir}/modal.mjs`)));
});

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

async function mount(available = true, cli = false) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  const previous = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document,
    navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const calls = { connect: [], wait: [], cancel: [], cancelConnect: [], open: [], connected: 0, closed: 0 };
  const waiting = deferred();
  window.open = (...args) => { calls.open.push(args); };
  globalThis.__oauthStore = {
    connect: async (...args) => { calls.connect.push(args); return { type: 'auth_required', oauthSession: 'session-1', authUrl: 'https://agent.qcc.com/oauth/authorize?state=test', stepIndex: 0 }; },
    waitAuth: (...args) => { calls.wait.push(args); return waiting.promise; },
    cancelConnectAction: async (...args) => { calls.cancelConnect.push(args); },
    cancelOAuth: async (...args) => { calls.cancel.push(args); },
    saveCredentialsAndConnect: async () => ({ type: 'connected' }),
  };
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(cli ? CliAuthModal : ConnectTokenModal, {
    initial: { type: 'auth_required', authUrl: 'https://example.com/auth', stepIndex: 0 },
    name: 'qcc-company', displayName: '企查查',
    response: { type: 'credentials_required', oauthAvailable: available, requiredTokens: ['QICHACHA_API_KEY'], fields: { QICHACHA_API_KEY: { type: 'password' } } },
    onConnected: () => { calls.connected++; }, onCancel: () => { calls.closed++; },
  })));
  const byId = id => document.querySelector(`[data-testid="connector-market-${id}"]`);
  return { calls, waiting, byId,
    click: async id => { await act(async () => byId(id).click()); },
    cleanup: async () => {
      await act(async () => root.unmount());
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
      delete globalThis.__oauthStore;
      dom.window.close();
    },
  };
}

test('OAuth is recommended, waits for the server probe, and carries its session ID', async () => {
  const view = await mount();
  try {
    assert.ok(view.byId('oauth-connect'));
    assert.equal(view.byId('token-modal-field'), null);
    await view.click('oauth-connect');
    assert.deepEqual(view.calls.connect, [['qcc-company', 'oauth']]);
    assert.deepEqual(view.calls.wait, [['qcc-company', 0, 'session-1']]);
    assert.equal(view.calls.open.length, 1);
    assert.equal(view.calls.connected, 0);
    await act(async () => view.waiting.resolve({ type: 'connected' }));
    assert.equal(view.calls.connected, 1);
  } finally { await view.cleanup(); }
  assert.equal(view.calls.cancel.length, 0);
});

test('API Key fallback remains available and other connectors keep their original form', async () => {
  let view = await mount();
  try {
    await view.click('oauth-use-api-key');
    assert.equal(view.byId('token-modal-field').type, 'password');
    assert.ok(view.byId('token-modal-submit').disabled);
    await view.click('oauth-use-oauth');
    assert.ok(view.byId('oauth-connect'));
  } finally { await view.cleanup(); }
  view = await mount(false);
  try {
    assert.ok(view.byId('token-modal-field'));
    assert.equal(view.byId('oauth-connect'), null);
  } finally { await view.cleanup(); }
});

for (const button of ['cli-auth-modal-close', 'cli-auth-modal-cancel']) {
test(`cancelling authorization via ${button} ignores late completion`, async () => {
  const view = await mount();
  try {
    await view.click('oauth-connect');
    await view.click(button);
    assert.deepEqual(view.calls.cancel, [['qcc-company', 'session-1']]);
    assert.ok(view.byId('oauth-connect'));
    await act(async () => view.waiting.resolve({ type: 'connected' }));
    assert.equal(view.calls.connected, 0);
  } finally { await view.cleanup(); }
});

}

test('navigating away cancels a pending remote authorization', async () => {
  const view = await mount();
  await view.click('oauth-connect');
  await view.cleanup();
  assert.deepEqual(view.calls.cancel, [['qcc-company', 'session-1']]);
});


test('ordinary CLI authorization keeps the upstream cancel path and ignores late completion', async () => {
  const view = await mount(false, true);
  try {
    await view.click('cli-auth-modal-cancel');
    assert.deepEqual(view.calls.cancelConnect, [['qcc-company']]);
    assert.deepEqual(view.calls.cancel, []);
    await act(async () => view.waiting.resolve({ type: 'connected' }));
    assert.equal(view.calls.connected, 0);
    assert.equal(view.calls.closed, 1);
  } finally { await view.cleanup(); }
});
