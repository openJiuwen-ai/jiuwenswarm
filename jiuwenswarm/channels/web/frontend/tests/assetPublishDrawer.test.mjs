import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
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
const { createRoot } = await import('react-dom/client');
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
test('publish form uses compact resource, account, grouped settings, and folded secondary content', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  sessionStorage.setItem(
    'marketplace_oauth_user',
    JSON.stringify({ id: 'user-1', name: 'Alice', login: 'alice', avatar_url: '', is_market_moderation_admin: false }),
  );
  assetPublishApi.describe = async () => ({
    ...description,
    hub_url: 'https://swarmskills.openjiuwen.com/',
  });
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () =>
    openAssetPublish({
      kind: 'skill',
      local_id: 'demo-skill',
      avatar_url: 'data:image/png;base64,ZGVtby1hdmF0YXI=',
    }),
  );
  await tick();

  assert.match(find('asset-publish-resource-summary').textContent, /Demo/);
  assert.match(find('asset-publish-resource-summary').textContent, /Skill/);
  assert.equal(find('asset-publish-resource-avatar-image').getAttribute('src'), 'data:image/png;base64,ZGVtby1hdmF0YXI=');
  assert.equal(find('asset-publish-resource-id').title, 'demo-skill');
  assert.match(find('asset-publish-account-card').textContent, /GitCode/);
  assert.equal(find('asset-publish-provider-icon').dataset.variant, 'gitcode');
  assert.equal(find('asset-publish-provider-icon').getAttribute('alt'), '');
  assert.match(find('asset-publish-account-card').textContent, /Alice/);
  assert.match(find('asset-publish-account-card').textContent, /swarmskills\.openjiuwen\.com/);
  assert.ok(find('asset-publish-switch-account'));
  assert.equal(document.querySelectorAll('[data-testid="asset-publish-provider"]').length, 0);

  assert.ok(document.querySelector('[data-testid="asset-publish-form-section"][data-variant="basic"]'));
  assert.ok(document.querySelector('[data-testid="asset-publish-form-section"][data-variant="publish"]'));
  assert.ok(document.querySelector('[data-testid="asset-publish-form-section"][data-variant="advanced"]'));
  assert.equal(document.querySelectorAll('[data-testid="asset-publish-visibility-option"]').length, 2);
  assert.equal(find('asset-publish-visibility'), null);
  assert.ok(find('asset-publish-records-fold'));
  assert.equal(find('asset-publish-records-fold').open, false);
  assert.match(find('asset-publish-records-fold').textContent, /Local publishing history/);
  assert.doesNotMatch(find('asset-publish-records-fold').querySelector('summary').textContent, /\(0\)/);
  assert.match(find('asset-publish-records-help').dataset.tooltip, /does not represent the latest Hub moderation status/);
  assert.equal(find('asset-publish-records-help').closest('summary') !== null, true);
  assert.match(find('asset-publish-advanced-help').dataset.tooltip, /updating an existing resource/);
  assert.equal(find('asset-publish-advanced-help').closest('summary') !== null, true);
  assert.equal(find('asset-publish-advanced-help').className, find('asset-publish-records-help').className);
  assert.equal(find('asset-publish-refresh').closest('[data-testid="asset-publish-records-fold"]') !== null, true);
  assert.equal(find('asset-publish-refresh').closest('[data-testid="asset-publish-records-toolbar"]') !== null, true);
  assert.match(find('asset-publish-records').textContent, /No local publishing records/);
  assert.equal(find('asset-publish-tags').closest('label').textContent.includes('comma separated'), false);
  const hints = [...document.querySelectorAll('[data-testid="asset-publish-field-hint"]')];
  assert.equal(hints.length, 4);
  assert.ok(hints.every((hint) => hint.dataset.tooltip?.length > 20));
  assert.ok(hints.every((hint) => hint.tabIndex === 0));

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_user');
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
    result: { publish_result: 'pending_moderation', visibility: null, asset_id: 'hub-asset', version: '1.0.0' },
    error: null,
    updated_at: '2026-09-16T06:49:59Z',
  });
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  for (const kind of ['skill', 'agent_template', 'agent_group', 'plugin', 'mcp']) {
    await act(async () => openAssetPublish({ kind, local_id: 'demo' }));
    await tick();
    assert.ok(find('asset-publish-drawer'));
    assert.equal(find('asset-publish-target').value, '');
    await act(async () =>
      find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
    );
    await tick();
    assert.ok(find('asset-publish-review'));
    assert.equal(find('asset-publish-notice'), null);
    assert.equal(submits.at(-1)[2], '');
    assert.equal(submits.at(-1)[3], false);
    await act(async () => find('asset-publish-commit').click());
    await tick();
    assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
    assert.ok(find('asset-publish-result-header'));
    assert.ok(find('asset-publish-result-metadata'));
    assert.match(find('asset-publish-result-metadata').textContent, /hub-asset/);
    assert.match(find('asset-publish-result-metadata').textContent, /1\.0\.0/);
    assert.ok(find('asset-publish-result-footer'));
    assert.equal(find('asset-publish-result-edit').closest('[data-testid="asset-publish-drawer-footer"]') !== null, true);
    assert.equal(find('asset-publish-notice'), null);
    assert.equal(find('asset-publish-visibility-unconfirmed'), null);
    await act(async () => find('asset-publish-close').click());
  }
  assert.deepEqual(
    refs.map((ref) => ref.kind),
    ['skill', 'agent_template', 'agent_group', 'plugin', 'mcp'],
  );
});
test('review lists show file paths and notes verbatim', async () => {
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => ({
    ...draft,
    files: ['SKILL.md', 'templates/a.md'],
    excluded: ['.env'],
    normalizations: ['manifest.display_name: generated zh/en'],
    dependencies: [{ kind: 'skill', path: 'skills/demo', embedded: true }],
  });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'verbatim' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  const items = [...document.querySelectorAll('[data-testid="asset-publish-review-item"]')].map((node) => node.textContent);
  assert.ok(items.includes('SKILL.md'));
  assert.ok(items.includes('.env'));
  assert.ok(items.some((value) => value.startsWith('manifest.display_name')));
  // Dependencies are objects; they must be summarized, not turned into a failure message.
  assert.ok(items.some((value) => value.startsWith('kind: skill') && value.includes('skills/demo')));
  assert.ok(!items.some((value) => /请求未完成|The request did not complete/.test(value)));
  await act(async () => find('asset-publish-close').click());
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
  assert.equal(!!find('asset-publish-result'), false);
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
  assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
  assert.ok(find('asset-publish-result-edit'));
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
test('Hub request rejection identifies Hub and gives actionable guidance', async () => {
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async () => ({
    operation_id: 'hub-rejected-operation',
    draft_id: 'draft',
    execution_status: 'failed',
    result: null,
    error: { code: 'hub_request_failed' },
    updated_at: '2026-09-24T06:00:00Z',
  });
  await act(async () => openAssetPublish({ kind: 'agent_template', local_id: 'hub-rejected' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  await act(async () => find('asset-publish-commit').click());
  await tick();
  const message = document.querySelector('.asset-publish-result-error').textContent;
  assert.match(message, /Hub did not accept this publishing request/);
  assert.match(message, /contact the Hub administrator/);
  assert.doesNotMatch(message, /hub_request_failed/i);
  await act(async () => find('asset-publish-close').click());
});
test('record errors show the Hub guidance instead of the generic fallback', async () => {
  const record = {
    operation_id: 'hub-guidance',
    execution_status: 'failed',
    result: null,
    error: {
      code: 'hub_request_failed',
      message: '发布新版本时不能修改 Skill 可见性，请沿用现有设置',
    },
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'hub-guidance' }));
  await tick();
  // The result card is opened from the history list instead of covering the form on open.
  await act(async () => find('asset-publish-record').click());
  const message = document.querySelector('.asset-publish-result-error').textContent;
  assert.equal(message, '发布新版本时不能修改 Skill 可见性，请沿用现有设置');
  assert.doesNotMatch(message, /Retry or sign in again/);
  await act(async () => find('asset-publish-close').click());
});
test('curated local text keeps priority for known codes', async () => {
  const record = {
    operation_id: 'version-clash',
    execution_status: 'failed',
    result: null,
    error: { code: 'VERSION_CONFLICT', message: 'hub-only wording' },
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'version-clash' }));
  await tick();
  // The result card is opened from the history list instead of covering the form on open.
  await act(async () => find('asset-publish-record').click());
  const message = document.querySelector('.asset-publish-result-error').textContent;
  assert.match(message, /This version exists/);
  assert.doesNotMatch(message, /hub-only wording/);
  await act(async () => find('asset-publish-close').click());
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
  const polledRow = find('asset-publish-record').textContent;
  assert.match(polledRow, /submitted/i);
  assert.doesNotMatch(polledRow, /pending moderation/i);
  await act(async () => find('asset-publish-close').click());
});
<<<<<<< HEAD
test('completed records use the same local text, variants, and icon regardless of Hub result', async () => {
  for (const publishResult of ['pending_moderation', 'published', 'unrecognized']) {
    const record = {
      operation_id: `local-${publishResult}`,
      execution_status: 'completed',
      result: { publish_result: publishResult, version: '1.0.0' },
      error: null,
    };
    assetPublishApi.describe = async () => ({ ...description, records: [record] });
    assetPublishApi.records = async () => ({ records: [record] });
    await act(async () => openAssetPublish({ kind: 'plugin', local_id: record.operation_id }));
    await tick();

    // The drawer opens on the form now, the result card is opened from the history list.
    await act(async () => find('asset-publish-record').click());
    assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
    assert.equal(find('asset-publish-result-status').textContent, 'Submitted');
    assert.ok(find('asset-publish-result-icon').querySelector('svg.lucide-circle-check'));
    const historyRow = find('asset-publish-record');
    assert.equal(historyRow.dataset.variant, record.operation_id);
    const status = find('asset-publish-record-status');
    assert.equal(status.textContent, 'Submitted');
    assert.equal(status.dataset.variant, 'submitted');
    await act(async () => find('asset-publish-close').click());
  }
});
test('unconfirmed submissions keep their local variant and waiting icon', async () => {
  const record = {
    operation_id: 'local-unconfirmed',
    execution_status: 'unknown',
    result: null,
    error: null,
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'plugin', local_id: record.operation_id }));
  await tick();

  // Opening the history record is what shows the result card.
  await act(async () => find('asset-publish-record').click());
  assert.equal(find('asset-publish-result').dataset.variant, 'unconfirmed');
  assert.equal(find('asset-publish-result-status').textContent, 'Submission result unconfirmed');
  assert.ok(find('asset-publish-result-icon').querySelector('svg.lucide-clock-3'));
  const status = find('asset-publish-record-status');
  assert.equal(status.textContent, 'Submission result unconfirmed');
  assert.equal(status.dataset.variant, 'unconfirmed');
  // An unverified outcome must not lock the asset; the drawer keeps an exit.
  const entry = find('asset-publish-result-edit');
  assert.ok(entry);
  assert.equal(entry.disabled, false);
  assert.equal(find('asset-publish-record').disabled, false);
  await act(async () => entry.click());
  await tick();
  assert.equal(find('asset-publish-result'), null);
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-close').click());
});
test('opening the drawer with history lands on the form and the result card reopens on demand', async () => {
  const record = {
    operation_id: 'history-only',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'history-only' }));
  await tick();

  // A stale record must not cover the form; the drawer opens on the editable fields.
  assert.ok(find('asset-publish-form'));
  assert.equal(!!find('asset-publish-result'), false);
  assert.equal(find('asset-publish-version').value, '1.0.0');

  // The result of a history record is still one click away.
  await act(async () => find('asset-publish-record').click());
  assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
  assert.match(find('asset-publish-result-metadata').textContent, /2\.0\.0/);

  // "Back to editing" sits in the drawer footer, left of "close", not inside the card.
  assert.equal(find('asset-publish-result-edit').closest('[data-testid="asset-publish-drawer-footer"]') !== null, true);
  assert.equal(find('asset-publish-result-edit').nextElementSibling.dataset.testid, 'asset-publish-done');
  assert.equal(find('asset-publish-result-footer').querySelector('button'), null);
  assert.equal(find('asset-publish-result-edit').textContent, 'Back to editing');

  // "Back to editing" restores the form and keeps the entered values.
  await act(async () => find('asset-publish-result-edit').click());
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  assert.equal(find('asset-publish-version').value, '1.0.0');
  await act(async () => find('asset-publish-close').click());
});
test('refreshing from the review panel keeps the checked package on screen', async () => {
  const record = {
    operation_id: 'review-refresh',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'review-refresh' }));
  await tick();
  assert.ok(find('asset-publish-form'));

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.ok(find('asset-publish-review'));

  // The history record written by the refresh must not hide the checked package nor empty the
  // action bar: the review panel, the form and the result card all follow the same condition.
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.ok(find('asset-publish-review'));
  assert.ok(find('asset-publish-commit'));
  assert.equal(!!find('asset-publish-form'), false);
  assert.equal(!!find('asset-publish-result'), false);
  await act(async () => find('asset-publish-close').click());
});
test('a definitively rejected submission does not arm the result card for the next refresh', async () => {
  const historical = {
    operation_id: 'kept-history',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  assetPublishApi.describe = async () => ({ ...description, records: [historical] });
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async () => {
    throw Object.assign(new Error('Rejected'), { code: 'DRAFT_EXPIRED' });
  };
  assetPublishApi.records = async () => ({ records: [historical] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'rejected-refresh' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  await act(async () => find('asset-publish-commit').click());
  await tick();

  // A definitive rejection never writes a record; going back must clear any "result view" state.
  await act(async () => find('asset-publish-edit').click());
  await tick();
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  assert.equal(find('asset-publish-version').value, '1.0.0');
  await act(async () => find('asset-publish-close').click());
});
test('changing the account from the result view keeps the form for the next account', async (t) => {
  const record = {
    operation_id: 'account-switch',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'first-account');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'account-switch' }));
  await tick();
  await act(async () => find('asset-publish-record').click());
  assert.ok(find('asset-publish-result'));

  await act(async () => find('asset-publish-sign-out').click());
  await tick();
  // Signing back in replays the mount effect, which selects the newest record again; that must not
  // reopen the card over the form the user is about to fill in.
  await act(async () => {
    sessionStorage.setItem('marketplace_oauth_access_token', 'second-account');
    window.dispatchEvent(new dom.window.Event('oauth-callback-complete'));
  });
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-close').click());
});
test('logged-out publishing loads local names for all five kinds and preserves edits after login', async () => {
  for (const kind of ['skill', 'agent_template', 'agent_group', 'plugin', 'mcp']) {
    sessionStorage.removeItem('marketplace_oauth_access_token');
    let resolveDescription;
    assetPublishApi.describe = () => new Promise((resolve) => {
      resolveDescription = resolve;
    });
    await act(async () => openAssetPublish({ kind, local_id: '1022' }));
    await tick();
    try {
      assert.equal(find('asset-publish-display-name').closest('fieldset').disabled, true);
      assert.equal(typeof resolveDescription, 'function');
      await act(async () => resolveDescription({
        ...description,
        defaults: { ...defaults, display_name: '中文资产', description: '本地介绍' },
      }));
      assert.equal(find('asset-publish-display-name').value, '中文资产');
      assert.equal(find('asset-publish-description').value, '本地介绍');
      assert.equal(find('asset-publish-prepare').disabled, true);
      const input = find('asset-publish-display-name');
      await act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, '用户修改名称');
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
        input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
        input.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true }));
      });
      assetPublishApi.describe = async () => description;
      await act(async () => {
        sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
        window.dispatchEvent(new dom.window.Event('storage'));
      });
      await tick();
      assert.equal(find('asset-publish-display-name').value, '用户修改名称');
    } finally {
      await act(async () => find('asset-publish-close').click());
    }
  }
});
test('signing out clears the stored Hub authorization and returns to the sign-in state', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'sign-out-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  sessionStorage.setItem(
    'marketplace_oauth_user',
    JSON.stringify({ id: 'user-1', name: 'Alice', login: 'alice', avatar_url: '', is_market_moderation_admin: false }),
  );
  assetPublishApi.describe = async () => description;
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'sign-out-demo' }));
  await tick();

  assert.ok(find('asset-publish-account-card'));
  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), null);
  assert.equal(sessionStorage.getItem('marketplace_oauth_provider'), null);
  assert.equal(sessionStorage.getItem('marketplace_oauth_user'), null);
  assert.equal(find('asset-publish-account-card'), null);
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
});
test('signing out from the review panel returns to the editable form', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'review-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'review-sign-out' }));
  await tick();

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.ok(find('asset-publish-review'));

  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  // The review panel must not survive without a draft behind it.
  assert.equal(find('asset-publish-review'), null);
  assert.ok(find('asset-publish-form'));
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
test('signing out cancels an authorization that is still starting', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'pending-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  const originalConfirm = dom.window.confirm;
  const originalFetch = globalThis.fetch;
  const originalOpen = dom.window.open;
  const calls = [];
  let releaseStart;
  let tabClosed = false;
  const tab = {
    location: { href: '' },
    opener: null,
    closed: false,
    close() {
      tabClosed = true;
    },
  };
  t.after(() => {
    dom.window.confirm = originalConfirm;
    globalThis.fetch = originalFetch;
    dom.window.open = originalOpen;
  });
  dom.window.confirm = () => true;
  dom.window.open = () => tab;
  const response = (payload) => ({ ok: true, text: async () => JSON.stringify(payload) });
  globalThis.fetch = (url) => {
    const target = String(url);
    calls.push(target);
    if (target.includes('/hub/start')) {
      return new Promise((resolve) => {
        releaseStart = () =>
          resolve(response({ authorize_url: 'https://hub.test/auth', flow: 'flow-1', claim: 'claim-1' }));
      });
    }
    // A late poll result: storing it would silently undo the sign-out.
    return Promise.resolve(response({ status: 'complete', access_token: 'late-token', provider: 'gitcode' }));
  };
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'pending-sign-out' }));
  await tick();

  await act(async () => find('asset-publish-switch-account').click());
  await act(async () => find('asset-publish-provider').click());
  await tick();
  assert.deepEqual(calls, ['/marketplace-oauth/hub/start']);

  await act(async () => find('asset-publish-sign-out').click());
  await act(async () => releaseStart());
  // Wait out one polling interval so a surviving poll would actually run and store the token.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1300));
  });
  await tick();

  assert.equal(tabClosed, true);
  assert.deepEqual(calls, ['/marketplace-oauth/hub/start']);
  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), null);
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
});
test('sign-out stays disabled while a submission result is unconfirmed', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'uncertain-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async () => {
    throw Object.assign(new Error('Timed out'), { code: 'REQUEST_TIMEOUT' });
  };
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'plugin', local_id: 'sign-out-uncertain' }));
  await tick();

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.equal(find('asset-publish-sign-out').disabled, false);

  await act(async () => find('asset-publish-commit').click());
  await tick();

  // The local record is the only place to check an unconfirmed submission; signing out would drop it.
  assert.equal(find('asset-publish-sign-out').disabled, true);

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
test('cancelling sign-out keeps the stored Hub authorization and its records', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'keep-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'github');
  let describeCalls = 0;
  assetPublishApi.describe = async () => {
    describeCalls++;
    return description;
  };
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => false;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'keep-demo' }));
  await tick();
  const callsBefore = describeCalls;

  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), 'keep-token');
  assert.ok(find('asset-publish-account-card'));
  assert.equal(describeCalls, callsBefore);

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
||||||| e487e9a30
=======
test('completed records use the same local text, variants, and icon regardless of Hub result', async () => {
  for (const publishResult of ['pending_moderation', 'published', 'unrecognized']) {
    const record = {
      operation_id: `local-${publishResult}`,
      execution_status: 'completed',
      result: { publish_result: publishResult, version: '1.0.0' },
      error: null,
    };
    assetPublishApi.describe = async () => ({ ...description, records: [record] });
    assetPublishApi.records = async () => ({ records: [record] });
    await act(async () => openAssetPublish({ kind: 'plugin', local_id: record.operation_id }));
    await tick();

    // The drawer opens on the form now, the result card is opened from the history list.
    await act(async () => find('asset-publish-record').click());
    assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
    assert.equal(find('asset-publish-result-status').textContent, 'Submitted');
    assert.ok(find('asset-publish-result-icon').querySelector('svg.lucide-circle-check'));
    const historyRow = find('asset-publish-record');
    assert.equal(historyRow.dataset.variant, record.operation_id);
    const status = find('asset-publish-record-status');
    assert.equal(status.textContent, 'Submitted');
    assert.equal(status.dataset.variant, 'submitted');
    await act(async () => find('asset-publish-close').click());
  }
});
test('unconfirmed submissions keep their local variant and waiting icon', async () => {
  const record = {
    operation_id: 'local-unconfirmed',
    execution_status: 'unknown',
    result: null,
    error: null,
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'plugin', local_id: record.operation_id }));
  await tick();

  // Opening the history record is what shows the result card.
  await act(async () => find('asset-publish-record').click());
  assert.equal(find('asset-publish-result').dataset.variant, 'unconfirmed');
  assert.equal(find('asset-publish-result-status').textContent, 'Submission result unconfirmed');
  assert.ok(find('asset-publish-result-icon').querySelector('svg.lucide-clock-3'));
  const status = find('asset-publish-record-status');
  assert.equal(status.textContent, 'Submission result unconfirmed');
  assert.equal(status.dataset.variant, 'unconfirmed');
  // An unverified outcome must not lock the asset; the drawer keeps an exit.
  const entry = find('asset-publish-result-edit');
  assert.ok(entry);
  assert.equal(entry.disabled, false);
  assert.equal(find('asset-publish-record').disabled, false);
  await act(async () => entry.click());
  await tick();
  assert.equal(find('asset-publish-result'), null);
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-close').click());
});
test('undefined placeholders show a localized package error without asking for a new login', async () => {
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => {
    throw Object.assign(new Error('privatevalue'), { code: 'UNDEFINED_PLACEHOLDER' });
  };
  let commitCalls = 0;
  assetPublishApi.commit = async () => { commitCalls++; };
  for (const language of ['zh', 'en']) {
    await act(async () => i18next.changeLanguage(language));
    await act(async () => openAssetPublish({ kind: 'mcp', local_id: 'canva' }));
    await tick();
    await act(async () =>
      find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
    );
    await tick();
    const message = find('asset-publish-error').textContent;
    assert.match(message, language === 'zh' ? /未声明的参数/ : /undeclared parameter/);
    assert.doesNotMatch(message, /privatevalue|请求未完成|The request did not complete/);
    assert.equal(find('asset-publish-commit').disabled, true);
    assert.equal(commitCalls, 0);
    await act(async () => find('asset-publish-close').click());
  }
  await act(async () => i18next.changeLanguage('en'));
});

test('persisted Hub MCP rejection shows a package error in both languages without resubmitting', async () => {
  const record = {
    operation_id: 'canva-failed',
    execution_status: 'failed',
    result: null,
    error: { code: 'invalid_agent_mcp', http_status: 400, message: 'privatevalue' },
    version: '1.0.0',
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  let calls = 0;
  assetPublishApi.commit = async () => { calls++; };
  assetPublishApi.prepare = async () => { calls++; };
  for (const language of ['zh', 'en']) {
    await act(async () => i18next.changeLanguage(language));
    await act(async () => openAssetPublish({ kind: 'mcp', local_id: 'canva' }));
    await tick();
    // The drawer lands on the form now; the persisted result is one click away.
    await act(async () => find('asset-publish-record').click());
    await tick();
    const content = find('asset-publish-result').textContent;
    assert.match(content, /invalid_agent_mcp/);
    assert.match(content, language === 'zh' ? /包校验/ : /package validation/);
    assert.doesNotMatch(content, /privatevalue|请求未完成|The request did not complete/);
    assert.equal(calls, 0);
    await act(async () => find('asset-publish-close').click());
  }
  await act(async () => i18next.changeLanguage('en'));
});

test('opening the drawer with history lands on the form and the result card reopens on demand', async () => {
  await act(async () => i18next.changeLanguage('en'));
  const record = {
    operation_id: 'history-only',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'history-only' }));
  await tick();

  // A stale record must not cover the form; the drawer opens on the editable fields.
  assert.ok(find('asset-publish-form'));
  assert.equal(!!find('asset-publish-result'), false);
  assert.equal(find('asset-publish-version').value, '1.0.0');

  // The result of a history record is still one click away.
  await act(async () => find('asset-publish-record').click());
  assert.equal(find('asset-publish-result').dataset.variant, 'submitted');
  assert.match(find('asset-publish-result-metadata').textContent, /2\.0\.0/);

  // "Back to editing" sits in the drawer footer, left of "close", not inside the card.
  assert.equal(find('asset-publish-result-edit').closest('[data-testid="asset-publish-drawer-footer"]') !== null, true);
  assert.equal(find('asset-publish-result-edit').nextElementSibling.dataset.testid, 'asset-publish-done');
  assert.equal(find('asset-publish-result-footer').querySelector('button'), null);
  assert.equal(find('asset-publish-result-edit').textContent, 'Back to editing');

  // "Back to editing" restores the form and keeps the entered values.
  await act(async () => find('asset-publish-result-edit').click());
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  assert.equal(find('asset-publish-version').value, '1.0.0');
  await act(async () => find('asset-publish-close').click());
});
test('refreshing from the review panel keeps the checked package on screen', async () => {
  const record = {
    operation_id: 'review-refresh',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.records = async () => ({ records: [record] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'review-refresh' }));
  await tick();
  assert.ok(find('asset-publish-form'));

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.ok(find('asset-publish-review'));

  // The history record written by the refresh must not hide the checked package nor empty the
  // action bar: the review panel, the form and the result card all follow the same condition.
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.ok(find('asset-publish-review'));
  assert.ok(find('asset-publish-commit'));
  assert.equal(!!find('asset-publish-form'), false);
  assert.equal(!!find('asset-publish-result'), false);
  await act(async () => find('asset-publish-close').click());
});
test('a definitively rejected submission does not arm the result card for the next refresh', async () => {
  const historical = {
    operation_id: 'kept-history',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  assetPublishApi.describe = async () => ({ ...description, records: [historical] });
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async () => {
    throw Object.assign(new Error('Rejected'), { code: 'DRAFT_EXPIRED' });
  };
  assetPublishApi.records = async () => ({ records: [historical] });
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'rejected-refresh' }));
  await tick();
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  await act(async () => find('asset-publish-commit').click());
  await tick();

  // A definitive rejection never writes a record; going back must clear any "result view" state.
  await act(async () => find('asset-publish-edit').click());
  await tick();
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-refresh').click());
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  assert.equal(find('asset-publish-version').value, '1.0.0');
  await act(async () => find('asset-publish-close').click());
});
test('changing the account from the result view keeps the form for the next account', async (t) => {
  const record = {
    operation_id: 'account-switch',
    execution_status: 'completed',
    result: { publish_result: 'published', version: '2.0.0' },
    error: null,
  };
  sessionStorage.setItem('marketplace_oauth_access_token', 'first-account');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => ({ ...description, records: [record] });
  assetPublishApi.records = async () => ({ records: [record] });
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'account-switch' }));
  await tick();
  await act(async () => find('asset-publish-record').click());
  assert.ok(find('asset-publish-result'));

  await act(async () => find('asset-publish-sign-out').click());
  await tick();
  // Signing back in replays the mount effect, which selects the newest record again; that must not
  // reopen the card over the form the user is about to fill in.
  await act(async () => {
    sessionStorage.setItem('marketplace_oauth_access_token', 'second-account');
    window.dispatchEvent(new dom.window.Event('oauth-callback-complete'));
  });
  await tick();
  assert.equal(!!find('asset-publish-result'), false);
  assert.ok(find('asset-publish-form'));
  await act(async () => find('asset-publish-close').click());
});
test('logged-out publishing loads local names for all five kinds and preserves edits after login', async () => {
  for (const kind of ['skill', 'agent_template', 'agent_group', 'plugin', 'mcp']) {
    sessionStorage.removeItem('marketplace_oauth_access_token');
    let resolveDescription;
    assetPublishApi.describe = () => new Promise((resolve) => {
      resolveDescription = resolve;
    });
    await act(async () => openAssetPublish({ kind, local_id: '1022' }));
    await tick();
    try {
      assert.equal(find('asset-publish-display-name').closest('fieldset').disabled, true);
      assert.equal(typeof resolveDescription, 'function');
      await act(async () => resolveDescription({
        ...description,
        defaults: { ...defaults, display_name: '中文资产', description: '本地介绍' },
      }));
      assert.equal(find('asset-publish-display-name').value, '中文资产');
      assert.equal(find('asset-publish-description').value, '本地介绍');
      assert.equal(find('asset-publish-prepare').disabled, true);
      const input = find('asset-publish-display-name');
      await act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, '用户修改名称');
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
        input.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
        input.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true }));
      });
      assetPublishApi.describe = async () => description;
      await act(async () => {
        sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
        window.dispatchEvent(new dom.window.Event('storage'));
      });
      await tick();
      assert.equal(find('asset-publish-display-name').value, '用户修改名称');
    } finally {
      await act(async () => find('asset-publish-close').click());
    }
  }
});
test('signing out clears the stored Hub authorization and returns to the sign-in state', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'sign-out-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  sessionStorage.setItem(
    'marketplace_oauth_user',
    JSON.stringify({ id: 'user-1', name: 'Alice', login: 'alice', avatar_url: '', is_market_moderation_admin: false }),
  );
  assetPublishApi.describe = async () => description;
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'sign-out-demo' }));
  await tick();

  assert.ok(find('asset-publish-account-card'));
  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), null);
  assert.equal(sessionStorage.getItem('marketplace_oauth_provider'), null);
  assert.equal(sessionStorage.getItem('marketplace_oauth_user'), null);
  assert.equal(find('asset-publish-account-card'), null);
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
});
test('signing out from the review panel returns to the editable form', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'review-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => true;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'review-sign-out' }));
  await tick();

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.ok(find('asset-publish-review'));

  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  // The review panel must not survive without a draft behind it.
  assert.equal(find('asset-publish-review'), null);
  assert.ok(find('asset-publish-form'));
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
test('signing out cancels an authorization that is still starting', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'pending-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  const originalConfirm = dom.window.confirm;
  const originalFetch = globalThis.fetch;
  const originalOpen = dom.window.open;
  const calls = [];
  let releaseStart;
  let tabClosed = false;
  const tab = {
    location: { href: '' },
    opener: null,
    closed: false,
    close() {
      tabClosed = true;
    },
  };
  t.after(() => {
    dom.window.confirm = originalConfirm;
    globalThis.fetch = originalFetch;
    dom.window.open = originalOpen;
  });
  dom.window.confirm = () => true;
  dom.window.open = () => tab;
  const response = (payload) => ({ ok: true, text: async () => JSON.stringify(payload) });
  globalThis.fetch = (url) => {
    const target = String(url);
    calls.push(target);
    if (target.includes('/hub/start')) {
      return new Promise((resolve) => {
        releaseStart = () =>
          resolve(response({ authorize_url: 'https://hub.test/auth', flow: 'flow-1', claim: 'claim-1' }));
      });
    }
    // A late poll result: storing it would silently undo the sign-out.
    return Promise.resolve(response({ status: 'complete', access_token: 'late-token', provider: 'gitcode' }));
  };
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'pending-sign-out' }));
  await tick();

  await act(async () => find('asset-publish-switch-account').click());
  await act(async () => find('asset-publish-provider').click());
  await tick();
  assert.deepEqual(calls, ['/marketplace-oauth/hub/start']);

  await act(async () => find('asset-publish-sign-out').click());
  await act(async () => releaseStart());
  // Wait out one polling interval so a surviving poll would actually run and store the token.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1300));
  });
  await tick();

  assert.equal(tabClosed, true);
  assert.deepEqual(calls, ['/marketplace-oauth/hub/start']);
  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), null);
  assert.ok(find('asset-publish-login'));

  await act(async () => find('asset-publish-close').click());
});
test('sign-out stays disabled while a submission result is unconfirmed', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'uncertain-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'gitcode');
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => draft;
  assetPublishApi.commit = async () => {
    throw Object.assign(new Error('Timed out'), { code: 'REQUEST_TIMEOUT' });
  };
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'plugin', local_id: 'sign-out-uncertain' }));
  await tick();

  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.equal(find('asset-publish-sign-out').disabled, false);

  await act(async () => find('asset-publish-commit').click());
  await tick();

  // The local record is the only place to check an unconfirmed submission; signing out would drop it.
  assert.equal(find('asset-publish-sign-out').disabled, true);

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
test('cancelling sign-out keeps the stored Hub authorization and its records', async (t) => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'keep-token');
  sessionStorage.setItem('marketplace_oauth_provider', 'github');
  let describeCalls = 0;
  assetPublishApi.describe = async () => {
    describeCalls++;
    return description;
  };
  const originalConfirm = dom.window.confirm;
  t.after(() => {
    dom.window.confirm = originalConfirm;
  });
  dom.window.confirm = () => false;
  await act(async () => root.render(React.createElement(AssetPublishHost)));
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'keep-demo' }));
  await tick();
  const callsBefore = describeCalls;

  await act(async () => find('asset-publish-sign-out').click());
  await tick();

  assert.equal(sessionStorage.getItem('marketplace_oauth_access_token'), 'keep-token');
  assert.ok(find('asset-publish-account-card'));
  assert.equal(describeCalls, callsBefore);

  await act(async () => find('asset-publish-close').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  sessionStorage.removeItem('marketplace_oauth_provider');
});
test('SkillPack shows one public member plan and resumes review progress in the shared drawer', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  const members = [
    { name: 'notes', version: '1.0.0', action: 'reuse', review: 'approved' },
    { name: 'risk', version: '2.0.0', action: 'publish', visibility: 'public' },
  ];
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => ({ ...draft, members });
  assetPublishApi.commit = async () => ({
    operation_id: 'skillpack-operation', execution_status: 'uploading',
    stage: 'waiting_member_review', members, result: null, error: null,
  });
  await act(async () => openAssetPublish({ kind: 'skillpack', local_id: 'release-pack' }));
  await tick();
  assert.equal(find('asset-publish-force'), null);
  assert.equal(document.querySelectorAll('[name="asset-publish-visibility"]').length, 1);
  await act(async () =>
    find('asset-publish-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })),
  );
  await tick();
  assert.match(find('asset-publish-pack-consent').textContent, /publish.*members publicly/);
  assert.match(find('asset-publish-pack-member-list').textContent, /notes.*1.0.0.*Reuse published/s);
  assert.match(find('asset-publish-pack-member-list').textContent, /risk.*2.0.0.*Publish member first/s);
  await act(async () => find('asset-publish-commit').click());
  await tick();
  assert.match(find('asset-publish-result-status').textContent, /Waiting for member reviews/);
  assert.equal(find('asset-publish-new-version'), null);
  assert.equal(find('asset-publish-pack-consent'), null);
  await act(async () => find('asset-publish-close').click());
});

test('SkillPack action-required stages stop polling and show recovery guidance', async () => {
  const originalTimeout = globalThis.setTimeout;
  const scheduled = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 2000 || delay === 5000) scheduled.push(delay);
    return originalTimeout(callback, delay === 2000 ? 0 : delay, ...args);
  };
  try {
    for (const [stage, code, label] of [
      ['reconcile_upload', 'SKILLPACK_UPLOAD_UNCONFIRMED', /upload.*confirm|confirm.*upload/i],
      ['reauth_required', 'AUTH_REQUIRED', /sign in/i],
    ]) {
      scheduled.length = 0;
      let calls = 0;
      const queued = {
        kind: 'skillpack', operation_id: stage, execution_status: 'uploading',
        stage: 'publishing_members', result: null, error: null,
      };
      assetPublishApi.describe = async () => ({ ...description, records: [queued] });
      assetPublishApi.status = async () => {
        calls++;
        // Also tolerate an older server that still reports uploading here.
        return { ...queued, stage, error: { code } };
      };
      await act(async () => openAssetPublish({ kind: 'skillpack', local_id: stage }));
      await tick();
      await tick();
      assert.equal(calls, 1);
      assert.deepEqual(scheduled, [2000]);
      assert.match(find('asset-publish-result-status').textContent, label);
      assert.ok(find('asset-publish-retry-pack'));
      assert.equal(find('asset-publish-new-version'), null);
      await act(async () => find('asset-publish-close').click());
    }
  } finally {
    globalThis.setTimeout = originalTimeout;
  }
});

test('paused SkillPack retries its persisted operation once without preparing or losing members', async () => {
  for (const [stage, code] of [
    ['reconcile_upload', 'SKILLPACK_UPLOAD_UNCONFIRMED'],
    ['reauth_required', 'AUTH_REQUIRED'],
    ['blocked', 'SKILLPACK_HUB_CHECK_FAILED'],
  ]) {
    const paused = {
      operation_id: `paused-${stage}`, execution_status: 'failed', stage, error: { code }, result: null,
      members: [{ name: 'notes', version: '1.0.0', action: 'publish', review: 'approved' }],
    };
    sessionStorage.setItem('marketplace_oauth_access_token', 'refreshed-token');
    assetPublishApi.describe = async () => ({ ...description, records: [paused] });
    let prepares = 0;
    assetPublishApi.prepare = async () => { prepares++; return draft; };
    const commits = [];
    let finish;
    assetPublishApi.commit = (draftId, requestId) => {
      commits.push({ draftId, requestId });
      return new Promise((resolve) => { finish = resolve; });
    };
    await act(async () => openAssetPublish({ kind: 'skillpack', local_id: stage }));
    await tick();
    assert.equal(commits.length, 0);
    assert.ok(find('asset-publish-form'));
    assert.equal(find('asset-publish-retry-pack'), null);
    await act(async () => find('asset-publish-record').click());
    assert.equal(find('asset-publish-result-edit'), null);
    await act(async () => {
      find('asset-publish-retry-pack').click();
      find('asset-publish-retry-pack').click();
    });
    assert.equal(commits.length, 1);
    assert.equal(commits[0].draftId, paused.operation_id);
    assert.match(commits[0].requestId, /^[a-f0-9-]{36}$/);
    assert.equal(find('asset-publish-retry-pack').disabled, true);
    await act(async () => finish({ ...paused, execution_status: 'uploading', stage: 'waiting_member_review', error: null }));
    await tick();
    assert.equal(prepares, 0);
    assert.equal(find('asset-publish-retry-pack'), null);
    assert.match(find('asset-publish-result-status').textContent, /Waiting for member reviews/);
    assert.match(find('asset-publish-pack-member-list').textContent, /notes.*Approved/s);
    await act(async () => find('asset-publish-close').click());
  }
});

test('retry action is unavailable without login and never applies to ordinary Skill publishing', async () => {
  const paused = {
    operation_id: 'paused-auth', draft_id: 'paused-auth', execution_status: 'failed',
    stage: 'reauth_required', error: { code: 'AUTH_REQUIRED' }, result: null,
  };
  let commits = 0;
  assetPublishApi.commit = async () => { commits++; return paused; };
  assetPublishApi.describe = async () => ({ ...description, records: [paused] });
  sessionStorage.removeItem('marketplace_oauth_access_token');
  await act(async () => openAssetPublish({ kind: 'skillpack', local_id: 'no-login' }));
  await tick();
  assert.ok(find('asset-publish-login'));
  assert.equal(find('asset-publish-retry-pack'), null);
  assert.equal(find('asset-publish-record'), null);
  assert.equal(commits, 0);
  await act(async () => find('asset-publish-close').click());
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  await act(async () => openAssetPublish({ kind: 'skillpack', local_id: 'token-expired' }));
  await tick();
  await act(async () => find('asset-publish-record').click());
  sessionStorage.removeItem('marketplace_oauth_access_token');
  // Recheck authorization at invocation even before the UI receives an auth event.
  await act(async () => find('asset-publish-retry-pack').click());
  assert.equal(commits, 0);
  await act(async () => find('asset-publish-close').click());
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  await act(async () => openAssetPublish({ kind: 'skill', local_id: 'ordinary' }));
  await tick();
  await act(async () => find('asset-publish-record').click());
  assert.equal(find('asset-publish-retry-pack'), null);
  assert.ok(find('asset-publish-result-edit'));
  assert.equal(commits, 0);
  await act(async () => find('asset-publish-close').click());
});

test('SkillPack coerces private defaults and OAuth metadata to the public-only publish scope', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  for (const kind of ['skillpack', 'skill']) {
    for (const restore of [false, true]) {
      assetPublishApi.describe = async () => ({
        ...description, defaults: { ...defaults, visibility: 'private' },
      });
      const prepared = [];
      assetPublishApi.prepare = async (_reference, metadata) => {
        prepared.push(metadata);
        return draft;
      };
      try {
        const reference = { kind, local_id: `visibility-${kind}-${restore}` };
        if (restore) {
          sessionStorage.setItem('asset-publish-metadata', JSON.stringify({
            reference, metadata: { ...defaults, visibility: 'private' },
          }));
          await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
        } else await act(async () => openAssetPublish(reference));
        await tick();
        const selected = document.querySelector('[name="asset-publish-visibility"]:checked');
        assert.ok(selected, `${kind} must display its selected publish scope`);
        assert.equal(prepared.length, 0);
        await act(async () => find('asset-publish-form').dispatchEvent(
          new dom.window.Event('submit', { bubbles: true, cancelable: true }),
        ));
        await tick();
        assert.equal(prepared[0].visibility, kind === 'skillpack' ? 'public' : 'private');
      } finally {
        if (find('asset-publish-close')) await act(async () => find('asset-publish-close').click());
      }
    }
  }
});

test('uncertain SkillPack retry recovers the original operation by polling, without a second commit', async () => {
  const originalTimeout = globalThis.setTimeout;
  const timers = [];
  const scheduled = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay !== 2000 && delay !== 5000) return originalTimeout(callback, delay, ...args);
    scheduled.push({ callback, delay });
    const timer = originalTimeout(() => {}, 60000);
    timers.push(timer);
    return timer;
  };
  try {
    for (const recoveredStage of ['reconcile_upload', 'waiting_member_review']) {
      scheduled.length = 0;
      const paused = {
        operation_id: `retry-${recoveredStage}`, draft_id: `draft-${recoveredStage}`,
        execution_status: 'failed', stage: 'reconcile_upload',
        error: { code: 'SKILLPACK_UPLOAD_UNCONFIRMED' }, result: null,
        members: [{ name: 'notes', version: '1.0.0', action: 'publish', review: 'approved' }],
      };
      sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
      assetPublishApi.describe = async () => ({ ...description, records: [paused] });
      let commits = 0;
      let rejectCommit;
      assetPublishApi.commit = (draftId) => {
        commits++;
        assert.equal(draftId, paused.draft_id);
        return new Promise((_resolve, reject) => { rejectCommit = reject; });
      };
      const statusIds = [];
      assetPublishApi.status = async (operationId) => {
        statusIds.push(operationId);
        if (statusIds.length === 1) throw new Error('network unavailable');
        return {
          ...paused, stage: recoveredStage,
          execution_status: recoveredStage === 'reconcile_upload' ? 'failed' : 'uploading',
        };
      };
      await act(async () => openAssetPublish({ kind: 'skillpack', local_id: recoveredStage }));
      await tick();
      await act(async () => find('asset-publish-record').click());
      await act(async () => find('asset-publish-retry-pack').click());
      assert.equal(scheduled.length, 0, 'do not poll stale status while the retry request is in flight');
      await act(async () => rejectCommit(new Error('response lost')));
      await tick();
      assert.equal(find('asset-publish-retry-pack').disabled, true);
      assert.equal(scheduled.length, 1, 'uncertain retry must start status recovery');
      assert.equal(scheduled[0].delay, 2000);
      await act(async () => scheduled[0].callback());
      assert.equal(find('asset-publish-retry-pack').disabled, true, 'status failure must keep the operation locked');
      assert.equal(scheduled[1].delay, 5000);
      await act(async () => scheduled[1].callback());
      await tick();
      assert.deepEqual(statusIds, [paused.operation_id, paused.operation_id]);
      assert.equal(commits, 1);
      if (recoveredStage === 'reconcile_upload') {
        assert.equal(find('asset-publish-retry-pack').disabled, false);
        assert.equal(scheduled.length, 2, 'confirmed paused operations do not poll forever');
      } else {
        assert.equal(find('asset-publish-retry-pack'), null);
        assert.match(find('asset-publish-result-status').textContent, /Waiting for member reviews/);
        assert.ok(scheduled.length > 2, 'resumed operations continue normal status polling');
      }
      assert.match(find('asset-publish-pack-member-list').textContent, /notes.*Approved/s);
      await act(async () => find('asset-publish-close').click());
    }
  } finally {
    if (find('asset-publish-close')) await act(async () => find('asset-publish-close').click());
    globalThis.setTimeout = originalTimeout;
    for (const timer of timers) clearTimeout(timer);
  }
});

test('refresh rechecks a temporarily unavailable description before enabling publish', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  let describes = 0;
  let records = 0;
  assetPublishApi.describe = async () => {
    describes++;
    return describes === 1
      ? { ...description, can_publish: false, errors: [{ code: 'HUB_REQUEST_FAILED' }] }
      : description;
  };
  assetPublishApi.records = async () => { records++; return { records: [] }; };
  try {
    await act(async () => openAssetPublish({ kind: 'skillpack', local_id: 'recover-description' }));
    await tick();
    assert.equal(find('asset-publish-prepare').disabled, true);
    await act(async () => find('asset-publish-refresh').click());
    await tick();
    assert.equal(describes, 2);
    assert.equal(records, 1);
    assert.equal(find('asset-publish-prepare').disabled, false);
  } finally {
    if (find('asset-publish-close')) await act(async () => find('asset-publish-close').click());
  }
});

test('unavailable SkillPack member download is explained in both languages without a guessed cause', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'test-token');
  assetPublishApi.describe = async () => description;
  assetPublishApi.prepare = async () => ({
    ...draft, can_submit: false,
    errors: [{ code: 'SKILLPACK_MEMBER_UNAVAILABLE' }],
    members: [{
      name: 'notes', version: '1.0.0', action: 'blocked',
      error: { code: 'SKILLPACK_MEMBER_UNAVAILABLE' },
    }],
  });
  try {
    for (const [language, message] of [
      ['en', /member version is not publicly downloadable.*review and listing status/i],
      ['zh', /成员版本当前不可公开下载，请检查审核\/上架状态后重新检查/],
    ]) {
      await act(async () => i18next.changeLanguage(language));
      await act(async () => openAssetPublish({ kind: 'skillpack', local_id: `unavailable-${language}` }));
      await tick();
      await act(async () => find('asset-publish-form').dispatchEvent(
        new dom.window.Event('submit', { bubbles: true, cancelable: true }),
      ));
      await tick();
      assert.match(document.body.textContent, message);
      assert.equal(find('asset-publish-commit').disabled, true);
      await act(async () => find('asset-publish-close').click());
    }
  } finally {
    if (find('asset-publish-close')) await act(async () => find('asset-publish-close').click());
    await act(async () => i18next.changeLanguage('en'));
  }
});

>>>>>>> origin/develop
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
