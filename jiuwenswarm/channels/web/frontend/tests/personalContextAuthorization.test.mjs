import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: ['src/services/personalContextApi.ts'],
  outfile: 'node_modules/.cache/personal-context-authorization/personalContextApi.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  plugins: [
    {
      name: 'personal-context-web-client',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/webClient$/ }, () => ({
          path: 'webClient',
          namespace: 'mock',
        }));
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({
          contents: `
            export const webRequest = async (method, params) => {
              globalThis.personalContextRequests.push({ method, params });
              return {
                provider: params.provider,
                state: 'authorized',
                verification_url: null,
                expires_at: null,
                error: null,
              };
            };
            export const webClient = {
              on: () => () => {},
              sendFireAndForget: () => {},
            };
          `,
        }));
      },
    },
  ],
});

const { pcApi } = await import('../node_modules/.cache/personal-context-authorization/personalContextApi.mjs');

await build({
  entryPoints: ['src/components/PersonalContext/authorizationPolling.ts'],
  outfile: 'node_modules/.cache/personal-context-authorization/authorizationPolling.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
});

const { AUTHORIZATION_POLL_INTERVAL_MS, authorizationAction, safeHttpsUrl, startAuthorizationPolling } =
  await import('../node_modules/.cache/personal-context-authorization/authorizationPolling.mjs');

const settingsSource = readFileSync(
  new URL('../src/components/PersonalContext/SettingsPanel.tsx', import.meta.url),
  'utf8',
);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const fakeRuntime = (now = 0) => {
  const timers = new Map();
  let nextId = 0;
  return {
    timers,
    runtime: {
      now: () => now,
      setTimeout: (callback, delay) => {
        const id = ++nextId;
        timers.set(id, { callback, delay });
        return id;
      },
      clearTimeout: (id) => timers.delete(id),
    },
  };
};
const takeTimer = (timers) => {
  const entry = timers.entries().next().value;
  assert.ok(entry);
  const [id, timer] = entry;
  timers.delete(id);
  return timer;
};

test('provider reauthorization sends the explicit boolean without changing credential shape', async () => {
  globalThis.personalContextRequests = [];
  await pcApi.authorizeProvider('github', { token: 'token-canary' }, true);
  assert.deepEqual(globalThis.personalContextRequests, [
    {
      method: 'personal_context.fetch.authorize_provider',
      params: {
        provider: 'github',
        credentials: { token: 'token-canary' },
        reauthorize: true,
      },
    },
  ]);
});

test('settings sends explicit reauthorization for all providers', () => {
  assert.match(settingsSource, /authorizeProvider\('feishu', undefined, true\)/);
  assert.match(settingsSource, /authorizeProvider\('github', \{ token \}, true\)/);
  assert.match(settingsSource, /authorizeProvider\('gitcode', \{ pat \}, true\)/);
  assert.doesNotMatch(settingsSource, /setInterval\(/);
  assert.match(settingsSource, /personalContext\.authorization\.openVerification/);
  assert.match(settingsSource, /personalContext\.authorization\.repositorySuccess/);
  assert.match(settingsSource, /personalContext\.authorization\.failedOriginalRetained/);
  for (const provider of ['feishu', 'github', 'gitcode']) {
    assert.match(settingsSource, new RegExp(`data-testid="personal-context-authorization-action-${provider}"`));
  }
  assert.match(settingsSource, /data-testid="personal-context-feishu-verification-link"/);
  assert.match(
    settingsSource,
    /disabled=\{!isConnected \|\| !!pendingWrites\['auth:feishu'\] \|\| feishuDisplayState === 'authorizing'\}/,
  );
  assert.match(settingsSource, /role="status"/);
});

test('authorization actions distinguish first use, retry, and pending states', () => {
  assert.equal(authorizationAction('not_authorized'), 'start');
  assert.equal(authorizationAction('authorization_required'), 'start');
  assert.equal(authorizationAction('authorized'), 'reauthorize');
  assert.equal(authorizationAction('authorization_failed'), 'reauthorize');
  assert.equal(authorizationAction('authorizing'), 'authorizing');
});

test('verification links accept only HTTPS', () => {
  assert.equal(safeHttpsUrl('https://open.feishu.cn/authorize'), 'https://open.feishu.cn/authorize');
  assert.equal(safeHttpsUrl('http://open.feishu.cn/authorize'), null);
  assert.equal(safeHttpsUrl('javascript:alert(1)'), null);
  assert.equal(safeHttpsUrl('not a URL'), null);
});

test('Feishu polling is serial and schedules only after completion', async () => {
  const held = deferred();
  const { timers, runtime } = fakeRuntime();
  const terminals = [];
  const cancel = startAuthorizationPolling(
    {
      expiresAt: '2099-09-15T12:00:00Z',
      readStatus: () => held.promise,
      onTerminal: (result) => terminals.push(result.state),
      onExpired: () => terminals.push('expired'),
    },
    runtime,
  );

  assert.equal(timers.size, 1);
  const first = takeTimer(timers);
  assert.equal(first.delay, AUTHORIZATION_POLL_INTERVAL_MS);
  const request = first.callback();
  assert.equal(timers.size, 0);
  held.resolve({
    provider: 'feishu',
    state: 'authorizing',
    verification_url: 'https://open.feishu.cn/authorize',
    expires_at: '2099-09-15T12:00:00Z',
    error: null,
  });
  await request;
  assert.equal(timers.size, 1);
  assert.deepEqual(terminals, []);
  cancel();
  assert.equal(timers.size, 0);
});

test('Feishu polling stops on terminal state, expiry, and cancellation', async () => {
  for (const state of ['authorized', 'authorization_failed']) {
    const { timers, runtime } = fakeRuntime();
    const terminals = [];
    startAuthorizationPolling(
      {
        expiresAt: '2099-09-15T12:00:00Z',
        readStatus: async () => ({
          provider: 'feishu',
          state,
          verification_url: null,
          expires_at: null,
          error: state === 'authorization_failed' ? 'safe failure' : null,
        }),
        onTerminal: (result) => terminals.push(result.state),
        onExpired: () => terminals.push('expired'),
      },
      runtime,
    );
    await takeTimer(timers).callback();
    assert.deepEqual(terminals, [state]);
    assert.equal(timers.size, 0);
  }

  const expired = fakeRuntime(Date.parse('2026-09-15T12:00:01Z'));
  let reads = 0;
  let expirations = 0;
  startAuthorizationPolling(
    {
      expiresAt: '2026-09-15T12:00:00Z',
      readStatus: async () => {
        reads += 1;
        return null;
      },
      onTerminal: () => {},
      onExpired: () => {
        expirations += 1;
      },
    },
    expired.runtime,
  );
  assert.equal(expired.timers.size, 0);
  assert.equal(reads, 0);
  assert.equal(expirations, 1);

  const held = deferred();
  const cancelled = fakeRuntime();
  let terminalAfterCancel = false;
  const cancel = startAuthorizationPolling(
    {
      expiresAt: '2099-09-15T12:00:00Z',
      readStatus: () => held.promise,
      onTerminal: () => {
        terminalAfterCancel = true;
      },
      onExpired: () => {},
    },
    cancelled.runtime,
  );
  const request = takeTimer(cancelled.timers).callback();
  cancel();
  held.resolve({
    provider: 'feishu',
    state: 'authorized',
    verification_url: null,
    expires_at: null,
    error: null,
  });
  await request;
  assert.equal(terminalAfterCancel, false);
  assert.equal(cancelled.timers.size, 0);
});
