import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getProjectMenuItems,
} from '../node_modules/.cache/sidebar-model/multi-session/sidebar/sidebarModel.js';

const t = (key) => key;
const findItem = (items, action) => items.find((item) => item.action === action);

test('ordinary project menu shows pin, rename, delete and archive-sessions', () => {
  const items = getProjectMenuItems(false, t);
  assert.deepEqual(items.map((item) => item.action), ['pin', 'rename', 'delete', 'archive-sessions']);
  assert.equal(findItem(items, 'delete').danger, true);
  assert.equal(findItem(items, 'archive-sessions').disabled, undefined);
});

test('default project menu only shows archive-sessions', () => {
  const items = getProjectMenuItems(false, t, { isDefault: true });
  assert.deepEqual(items.map((item) => item.action), ['archive-sessions']);
});

test('archive-sessions is disabled when archiveSessionsDisabled is true', () => {
  const items = getProjectMenuItems(false, t, { archiveSessionsDisabled: true });
  assert.equal(findItem(items, 'archive-sessions').disabled, true);

  const defaultItems = getProjectMenuItems(false, t, { isDefault: true, archiveSessionsDisabled: true });
  assert.equal(findItem(defaultItems, 'archive-sessions').disabled, true);
});

test('archive-sessions stays enabled when archiveSessionsDisabled is false or omitted', () => {
  const explicit = getProjectMenuItems(false, t, { archiveSessionsDisabled: false });
  assert.notEqual(findItem(explicit, 'archive-sessions').disabled, true);

  const omitted = getProjectMenuItems(false, t);
  assert.notEqual(findItem(omitted, 'archive-sessions').disabled, true);
});

test('pinned state switches the pin label without affecting other items', () => {
  const pinned = getProjectMenuItems(true, t);
  assert.equal(findItem(pinned, 'pin').pinned, true);
  assert.equal(findItem(pinned, 'pin').label, 'multiSession.project.unpinProject');

  const unpinned = getProjectMenuItems(false, t);
  assert.equal(findItem(unpinned, 'pin').pinned, false);
  assert.equal(findItem(unpinned, 'pin').label, 'multiSession.project.pinProject');
});
