import assert from 'node:assert/strict';
import test from 'node:test';

import { planUnsentImageDiscard } from '../node_modules/.cache/unsent-image-discard/components/ChatPanel/unsentImageDiscard.js';

test('discards a ready image copy and leaves documents alone', () => {
  const plan = planUnsentImageDiscard(
    [
      { id: 'img', kind: 'image', status: 'ready', persistedPath: 'C:/sessions/a/uploads/sample.png' },
      { id: 'doc', kind: 'document', status: 'ready', persistedPath: 'C:/Users/me/notes.md' },
    ],
    [],
  );
  assert.deepEqual(plan.pendingIds, []);
  assert.deepEqual(plan.paths, ['C:/sessions/a/uploads/sample.png']);
});

test('waits for an in-flight upload instead of deleting a path it does not have yet', () => {
  const plan = planUnsentImageDiscard(
    [{ id: 'img', kind: 'image', status: 'uploading' }],
    [],
  );
  assert.deepEqual(plan.pendingIds, ['img']);
  assert.deepEqual(plan.paths, []);
});

test('keeps a path that another remaining image still uses', () => {
  const shared = 'C:/sessions/a/uploads/sample.png';
  const plan = planUnsentImageDiscard(
    [{ id: 'gone', kind: 'image', status: 'ready', persistedPath: shared }],
    [{ id: 'kept', kind: 'image', status: 'ready', persistedPath: shared }],
  );
  assert.deepEqual(plan.paths, []);
});

test('dedupes the same removed path', () => {
  const path = 'C:/sessions/a/uploads/sample.png';
  const plan = planUnsentImageDiscard(
    [
      { id: 'a', kind: 'image', status: 'ready', persistedPath: path },
      { id: 'b', kind: 'image', status: 'ready', persistedPath: path },
    ],
    [],
  );
  assert.deepEqual(plan.paths, [path]);
});
