import assert from 'node:assert/strict';
import test from 'node:test';

import { designerRevisionAdoptionKey } from '../node_modules/.cache/designer-revision-key/designerRevisionKey.mjs';

const revision = (uri) => ({
  nodeId: 'leader',
  incoming: [{ uri }],
});

test('second regeneration of the same node yields a new key (issue #5326)', () => {
  const runId = 'run-lifelong';
  const round1 = designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), { version: 4 });
  const round2 = designerRevisionAdoptionKey(runId, revision('file:///b.mp4'), { version: 5 });
  assert.notEqual(round1, round2);
});

test('the same settled round always produces the same key', () => {
  const runId = 'run-lifelong';
  const key = designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), { version: 4 });
  assert.equal(key, designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), { version: 4 }));
});

test('a version bump alone defeats the lifelong run_id (issue #5326)', () => {
  const runId = 'run-lifelong';
  const round1 = designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), { version: 4 });
  const round2 = designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), { version: 5 });
  assert.notEqual(round1, round2);
});

test('candidate asset order and duplicates do not change the key', () => {
  const runId = 'run-lifelong';
  const a = designerRevisionAdoptionKey(
    runId,
    { nodeId: 'n1', incoming: [{ uri: 'u1' }, { uri: 'u2' }] },
    { version: 2 },
  );
  const b = designerRevisionAdoptionKey(
    runId,
    { nodeId: 'n1', incoming: [{ uri: 'u2' }, null, { uri: 'u1' }, { uri: 'u1' }] },
    { version: 2 },
  );
  assert.equal(a, b);
});

test('missing version / assets still key stably', () => {
  const runId = 'run-lifelong';
  assert.equal(
    designerRevisionAdoptionKey(runId, { nodeId: 'n1', incoming: [] }, null),
    designerRevisionAdoptionKey(runId, { nodeId: 'n1', incoming: [] }, undefined),
  );
  assert.notEqual(
    designerRevisionAdoptionKey(runId, { nodeId: 'n1', incoming: [] }, null),
    designerRevisionAdoptionKey(runId, revision('file:///a.mp4'), null),
  );
});
