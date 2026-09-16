import test from 'node:test';
import assert from 'node:assert/strict';
import {
  publishOutcome,
  validateMetadata,
  createCommitAttempt,
} from '../node_modules/.cache/asset-publish/assetPublishState.js';
test('completed upload does not imply published', () => {
  assert.equal(
    publishOutcome({ execution_status: 'completed', result: { publish_result: 'pending_moderation' } }),
    'pending_moderation',
  );
  assert.equal(publishOutcome({ execution_status: 'completed', result: { publish_result: 'published' } }), 'published');
  assert.equal(
    publishOutcome({ execution_status: 'completed', result: { publish_result: 'publish_success' } }),
    'published',
  );
  assert.equal(
    publishOutcome({ execution_status: 'completed', result: { publish_result: 'publish_failed' } }),
    'failed',
  );
  assert.equal(
    publishOutcome({ execution_status: 'completed', result: { publish_result: 'future_value' } }),
    'unknown',
  );
});
test('accept semver and seven-character revision, reject unsafe names', () => {
  const metadata = {
    asset_name: 'demo',
    display_name: 'Demo',
    version: 'abcdef0',
    description: 'Demo',
    visibility: 'public',
    tags: [],
    version_desc: '',
  };
  assert.deepEqual(validateMetadata(metadata), []);
  assert.ok(validateMetadata({ ...metadata, version: 'v1.0.0' }).includes('version'));
  assert.ok(validateMetadata({ ...metadata, asset_name: '../demo' }).includes('asset_name'));
});
test('concurrent clicks and a timeout reuse the same commit request', async () => {
  let calls = 0;
  const ids = [];
  let resolve;
  const send = async (id) => {
    ids.push(id);
    calls++;
    if (calls === 1) await new Promise((r) => (resolve = r));
    throw Error('timeout');
  };
  const attempt = createCommitAttempt('fixed-request', send);
  const first = attempt.run().catch(() => {});
  const second = attempt.run().catch(() => {});
  assert.equal(calls, 1);
  resolve();
  await Promise.all([first, second]);
  await attempt.run().catch(() => {});
  assert.deepEqual(ids, ['fixed-request', 'fixed-request']);
});

test('cache metadata stays alongside cards and refresh polling cancels stale queries', async (context) => {
  const { withCatalogCache, scheduleCatalogRefresh } =
    await import('../node_modules/.cache/asset-publish/catalogCache.js');
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const cards = withCatalogCache([{ id: 'retained' }], { state: 'stale', refreshing: true, complete: false });
  let calls = 0;
  scheduleCatalogRefresh(
    'query',
    cards.cache,
    () => calls++,
    () => true,
  );
  assert.equal(cards[0].id, 'retained');
  context.mock.timers.tick(4000);
  assert.equal(calls, 1);
  scheduleCatalogRefresh(
    'query',
    cards.cache,
    () => calls++,
    () => false,
  );
  context.mock.timers.tick(4000);
  assert.equal(calls, 1);
  scheduleCatalogRefresh(
    'query',
    { state: 'fresh', refreshing: false },
    () => calls++,
    () => true,
  );
  context.mock.timers.tick(4000);
  assert.equal(calls, 1);
});
test('timestamps support backend Unix seconds and ISO dates', async () => {
  const { publishTimestamp } = await import('../node_modules/.cache/asset-publish/assetPublishState.js');
  assert.equal(publishTimestamp(1700000000), 1700000000000);
  assert.equal(publishTimestamp('2023-11-14T22:13:20Z'), 1700000000000);
});
