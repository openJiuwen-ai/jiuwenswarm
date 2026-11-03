import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, hmr: false, watch: null },
});
let runtime;
let session;
try {
  runtime = await vite.ssrLoadModule('/src/services/runtimeScope.ts');
  session = await vite.ssrLoadModule('/src/auth/manager/authSession.ts');
} finally {
  await vite.close();
}

function withStorage(run) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const values = new Map();
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
  });
  try {
    values.set('openjiuwen_access_token', 'platform');
    values.set('openjiuwen_federation', JSON.stringify({
      user_id: 'user-a', access_token: 'customer', token_type: 'Bearer',
    }));
    run(values);
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original);
    else delete globalThis.localStorage;
  }
}

test('chat carries customer identity separately from platform authorization', () => {
  withStorage(() => {
    const headers = runtime.buildRuntimeIdentityHeaders('req', {}, { userId: 'user-a' });
    assert.equal(headers.Authorization, 'Bearer platform');
    assert.equal(headers['X-User-Token'], 'Bearer customer');
  });
});

test('a different user never receives the previous customer token', () => {
  withStorage(() => {
    const headers = runtime.buildRuntimeIdentityHeaders('req', {}, { userId: 'user-b' });
    assert.equal(headers['X-User-Token'], undefined);
  });
});

test('malformed browser records do not prevent local-account requests', () => {
  withStorage((values) => {
    values.set('openjiuwen_federation', '{incomplete');
    const headers = runtime.buildRuntimeIdentityHeaders('req', {}, { userId: 'user-a' });
    assert.equal(headers.Authorization, 'Bearer platform');
    assert.equal(headers['X-User-Token'], undefined);
  });
});

test('platform refresh preserves the customer token; logout clears both', () => {
  withStorage((values) => {
    session.setManagerTokens('renewed-platform', 'renewed-refresh');
    assert.ok(values.has('openjiuwen_federation'));
    session.clearManagerTokens();
    assert.equal(values.has('openjiuwen_federation'), false);
    assert.equal(values.has('openjiuwen_access_token'), false);
  });
});
