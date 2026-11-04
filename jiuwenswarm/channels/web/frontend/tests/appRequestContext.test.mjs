import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

// Execute App's actual callbacks and effects with controlled request boundaries.
const source = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
function extract(startMarker, endMarker, includeEnd = false) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, `App block exists: ${startMarker}`);
  return source.slice(start, end + (includeEnd ? endMarker.length : 0));
}
function callback(name, dependencies) {
  const marker = `const ${name} = useCallback(`;
  return extract(marker, `}, [${dependencies}]);`).slice(marker.length) + '}';
}
const metadataDependencies = [
  'request', 'sessionIdRef', 'useChatStore', 'useSessionStore', 'useWorkspaceStore',
  'navigate', 'upsertSessionMetadata', 'setProcessing', 'setThinking', 'restoreSessionEquipment',
  'setMissingSessionId', 'parseModelSelection', 'normalizeTeamLeaderIdentity', 'normalizeTeamDispatchMode', 'console',
];
await build({
  stdin: {
    contents: `
      import { useEffect, useRef, useSyncExternalStore } from 'react';
      export { createClientCapabilityHost } from './src/features/webui/client/createClientCapabilityHost';
      let browserClientCapabilities;
      export function setCapabilities(value) { browserClientCapabilities = value; }
      ${extract('function getClientRequestContextSnapshot()', '\nfunction AppContent(')}
      export function createMetadataFetch({ ${metadataDependencies.join(', ')} }) {
        return ${callback('fetchSessionMetadata', 'navigate, request, setProcessing, setThinking, upsertSessionMetadata')};
      }
      export function createMetadataLoader(fetchSessionMetadata, sessionMetadataRequestsRef) {
        return ${callback('loadSessionMetadata', 'fetchSessionMetadata')};
      }
      export function RecoveryProbe({ isConnected, fetchConfig, setInitialDataLoaded,
        routeSessionId, loadSessionMetadata, setMissingSessionId }) {
        ${extract('const clientRequestContext = useSyncExternalStore(',
          'const clientRequestContextReady = !browserClientCapabilities.isChanging() && !browserClientCapabilities.isUnavailable();', true)}
        ${extract('const configInitializationRef = useRef<',
          '}, [clientRequestContext, clientRequestContextReady, fetchConfig, isConnected]);', true)}
        ${extract('useEffect(() => {\n    setMissingSessionId(null);\n    if (!isConnected || !routeSessionId || !clientRequestContextReady) return;',
          '}, [clientRequestContext, clientRequestContextReady, isConnected, loadSessionMetadata, routeSessionId]);', true)}
        return null;
      }
    `,
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/app-request-context.mjs',
});
const { createClientCapabilityHost, setCapabilities, createMetadataFetch, createMetadataLoader, RecoveryProbe } =
  await import('../node_modules/.cache/app-request-context.mjs');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const snapshot = {
  clientProtocolVersion: 1, gatewayInstanceId: 'gateway', connectionEpoch: 'connection', packages: [{
    packageId: 'example', activationGeneration: 'generation',
    requestBindings: [{ type: 'websocket', source: { key: 'record' },
      target: { location: 'message', path: ['request_context', 'credential'] } }],
  }],
};
async function fixture() {
  const records = new Map();
  const host = createClientCapabilityHost({
    storage: () => ({ getItem: (key) => records.get(key) ?? null,
      setItem: (key, value) => records.set(key, value), removeItem: (key) => records.delete(key) }),
    gatewayUrl: () => 'http://gateway.test/',
  });
  host.configure(snapshot);
  await Promise.resolve();
  setCapabilities(host);
  const requests = [];
  const writes = [];
  let missing = null;
  const setMissingSessionId = (value) => { missing = typeof value === 'function' ? value(missing) : value; };
  const dependencies = {
    request: (method, params) => {
      assert.equal(method, 'session.get_metadata');
      const result = deferred(); requests.push({ ...result, params }); return result.promise;
    },
    sessionIdRef: { current: 'saved-session' },
    useChatStore: { getState: () => ({ beginQueuedSessionMessageSnapshot: () => ({}),
      setCanContinueQueuedSessionMessages: () => {} }) },
    useSessionStore: { getState: () => ({ setTeamLeaderIdentity: () => {} }) },
    useWorkspaceStore: { getState: () => ({ upsertSession: (session) => writes.push(session) }) },
    navigate: () => {}, upsertSessionMetadata: () => {}, setProcessing: () => {}, setThinking: () => {},
    restoreSessionEquipment: () => {}, setMissingSessionId, parseModelSelection: () => null,
    normalizeTeamLeaderIdentity: (value) => value, normalizeTeamDispatchMode: () => null,
    console: { warn: () => {} },
  };
  const fetch = createMetadataFetch(dependencies);
  const pending = { current: new Map() };
  const load = createMetadataLoader(fetch, pending);
  return { host, requests, writes, fetch, load, pending, setMissingSessionId, missing: () => missing };
}

test('only an explicit metadata NOT_FOUND response marks a conversation missing', async () => {
  const app = await fixture();
  for (const code of ['AUTH_REQUIRED', 'FORBIDDEN', 'CONTEXT_CHANGED', 'WS_DISCONNECTED', 'REQUEST_TIMEOUT', 'INTERNAL_ERROR', undefined]) {
    const loading = app.fetch('saved-session');
    app.requests.at(-1).reject(Object.assign(new Error('session not found'), { code }));
    assert.equal(await loading, null);
    assert.equal(app.missing(), null, `do not invent a missing session for ${code}`);
  }
  const loading = app.fetch('saved-session');
  app.requests.at(-1).reject(Object.assign(new Error('session not found'), { code: 'NOT_FOUND' }));
  assert.equal(await loading, null);
  assert.equal(app.missing(), 'saved-session');
});

test('a new request context loads metadata independently and ignores the old failure', async () => {
  const app = await fixture();
  const first = app.load('saved-session');
  assert.equal(app.load('saved-session'), first);
  app.host.configure({ ...snapshot, connectionEpoch: 'next' });
  await Promise.resolve();
  const second = app.load('saved-session');
  assert.notEqual(second, first);
  app.requests[0].reject(Object.assign(new Error('obsolete result'), { code: 'NOT_FOUND' }));
  await first;
  assert.equal(app.missing(), null);
  assert.equal(app.pending.current.size, 1);
  const session = { session_id: 'saved-session' };
  app.requests[1].resolve(session);
  assert.deepEqual(await second, session);
  assert.deepEqual(app.writes, [session]);
  assert.equal(app.pending.current.size, 0);
});

test('late metadata success from an earlier context cannot update the current session', async () => {
  const app = await fixture();
  const loading = app.fetch('saved-session');
  app.host.configure({ ...snapshot, connectionEpoch: 'next' });
  app.requests[0].resolve({ session_id: 'saved-session' });
  assert.equal(await loading, null);
  assert.deepEqual(app.writes, []);
});

test('App rereads configuration and route metadata once the new context is ready', async () => {
  const app = await fixture();
  const dom = new JSDOM('<div id="root"></div>');
  const names = { window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    IS_REACT_ACT_ENVIRONMENT: true };
  const previous = new Map(Object.keys(names).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  for (const [name, value] of Object.entries(names)) Object.defineProperty(globalThis, name, { configurable: true, value });
  const root = createRoot(document.getElementById('root'));
  const reads = [];
  const loaded = [];
  const metadataReads = [];
  const props = { isConnected: true, routeSessionId: 'saved-session', setMissingSessionId: app.setMissingSessionId,
    fetchConfig: () => { const next = deferred(); reads.push(next); return next.promise; },
    setInitialDataLoaded: (value) => loaded.push(value),
    loadSessionMetadata: (sid) => { metadataReads.push(sid); return Promise.resolve(null); } };
  try {
    await act(async () => root.render(createElement(RecoveryProbe, props)));
    assert.equal(reads.length, 1);
    assert.deepEqual(metadataReads, ['saved-session']);
    app.setMissingSessionId('saved-session');
    const receipt = deferred();
    app.host.setInvocationWaiter(() => receipt.promise);
    await act(async () => app.host.acceptOperation({ clientProtocolVersion: 1,
      invocation_id: 'invocation', connection_epoch: 'connection', package_id: 'example',
      activation_generation: 'generation', method: 'local_storage.set', params: { key: 'record', value: 'new value' },
      deadline: Date.now() + 30_000 }));
    assert.equal(app.host.isChanging(), true);
    assert.equal(app.missing(), null);
    assert.equal(reads.length, 1, 'do not fetch using a pending context');
    await act(async () => receipt.resolve());
    assert.equal(reads.length, 2);
    assert.deepEqual(metadataReads, ['saved-session', 'saved-session']);
    await act(async () => root.render(createElement(RecoveryProbe, { ...props, fetchConfig: props.fetchConfig.bind(null) })));
    assert.equal(reads.length, 2, 'callback updates share the initialization already in flight');
    await act(async () => reads[0].resolve());
    assert.deepEqual(loaded, [], 'an obsolete initialization cannot mark the new one complete');
    await act(async () => reads[1].resolve());
    assert.deepEqual(loaded, [true]);
    await act(async () => root.render(createElement(RecoveryProbe, props)));
    assert.equal(reads.length, 2, 'retain completed initialization within the same context');
  } finally {
    await act(async () => root.unmount());
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name];
    }
    dom.window.close();
  }
});
