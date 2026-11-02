import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

for (const prefix of ['', '/portal/']) {
  const { outputFiles } = await build({
    entryPoints: ['src/auth/manager/ManagerAuthProvider.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    define: { 'import.meta.env': JSON.stringify({ VITE_API_PREFIX: prefix }) },
  });
  const { managerAuthProvider } = await import(
    `data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString('base64')}`
  );

  test(`Manager access uses the API prefix ${JSON.stringify(prefix)} and preserves permission results`, async () => {
    const originalFetch = globalThis.fetch;
    const originalStorage = globalThis.localStorage;
    globalThis.localStorage = {
      getItem: (key) => (key === 'openjiuwen_access_token' ? 'access-token' : null),
    };
    try {
      for (const [data, expected] of [
        [{ manager_access: true }, true],
        [{ is_platform_admin: true }, true],
        [{ manager_access: false, is_platform_admin: false }, false],
      ]) {
        const calls = [];
        globalThis.fetch = async (url, init) => {
          calls.push([url, new Headers(init.headers).get('Authorization')]);
          return Response.json({ code: 200, data });
        };

        assert.equal(await managerAuthProvider.getManagerAccess(), expected);
        assert.deepEqual(calls, [[`${prefix.replace(/\/+$/, '')}/manager-api/v1/authz/me`, 'Bearer access-token']]);
      }
    } finally {
      globalThis.fetch = originalFetch;
      globalThis.localStorage = originalStorage;
    }
  });

  test(`logout uses the API prefix ${JSON.stringify(prefix)}, includes cookies and clears the cluster`, async () => {
    const originals = Object.fromEntries(
      ['fetch', 'window', 'document', 'localStorage', 'sessionStorage'].map((key) => [key, globalThis[key]]),
    );
    const values = new Map([
      ['openjiuwen_access_token', 'access'],
      ['openjiuwen_refresh_token', 'refresh'],
    ]);
    const sessionValues = new Map([['jiuwenclaw_active_cluster', 'previous-account-cluster']]);
    const redirects = [];
    const calls = [];
    Object.assign(globalThis, {
      window: { location: { pathname: '/chat/', protocol: 'http:', replace: (path) => redirects.push(path) } },
      document: { cookie: '' },
      localStorage: { getItem: (key) => values.get(key) ?? null, removeItem: (key) => values.delete(key) },
      sessionStorage: { removeItem: (key) => sessionValues.delete(key) },
      fetch: async (url, init) => {
        calls.push([url, init.credentials, JSON.parse(init.body)]);
        return Response.json({ ok: true });
      },
    });
    try {
      await managerAuthProvider.logout();
      assert.deepEqual(calls, [
        [`${prefix.replace(/\/+$/, '')}/idp/v1/auth/logout`, 'include', { refresh_token: 'refresh' }],
      ]);
      assert.equal(values.size, 0);
      assert.equal(sessionValues.size, 0);
      assert.match(document.cookie, /Max-Age=0/);
      assert.deepEqual(redirects, ['/auth']);
    } finally {
      Object.assign(globalThis, originals);
    }
  });
}
