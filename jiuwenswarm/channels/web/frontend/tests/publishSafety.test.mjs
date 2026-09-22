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

test('oauth reports a closed authorization window and tolerates an empty error response', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    setTimeout: (callback) => setTimeout(callback, 0),
    dispatchEvent: () => true,
  };
  globalThis.fetch = async () => new Response('', { status: 502 });
  await assert.rejects(beginHubOAuth('gitcode'), /无法启动 Hub 授权/);
  await assert.rejects(
    waitForHubOAuth({ authorize_url: 'https://example.com', flow: 'flow', claim: 'claim' }, undefined, () => true),
    /授权窗口已关闭/,
  );
  globalThis.window = previousWindow;
  globalThis.fetch = previousFetch;
});
