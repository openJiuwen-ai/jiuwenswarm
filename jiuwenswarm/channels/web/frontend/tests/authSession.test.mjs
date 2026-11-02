import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const values = new Map();
const sessionValues = new Map();
let cookie = '';
let redirects = [];

globalThis.localStorage = {
  getItem(key) {
    return values.get(key) ?? null;
  },
  removeItem(key) {
    values.delete(key);
  },
  setItem(key, value) {
    values.set(key, String(value));
  },
};
globalThis.sessionStorage = {
  getItem: (key) => sessionValues.get(key) ?? null,
  removeItem: (key) => sessionValues.delete(key),
};
globalThis.document = {
  get cookie() {
    return cookie;
  },
  set cookie(value) {
    cookie = String(value);
  },
};
globalThis.window = {
  location: {
    pathname: '/chat/',
    protocol: 'http:',
    replace(path) {
      redirects.push(path);
    },
  },
};

async function loadAuthSession(prefix = '') {
  const { outputFiles } = await build({
    entryPoints: ['src/auth/manager/authSession.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    define: { 'import.meta.env': JSON.stringify({ VITE_API_PREFIX: prefix }) },
  });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString('base64')}`);
}

const {
  clearManagerTokens,
  getManagerAccessToken,
  getManagerRefreshToken,
  managerAuthenticatedFetch,
  setManagerTokens,
} = await loadAuthSession();

function reset() {
  values.clear();
  sessionValues.clear();
  cookie = '';
  redirects = [];
  window.location.pathname = '/chat/';
}

function authHeader(init) {
  return new Headers(init?.headers).get('Authorization');
}

test('concurrent 401 responses share one refresh and retry with the rotated access token', async () => {
  reset();
  setManagerTokens('old-access', 'old-refresh');
  let refreshCalls = 0;
  let protectedCalls = 0;

  globalThis.fetch = async (input, init) => {
    if (String(input) === '/idp/v1/auth/refresh') {
      refreshCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return new Response(
        JSON.stringify({
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }
    protectedCalls += 1;
    const authorized = authHeader(init) === 'Bearer new-access';
    return new Response(JSON.stringify({ ok: authorized }), {
      status: authorized ? 200 : 401,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const responses = await Promise.all([
    managerAuthenticatedFetch('/gateway-api/v1/health'),
    managerAuthenticatedFetch('/gateway-api/v1/sessions'),
  ]);

  assert.deepEqual(
    responses.map((response) => response.status),
    [200, 200],
  );
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 4);
  assert.equal(getManagerAccessToken(), 'new-access');
  assert.equal(getManagerRefreshToken(), 'new-refresh');
  assert.match(cookie, /^openjiuwen_access_token=new-access;/);
  assert.deepEqual(redirects, []);
});

test('a failed refresh clears the session and redirects to the login page', async () => {
  reset();
  setManagerTokens('expired-access', 'expired-refresh');
  let refreshCalls = 0;
  globalThis.fetch = async (input) => {
    if (String(input) === '/idp/v1/auth/refresh') {
      refreshCalls += 1;
      return new Response('{"detail":"auth_invalid_refresh"}', { status: 401 });
    }
    return new Response('{"detail":"invalid or expired token"}', { status: 401 });
  };

  const response = await managerAuthenticatedFetch('/gateway-api/v1/sessions/session-1/history');

  assert.equal(response.status, 401);
  assert.equal(refreshCalls, 1);
  assert.equal(getManagerAccessToken(), null);
  assert.equal(getManagerRefreshToken(), null);
  assert.match(cookie, /Max-Age=0/);
  assert.deepEqual(redirects, ['/auth']);
});

test('the retried request is sent only once even when it also returns 401', async () => {
  reset();
  setManagerTokens('old-access', 'refresh-1');
  let refreshCalls = 0;
  let protectedCalls = 0;
  globalThis.fetch = async (input) => {
    if (String(input) === '/idp/v1/auth/refresh') {
      refreshCalls += 1;
      return new Response(
        JSON.stringify({
          access_token: 'new-access',
          refresh_token: 'refresh-2',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }
    protectedCalls += 1;
    return new Response('{"detail":"still unauthorized"}', { status: 401 });
  };

  const response = await managerAuthenticatedFetch('/gateway-api/v1/health');

  assert.equal(response.status, 401);
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 2);
  assert.deepEqual(redirects, []);
});

test('clearing tokens removes both local values and expires the access cookie', () => {
  reset();
  setManagerTokens('access', 'refresh');
  sessionValues.set('jiuwenclaw_active_cluster', 'previous-account-cluster');
  sessionValues.set('unrelated-setting', 'keep');
  clearManagerTokens();

  assert.equal(getManagerAccessToken(), null);
  assert.equal(getManagerRefreshToken(), null);
  assert.match(cookie, /Max-Age=0/);
  assert.equal(sessionStorage.getItem('jiuwenclaw_active_cluster'), null);
  assert.equal(sessionStorage.getItem('unrelated-setting'), 'keep');
});

test('requests synchronize externally written tokens to the cookie and include credentials', async () => {
  reset();
  localStorage.setItem('openjiuwen_access_token', 'manager-web-token');
  cookie = 'stale';
  let sent;
  globalThis.fetch = async (_input, init) => {
    sent = { authorization: authHeader(init), cookie, credentials: init.credentials };
    return new Response('{}');
  };

  await managerAuthenticatedFetch('/manager-api/v1/authz/me');

  assert.equal(sent.authorization, 'Bearer manager-web-token');
  assert.equal(sent.credentials, 'include');
  assert.match(sent.cookie, /^openjiuwen_access_token=manager-web-token;/);
});

for (const prefix of ['', '/portal/']) {
  test(`refresh uses the API prefix ${JSON.stringify(prefix)} and includes credentials on every attempt`, async () => {
    reset();
    const session = await loadAuthSession(prefix);
    session.setManagerTokens('old-access', 'refresh');
    const calls = [];
    const refreshPath = `${prefix.replace(/\/+$/, '')}/idp/v1/auth/refresh`;
    globalThis.fetch = async (input, init) => {
      calls.push({ path: String(input), credentials: init.credentials });
      if (String(input) === refreshPath) {
        return Response.json({ access_token: 'new-access', refresh_token: 'new-refresh' });
      }
      return new Response('{}', { status: authHeader(init) === 'Bearer new-access' ? 200 : 401 });
    };

    const response = await session.managerAuthenticatedFetch('/manager-api/v1/authz/me');

    assert.equal(response.status, 200);
    assert.deepEqual(
      calls.map((call) => call.path),
      ['/manager-api/v1/authz/me', refreshPath, '/manager-api/v1/authz/me'],
    );
    assert.ok(calls.every((call) => call.credentials === 'include'));
    assert.deepEqual(redirects, []);
  });
}
