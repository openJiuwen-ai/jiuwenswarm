import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});
await i18next
  .use(initReactI18next)
  .init({ lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } } });
const { AssetPublishHost } =
  await import('../node_modules/.cache/asset-publish-ui/components/AssetPublishDrawer/index.js');
const { assetPublishApi, openAssetPublish } =
  await import('../node_modules/.cache/asset-publish-ui/services/assetPublishApi.js');
const tick = () =>
  act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
const find = (id) => document.querySelector(`[data-testid="${id}"]`);
const defaults = {
  asset_name: 'demo',
  display_name: 'Demo',
  description: 'Example',
  version: '1.0.0',
  tags: [],
  version_desc: '',
  visibility: 'public',
};
const description = { defaults, can_publish: true, errors: [], records: [], identity_verified: false };
const draft = {
  draft_id: 'draft',
  can_submit: true,
  errors: [],
  warnings: [],
  files: ['SKILL.md'],
  excluded: [],
  normalizations: [],
  dependencies: [],
  expires_at: Date.now() / 1000 + 600,
  size_bytes: 100,
  package_name: 'demo',
  version: '1.0.0',
};
const root = createRoot(document.getElementById('root'));
test('all five RPCs send the current token and provider without frontend identity claims', async () => {
  const { webClient } = await import('../node_modules/.cache/asset-publish-ui/services/webClient.js');
  const original = webClient.request;
  const calls = [];
  webClient.request = async (method, params) => {
    calls.push({ method, params });
    return {};
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'token-one');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  const ref = { kind: 'skill', local_id: 'demo' };
  await assetPublishApi.describe(ref);
  await assetPublishApi.prepare(ref, defaults, '', false);
  await assetPublishApi.commit('draft', 'same-request');
  sessionStorage.setItem('marketplace_oauth_access_token', 'token-two');
  sessionStorage.setItem('marketplace_oauth_provider', 'github');
  await assetPublishApi.status('operation');
  await assetPublishApi.records(ref);
  assert.deepEqual(
    calls.map((call) => call.method),
    [
      'assets.publish.describe',
      'assets.publish.prepare',
      'assets.publish.commit',
      'assets.publish.status',
      'assets.publish.records',
    ],
  );
  assert.equal(calls[0].params.auth.access_token, 'token-one');
  assert.equal(calls[4].params.auth.access_token, 'token-two');
  assert.equal(calls[4].params.auth.oauth_provider, 'github');
  assert.equal(calls[0].params.user_id, undefined);
  webClient.request = original;
});
test('all four kinds open the shared review, retain moderation result, and never trust install ownership', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  const refs = [];
  const submits = [];
  assetPublishApi.describe = async (ref) => {
    refs.push(ref);
    return description;
  };
  assetPublishApi.prepare = async (...args) => {
    submits.push(args);
    return draft;
  };
  assetPublishApi.commit = async () => ({
    operation_id: 'op',
    execution_status: 'completed',
    result: { publish_result: 'pending_moderation', visibility: null },
    error: null,
  });
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  for (const kind of ['skill', 'agent_template', 'plugin', 'mcp']) {
    await act(async () => openAssetPublish({ kind, local_id: 'demo' }));
    await tick();
    assert.ok(find('asset-publish-drawer'));
    assert.equal(find('asset-publish-target').value, '');
    await act(async () =>
      find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
    );
    await tick();
    assert.ok(find('asset-publish-review'));
    assert.equal(submits.at(-1)[2], '');
    assert.equal(submits.at(-1)[3], false);
    await act(async () => find('asset-publish-commit').click());
    await tick();
    assert.equal(find('asset-publish-result').dataset.variant, 'pending_moderation');
    assert.match(find('asset-publish-visibility-unconfirmed').textContent, /without confirming visibility/);
    await act(async () => find('asset-publish-close').click());
  }
  assert.deepEqual(
    refs.map((ref) => ref.kind),
    ['skill', 'agent_template', 'plugin', 'mcp'],
  );
});
test('account change discards an in-flight prepare result', async () => {
  let resolve;
  assetPublishApi.prepare = () => new Promise((r) => (resolve = r));
  await act(async () => openAssetPublish({ kind: 'mcp', local_id: 'custom' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await act(async () => {
    sessionStorage.setItem('marketplace_oauth_access_token', 'different-token');
    window.dispatchEvent(new dom.window.Event('oauth-callback-complete'));
  });
  await act(async () => resolve(draft));
  await tick();
  assert.equal(find('asset-publish-commit').disabled, true);
  await act(async () => find('asset-publish-close').click());
});
test('unresolved submission cannot be replaced by old history or lose its retry id', async () => {
  const ids = [];
  const old = {
    operation_id: 'old',
    draft_id: 'old-draft',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '0.9.0' },
    error: null,
  };
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async (_draft, id) => {
    ids.push(id);
    throw Object.assign(new Error('Timed out'), { code: 'REQUEST_TIMEOUT' });
  };
  assetPublishApi.records = async () => ({ records: [old] });
  await act(async () => openAssetPublish({ kind: 'plugin', local_id: 'uncertain' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  await act(async () => find('asset-publish-commit').click());
  await tick();
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.equal(find('asset-publish-record').disabled, true);
  assert.equal(find('asset-publish-edit').disabled, true);
  await act(async () => find('asset-publish-record').click());
  assert.equal(!!find('asset-publish-new-version'), false);
  await act(async () => find('asset-publish-commit').click());
  await tick();
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
  assetPublishApi.records = async () => ({
    records: [
      old,
      {
        ...old,
        operation_id: 'recovered',
        draft_id: 'draft',
        result: { publish_result: 'pending_moderation', version: '1.0.0' },
      },
    ],
  });
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.equal(find('asset-publish-result').dataset.variant, 'pending_moderation');
  assert.ok(find('asset-publish-new-version'));
  await act(async () => find('asset-publish-close').click());
});
test('definitive expired draft rejection unlocks editing and requires a new package check', async () => {
  for (const rejection of [
    Object.assign(new Error('Rejected'), { code: 'DRAFT_EXPIRED' }),
    new Error('draft_expired'),
  ]) {
    assetPublishApi.describe = async () => description;
    assetPublishApi.prepare = async () => draft;
    assetPublishApi.commit = async () => {
      throw rejection;
    };
    await act(async () => openAssetPublish({ kind: 'skill', local_id: 'expired' }));
    await tick();
    await act(async () =>
      find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
    );
    await tick();
    await act(async () => find('asset-publish-commit').click());
    await tick();
    assert.equal(find('asset-publish-edit').disabled, false);
    assert.equal(find('asset-publish-commit').disabled, true);
    assert.match(find('asset-publish-error').textContent, /expired/i);
    await act(async () => find('asset-publish-edit').click());
    assert.ok(find('asset-publish-prepare'));
    await act(async () => find('asset-publish-close').click());
  }
});
test('polling completion updates the matching history row', async () => {
  const queued = { operation_id: 'polled', draft_id: 'draft', execution_status: 'queued', result: null, error: null };
  assetPublishApi.describe = async () => ({ ...description, records: [queued] });
  assetPublishApi.status = async () => ({
    ...queued,
    execution_status: 'completed',
    result: { publish_result: 'pending_moderation' },
  });
  await act(async () => openAssetPublish({ kind: 'mcp', local_id: 'poll' }));
  await tick();
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 2100));
  });
  assert.match(find('asset-publish-record').textContent, /pending moderation/i);
  await act(async () => find('asset-publish-close').click());
});
test('OAuth restore contains metadata and does not auto prepare or commit', async () => {
  assetPublishApi.describe = async () => description;
  let called = 0;
  assetPublishApi.prepare = async () => {
    called++;
    return draft;
  };
  sessionStorage.setItem(
    'asset-publish-metadata',
    JSON.stringify({
      reference: { kind: 'plugin', local_id: 'restored' },
      metadata: { ...defaults, display_name: 'Restored' },
    }),
  );
  await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
  await tick();
  assert.equal(find('asset-publish-display-name').value, 'Restored');
  assert.equal(called, 0);
  assert.equal(sessionStorage.getItem('asset-publish-metadata'), null);
  await act(async () => root.unmount());
  dom.window.close();
});
