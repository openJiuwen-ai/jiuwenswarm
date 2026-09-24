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

test('oauth reports a manually closed window when the result is still pending', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.window = {
    setTimeout: (callback) => setTimeout(callback, 0),
    dispatchEvent: () => true,
  };
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response(JSON.stringify({ status: 'pending' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  try {
    await assert.rejects(
      waitForHubOAuth({ authorize_url: 'https://example.com', flow: 'flow', claim: 'claim' }, undefined, () => true),
      /授权窗口已关闭/,
    );
    assert.equal(fetchCalls, 1);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});
