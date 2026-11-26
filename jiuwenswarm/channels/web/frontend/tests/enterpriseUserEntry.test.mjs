import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

const appBase = process.env.TEST_APP_BASE ?? '';

await i18n.use(initReactI18next).init({
  lng: 'en',
  resources: {
    en: {
      translation: {
        enterpriseEntry: {
          emptyTitle: 'No Agent Available',
          emptyTitleNoTemplate: 'No Agent',
          noContext: 'No Agent is available for this account. Please grant access in the Manager console first.',
          noContextNoTemplate:
            'No Agent is available for this account. Please ask your administrator to add Agent Templates in the Manager console.',
        },
      },
    },
  },
  interpolation: { escapeValue: false },
});
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
  loader: { '.css': 'empty' },
  external: ['react', 'react/jsx-runtime', 'react-i18next', 'i18next', 'lucide-react'],
  define: { 'import.meta.env': JSON.stringify({ BASE_URL: appBase || '/' }) },
  alias: { 'virtual:login-auth-simulate-provider': './src/auth/simulate/available.ts' },
  outfile: 'node_modules/.cache/enterprise/userEntry.mjs',
});
const { EnterpriseEntry, useEnterpriseContext } = await import('../node_modules/.cache/enterprise/userEntry.mjs');
const context = { user_id: 'u', group_id: 'g', bot_id: 'b', jiuwenclaw_id: 'cluster' };

async function render(backend, run, enterprise = true, simulate = false, pathname = '/chat/new') {
  const dom = new JSDOM('<div id="root"></div>', { url: `http://localhost${appBase}${pathname}?user_id=forged` });
  const navigations = [];
  const location = new Proxy({}, {
    get(_target, key) {
      return key === 'replace' ? (url) => navigations.push(url) : Reflect.get(dom.window.location, key);
    },
  });
  const browser = new Proxy(dom.window, {
    get(target, key) {
      return key === 'location' ? location : Reflect.get(target, key, target);
    },
  });
  dom.window.__JIUWENSWARM_EDITION__ = enterprise ? 'enterprise' : 'personal';
  dom.window.__JIUWEN_LOGIN_AUTH_SIMULATE__ = String(simulate);
  Object.assign(globalThis, {
    window: browser,
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
  let enterpriseContext;
  function Probe() {
    const value = useEnterpriseContext();
    enterpriseContext = value;
    return React.createElement(
      'div',
      { 'data-testid': 'probe' },
      JSON.stringify(value && { user: value.user, selected: value.selected }),
    );
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => {
      root.render(React.createElement(EnterpriseEntry, null, React.createElement(Probe)));
    });
    await run(calls, () => enterpriseContext, navigations);
  } finally {
    await act(() => root.unmount());
    dom.window.close();
  }
}

function success(url) {
  if (url.endsWith('/auth/me')) return [200, { user_id: 'u', display_name: 'User U' }];
  if (url.endsWith('/agent-contexts')) return [200, { code: 200, data: { contexts: [context] } }];
  return [200, { code: 200 }];
}

test('raw Identity user and Manager context win over query; initial active cluster is posted even if cached', async () => {
  await render(success, (calls) => {
    const value = JSON.parse(document.querySelector('[data-testid="probe"]').textContent);
    assert.equal(value.user.user_id, 'u');
    assert.equal(value.selected.user_id, 'u');
    const active = calls.find(([url]) => url.endsWith('/active-cluster'));
    assert.equal(active[1].method, 'POST');
    assert.deepEqual(JSON.parse(active[1].body), { jiuwenclaw_id: 'cluster' });
    assert.match(window.location.search, /user_id=u/);
  });
});

test('bootstrap preserves an existing session deep link', async () => {
  await render(success, (_calls, _getContext, navigations) => {
    assert.equal(window.location.pathname, `${appBase}/chat/existing-session`);
    assert.equal(navigations.length, 0);
  }, true, false, '/chat/existing-session');
});

for (const pathname of ['/chat/existing-session', '/chat/new']) {
  test(`Agent switch from ${pathname} opens a new conversation with the target identity`, async () => {
    const target = { user_id: 'u', group_id: 'g2', bot_id: 'b2', jiuwenclaw_id: 'cluster2' };
    await render((url) => url.endsWith('/agent-contexts')
      ? [200, { code: 200, data: { contexts: [context, target] } }]
      : success(url), async (calls, getContext, navigations) => {
      await act(async () => {
        getContext().onContextChange(['b2', 'g2', 'u', 'cluster2'].join('\u0001'));
      });
      assert.equal(navigations.length, 1);
      const destination = new URL(navigations[0], 'http://localhost');
      assert.equal(destination.pathname, `${appBase}/chat/new`);
      for (const [key, value] of Object.entries(target)) assert.equal(destination.searchParams.get(key), value);
      assert.equal(destination.searchParams.get('debug_context'), null);
      const active = calls.filter(([url]) => url.endsWith('/active-cluster')).at(-1);
      assert.deepEqual(JSON.parse(active[1].body), { jiuwenclaw_id: 'cluster2' });
    }, true, false, pathname);
  });
}

test('failed Agent switch preserves the current conversation and exposes the error', async () => {
  const target = { user_id: 'u', group_id: 'g2', bot_id: 'b2', jiuwenclaw_id: 'cluster2' };
  await render((url, init) => {
    if (url.endsWith('/agent-contexts')) return [200, { code: 200, data: { contexts: [context, target] } }];
    if (url.endsWith('/active-cluster') && JSON.parse(init.body).jiuwenclaw_id === 'cluster2') return [503, {}];
    return success(url);
  }, async (_calls, getContext, navigations) => {
    await act(async () => {
      getContext().onContextChange(['b2', 'g2', 'u', 'cluster2'].join('\u0001'));
    });
    assert.equal(navigations.length, 0);
    assert.equal(window.location.pathname, `${appBase}/chat/existing-session`);
    assert.ok(getContext().contextError);
    assert.equal(getContext().contextSwitching, false);
  }, true, false, '/chat/existing-session');
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

test('no authorized context shows the original grant hint', async () => {
  await render(
    (url) => (url.endsWith('/agent-contexts') ? [200, { code: 200, data: { contexts: [] } }] : success(url)),
    () => {
      assert.equal(document.querySelector('[data-testid="probe"]'), null);
      assert.match(
        document.querySelector('[data-testid="enterprise-entry-status"]').textContent,
        /grant access in the Manager console/i,
      );
      assert.match(
        document.querySelector('[data-testid="enterprise-entry-title"]').textContent,
        /No Agent Available/i,
      );
    },
  );
});

test('missing agent templates shows the add-template hint', async () => {
  await render(
    (url) => (url.endsWith('/agent-contexts')
      ? [200, { code: 200, data: { contexts: [], empty_reason: 'no_agent_template' } }]
      : success(url)),
    () => {
      assert.equal(document.querySelector('[data-testid="probe"]'), null);
      assert.match(
        document.querySelector('[data-testid="enterprise-entry-status"]').textContent,
        /add Agent Templates/i,
      );
      assert.match(
        document.querySelector('[data-testid="enterprise-entry-title"]').textContent,
        /^No Agent$/i,
      );
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
