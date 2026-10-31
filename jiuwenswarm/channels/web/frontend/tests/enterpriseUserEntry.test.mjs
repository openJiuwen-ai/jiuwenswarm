import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

await i18n.use(initReactI18next).init({ lng: 'en', resources: {}, interpolation: { escapeValue: false } });
await build({
  stdin: {
    contents:
      "export { EnterpriseEntry } from './src/EnterpriseEntry'; export { useEnterpriseContext } from './src/services/enterpriseContext';",
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  jsx: 'automatic',
  external: ['react', 'react/jsx-runtime', 'react-i18next'],
  define: { 'import.meta.env': '{}' },
  alias: { 'virtual:login-auth-simulate-provider': './src/auth/simulate/available.ts' },
  outfile: 'node_modules/.cache/enterprise/userEntry.mjs',
});
const { EnterpriseEntry, useEnterpriseContext } = await import('../node_modules/.cache/enterprise/userEntry.mjs');
const context = { user_id: 'u', group_id: 'g', bot_id: 'b', jiuwenclaw_id: 'cluster' };

async function render(backend, run, enterprise = true, simulate = false) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/chat/new?user_id=forged' });
  dom.window.__JIUWENSWARM_EDITION__ = enterprise ? 'enterprise' : 'personal';
  dom.window.__JIUWEN_LOGIN_AUTH_SIMULATE__ = String(simulate);
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  localStorage.setItem('openjiuwen_access_token', 'token');
  sessionStorage.setItem('jiuwenclaw_active_cluster', 'cluster');
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([url, init]);
    const [status, body] = backend(url, init);
    return new Response(JSON.stringify(body), { status });
  };
  function Probe() {
    const value = useEnterpriseContext();
    return React.createElement(
      'div',
      { 'data-testid': 'probe' },
      JSON.stringify(value && { user: value.user, selected: value.selected, managerAccess: value.managerAccess }),
    );
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      root.render(React.createElement(EnterpriseEntry, null, React.createElement(Probe)));
    });
    await run(calls);
  } finally {
    await act(() => root.unmount());
    dom.window.close();
  }
}

function success(url) {
  if (url.endsWith('/auth/me')) return [200, { user_id: 'u', display_name: 'User U' }];
  if (url.endsWith('/authz/me')) return [200, { code: 200, data: { manager_access: false } }];
  if (url.endsWith('/agent-contexts')) return [200, { code: 200, data: { contexts: [context] } }];
  return [200, { code: 200 }];
}

test('raw Identity user and Manager context win over query; initial active cluster is posted even if cached', async () => {
  await render(success, (calls) => {
    const value = JSON.parse(document.querySelector('[data-testid="probe"]').textContent);
    assert.equal(value.user.user_id, 'u');
    assert.equal(value.selected.user_id, 'u');
    assert.equal(value.managerAccess, false);
    const active = calls.find(([url]) => url.endsWith('/active-cluster'));
    assert.equal(active[1].method, 'POST');
    assert.deepEqual(JSON.parse(active[1].body), { jiuwenclaw_id: 'cluster' });
    assert.match(window.location.search, /user_id=u/);
  });
});

test('Identity outage does not turn URL identity into a logged-in user', async () => {
  await render(
    () => [503, {}],
    () => {
      assert.equal(document.querySelector('[data-testid="probe"]'), null);
      assert.ok(document.querySelector('[data-testid="enterprise-entry-retry"]'));
      assert.ok(document.querySelector('[data-testid="enterprise-entry-login"]'));
    },
  );
});

test('no authorized context shows an error rather than a never-ending chat spinner', async () => {
  await render(
    (url) => (url.endsWith('/agent-contexts') ? [200, { code: 200, data: { contexts: [] } }] : success(url)),
    () => {
      assert.equal(document.querySelector('[data-testid="probe"]'), null);
      assert.match(document.querySelector('[data-testid="enterprise-entry-status"]').textContent, /noContext/);
    },
  );
});

test('personal edition renders without Identity or Manager calls', async () => {
  await render(
    () => {
      throw new Error('enterprise request');
    },
    (calls) => {
      assert.ok(document.querySelector('[data-testid="probe"]'));
      assert.equal(calls.length, 0);
    },
    false,
  );
});

test('dev-stable simulated enterprise login needs neither Identity nor Manager', async () => {
  await render(
    () => { throw new Error('unexpected authentication dependency'); },
    (calls) => {
      const value = JSON.parse(document.querySelector('[data-testid="probe"]').textContent);
      assert.equal(value.selected.user_id, 'forged');
      assert.equal(calls.length, 0);
    },
    true,
    true,
  );
});
