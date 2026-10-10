import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/auth-store/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/stores/authStore.ts'),
    vite.ssrLoadModule('/src/services/webClient.ts'),
  ]);
} finally {
  await vite.close();
}
const [{ useAuthStore }, { webClient }] = modules;
const initialState = useAuthStore.getState();

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('waitFor timed out');
    await sleep(20);
  }
}

function json(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

async function withBackend(routes, run) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
  const windows = [];
  dom.window.open = (url) => {
    const win = { url, opener: dom.window, closed: false, close() { this.closed = true; } };
    windows.push(win);
    return win;
  };
  const calls = [];
  const saved = new Map();
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    sessionStorage: dom.window.sessionStorage,
    CustomEvent: dom.window.CustomEvent,
    fetch: async (url, init = {}) => {
      calls.push({ url: String(url), init });
      return routes(String(url), init);
    },
  })) {
    saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const originalReconnect = webClient.reconnect;
  webClient.reconnect = async () => {};
  useAuthStore.setState(initialState, true);
  try {
    await run({ windows, calls });
  } finally {
    useAuthStore.getState().cancelLogin();
    webClient.reconnect = originalReconnect;
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  }
}

const authorizeBody = (n) => ({ authorizeUrl: `https://oauth/${n}`, state: `s${n}`, claimToken: `ct${n}` });

test('login completes when the claim succeeds', async () => {
  const claims = [];
  await withBackend((url, init) => {
    if (url.endsWith('/authorize')) return json(200, authorizeBody(1));
    if (url.endsWith('/claim')) {
      claims.push(JSON.parse(init.body));
      return json(200, { islogin: true, userId: 'openid-1' });
    }
    if (url.endsWith('/quota')) return json(200, { available: false });
    throw new Error(`unexpected ${url}`);
  }, async ({ windows }) => {
    await useAuthStore.getState().startLogin();
    assert.equal(windows.length, 1);
    await useAuthStore.getState().checkLogin();
    await waitFor(() => useAuthStore.getState().islogin);
    assert.deepEqual(claims, [{ state: 's1', claimToken: 'ct1' }]);
    assert.equal(useAuthStore.getState().phase, 'idle');
  });
});

test('the callback page broadcast triggers a claim; window messages do not', async () => {
  await withBackend((url) => {
    if (url.endsWith('/authorize')) return json(200, authorizeBody(1));
    if (url.endsWith('/claim')) return json(200, { islogin: true, userId: 'openid-1' });
    if (url.endsWith('/quota')) return json(200, { available: false });
    throw new Error(`unexpected ${url}`);
  }, async ({ windows, calls }) => {
    await useAuthStore.getState().startLogin();
    assert.equal(windows[0].opener, null, '授权窗口不能留着能反向导航本页的引用');

    window.dispatchEvent(new window.MessageEvent('message', { data: { type: 'jiuwenswarm:auth-callback' } }));
    await sleep(50);
    assert.equal(calls.filter((c) => c.url.endsWith('/claim')).length, 0);

    const landing = new BroadcastChannel('jiuwenswarm:auth');
    landing.postMessage({ type: 'jiuwenswarm:auth-callback', ok: true });
    landing.close();
    await waitFor(() => useAuthStore.getState().islogin);

    const stateChanging = calls.filter((c) => /\/(authorize|claim)$/.test(c.url));
    assert.equal(stateChanging.length, 2);
    for (const call of stateChanging) assert.equal(call.init.headers['X-Jiuwen-Auth'], '1');
  });
});

test('cancel while fetching the authorize URL: no browser window, no claim triggers', async () => {
  let releaseAuthorize = null;
  await withBackend((url) => {
    if (url.endsWith('/authorize')) {
      return new Promise((resolve) => {
        releaseAuthorize = () => resolve(json(200, authorizeBody(1)));
      });
    }
    throw new Error(`unexpected ${url}`);
  }, async ({ windows, calls }) => {
    const pending = useAuthStore.getState().startLogin();
    await waitFor(() => releaseAuthorize !== null);
    useAuthStore.getState().cancelLogin();
    releaseAuthorize();
    await pending;

    assert.equal(windows.length, 0, '用户已经取消了，不能再替他打开浏览器');
    assert.equal(useAuthStore.getState().phase, 'idle');
    window.dispatchEvent(new window.Event('focus'));
    await sleep(50);
    assert.equal(calls.filter((c) => c.url.endsWith('/claim')).length, 0);
  });
});

test('cancel then log in again: a late result from the first round is ignored', async () => {
  let authorizeCount = 0;
  let releaseFirstClaim = null;
  await withBackend((url, init) => {
    if (url.endsWith('/authorize')) {
      authorizeCount += 1;
      return json(200, authorizeBody(authorizeCount));
    }
    if (url.endsWith('/claim')) {
      const body = JSON.parse(init.body);
      if (body.state === 's1') {
        return new Promise((resolve) => {
          releaseFirstClaim = () => resolve(json(200, { islogin: true, userId: 'stale' }));
        });
      }
      return json(202, {});
    }
    throw new Error(`unexpected ${url}`);
  }, async () => {
    await useAuthStore.getState().startLogin();
    const firstClaim = useAuthStore.getState().checkLogin();
    await waitFor(() => releaseFirstClaim !== null);

    useAuthStore.getState().cancelLogin();
    await useAuthStore.getState().startLogin();
    releaseFirstClaim();
    await firstClaim;

    assert.equal(useAuthStore.getState().islogin, false, '已作废那一轮的认领结果不能生效');
    assert.equal(useAuthStore.getState().phase, 'waiting', '新流程的状态不能被旧流程踩掉');
  });
});

test('concurrent refresh() calls share one status request', async () => {
  let releaseStatus = null;
  await withBackend((url) => {
    if (url.endsWith('/status')) {
      return new Promise((resolve) => {
        releaseStatus = () => resolve(json(200, { enabled: true, islogin: false }));
      });
    }
    throw new Error(`unexpected ${url}`);
  }, async ({ calls }) => {
    const store = useAuthStore.getState();
    const first = store.refresh();
    const second = store.refresh();
    const third = store.refresh();
    assert.equal(first, second);
    assert.equal(second, third);

    await waitFor(() => releaseStatus !== null);
    releaseStatus();
    await Promise.all([first, second, third]);

    const statusCalls = () => calls.filter((c) => c.url.endsWith('/status')).length;
    assert.equal(statusCalls(), 1);
    assert.equal(useAuthStore.getState().enabled, true);

    releaseStatus = null;
    const again = useAuthStore.getState().refresh();
    await waitFor(() => releaseStatus !== null);
    releaseStatus();
    await again;
    assert.equal(statusCalls(), 2);
  });
});
