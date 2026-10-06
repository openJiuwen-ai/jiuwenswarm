import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildQaSummaryContent,
  parseQaSummaryContent,
} from '../node_modules/.cache/publish-safety/components/InteractionSlot/qaSummary.js';
import {
  publishFailureKey,
  publishIssueKey,
} from '../node_modules/.cache/publish-safety/features/assetPublishErrors.js';
import { beginHubOAuth, waitForHubOAuth } from '../node_modules/.cache/publish-safety/utils/gitcodeOAuth.js';

test('qa summary masks credential answers while retaining ordinary answers', () => {
  const content = buildQaSummaryContent({
    items: [
      { question: '请输入 GitCode access token', answers: ['mt_live_secret'] },
      { question: '选择发布范围', answers: ['公开'] },
    ],
  });
  const parsed = parseQaSummaryContent(content);
  assert.deepEqual(parsed.items[0].answers, ['••••••']);
  assert.deepEqual(parsed.items[1].answers, ['公开']);
  assert.doesNotMatch(content, /mt_live_secret/);

  const legacyContent = `qa.summary:{"items":[{"question":"请输入密码","answers":["legacy-secret"]}]}`;
  const legacyParsed = parseQaSummaryContent(legacyContent);
  assert.deepEqual(legacyParsed.items[0].answers, ['••••••']);
});

test('publish errors preserve actionable safe backend codes', () => {
  assert.equal(
    publishFailureKey(Object.assign(new Error('failed'), { code: 'INVALID_PLUGIN_STRUCTURE' })),
    'invalidPluginStructure',
  );
  assert.equal(publishFailureKey(Object.assign(new Error('failed'), { code: 'PLUGIN_NOT_FOUND' })), 'pluginNotFound');
  assert.equal(
    publishFailureKey(Object.assign(new Error('failed'), { code: 'SESSION_EXCHANGE_FAILED' })),
    'sessionExchangeFailed',
  );
  assert.equal(publishFailureKey(new Error('internal details')), 'requestFailed');
  assert.equal(publishIssueKey('invalid_plugin_structure'), 'invalidPluginStructure');
  assert.equal(publishIssueKey('hub_request_failed'), 'hubRequestFailed');
});

test('oauth start tolerates an empty error response', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    setTimeout: (callback) => setTimeout(callback, 0),
    dispatchEvent: () => true,
  };
  globalThis.fetch = async () => new Response('', { status: 502 });
  try {
    await assert.rejects(beginHubOAuth('gitcode'), /无法启动 Hub 授权/);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

for (const provider of ['gitcode', 'github']) {
  test(`oauth accepts a completed ${provider} result after the authorization window closes`, async () => {
    const previousWindow = globalThis.window;
    const previousSessionStorage = globalThis.sessionStorage;
    const previousFetch = globalThis.fetch;
    const stored = new Map();
    let fetchCalls = 0;
    globalThis.window = {
      setTimeout: (callback) => setTimeout(callback, 0),
      dispatchEvent: () => true,
    };
    globalThis.sessionStorage = {
      setItem: (key, value) => stored.set(key, value),
      removeItem: (key) => stored.delete(key),
    };
    globalThis.fetch = async () => {
      fetchCalls += 1;
      return new Response(
        JSON.stringify({
          status: 'complete',
          provider,
          access_token: 'oauth-token',
          user: { id: '42', login: 'tester', name: 'Tester' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };
    try {
      await waitForHubOAuth(
        { authorize_url: 'https://example.com', flow: 'flow', claim: 'claim' },
        undefined,
        () => true,
      );
      assert.equal(fetchCalls, 1);
      assert.equal(stored.get('marketplace_oauth_access_token'), 'oauth-token');
      assert.equal(stored.get('marketplace_oauth_provider'), provider);
    } finally {
      globalThis.window = previousWindow;
      globalThis.sessionStorage = previousSessionStorage;
      globalThis.fetch = previousFetch;
    }
  });
}

for (const provider of ['gitcode', 'github']) {
  for (const scenario of [
    'delayed-success',
    'closed-timeout',
    'explicit-failure',
    'expired',
    'open-window',
    'aborted',
  ]) {
    test(`oauth ${provider} handles ${scenario} during the closed-window grace period`, async () => {
      const previousWindow = globalThis.window;
      const previousFetch = globalThis.fetch;
      const previousStorage = globalThis.sessionStorage;
      const previousNow = Date.now;
      const stored = new Map();
      const controller = new AbortController();
      let now = 0;
      let calls = 0;
      Date.now = () => now;
      globalThis.window = {
        setTimeout: (callback, delay) =>
          setTimeout(() => {
            now += delay;
            if (scenario === 'aborted' && now >= 2000) controller.abort();
            callback();
          }, 0),
        dispatchEvent: () => true,
      };
      globalThis.sessionStorage = {
        setItem: (key, value) => stored.set(key, value),
        removeItem: (key) => stored.delete(key),
      };
      globalThis.fetch = async () => {
        calls++;
        let result = { status: 'pending' };
        if (scenario === 'explicit-failure' && calls === 2)
          result = { status: 'failed', error: 'session_exchange_failed' };
        if ((scenario === 'delayed-success' && calls === 3) || (scenario === 'open-window' && calls === 8)) {
          result = { status: 'complete', provider, access_token: 'synthetic-token' };
        }
        return new Response(JSON.stringify(result), { status: scenario === 'expired' ? 404 : 200 });
      };
      try {
        const promise = waitForHubOAuth(
          { authorize_url: 'https://example.com', flow: 'flow', claim: 'claim' },
          controller.signal,
          () => scenario !== 'open-window',
        );
        if (scenario === 'delayed-success' || scenario === 'open-window') {
          await promise;
          assert.equal(stored.get('marketplace_oauth_access_token'), 'synthetic-token');
          assert.equal(stored.get('marketplace_oauth_provider'), provider);
          assert.equal(calls, scenario === 'delayed-success' ? 3 : 8);
        } else {
          const expected = {
            'closed-timeout': /授权窗口已关闭/,
            'explicit-failure': /兑换失败/,
            expired: /授权请求已过期/,
            aborted: /OAuth cancelled/,
          }[scenario];
          await assert.rejects(promise, expected);
          assert.equal(stored.size, 0);
          assert.equal(calls, scenario === 'closed-timeout' ? 6 : scenario === 'explicit-failure' ? 2 : 1);
        }
      } finally {
        globalThis.window = previousWindow;
        globalThis.fetch = previousFetch;
        globalThis.sessionStorage = previousStorage;
        Date.now = previousNow;
      }
    });
  }
}
