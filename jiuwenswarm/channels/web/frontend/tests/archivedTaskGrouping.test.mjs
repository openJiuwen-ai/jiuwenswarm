import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildArchivedTaskGroups,
  normalizeArchivedListResponse,
  UNASSIGNED_GROUP_KEY,
} from '../node_modules/.cache/archived-task-grouping/features/workspace/archivedTaskGrouping.js';

test('normalizes session.archived.list sessions into the page item collection', () => {
  const session = { session_id: 'session-1', title: 'Archived session' };
  const page = normalizeArchivedListResponse({
    sessions: [session, null],
    total: 3,
    limit: 20,
    offset: 0,
    has_more: true,
  }, 'sessions');

  assert.deepEqual(page, {
    items: [session],
    total: 3,
    limit: 20,
    offset: 0,
    has_more: true,
  });
});

test('rejects a list response that does not contain the sessions field', () => {
  assert.throws(
    () => normalizeArchivedListResponse({ items: [] }, 'sessions'),
    /missing the sessions array/,
  );
});

test('groups archived sessions by project and sorts groups by latest archived_at', () => {
  const groups = buildArchivedTaskGroups([
    { session_id: 's1', project_id: 'p1', project_name: 'Alpha', archived_at: 100 },
    { session_id: 's2', project_id: 'p2', project_name: 'Beta', archived_at: 300 },
    { session_id: 's3', project_id: 'p1', project_name: 'Alpha', archived_at: 200 },
  ]);

  assert.deepEqual(groups.map((group) => [group.projectId, group.latestArchivedAt]), [
    ['p2', 300],
    ['p1', 200],
  ]);
  assert.deepEqual(groups[1].sessions.map((session) => session.session_id), ['s1', 's3']);
});

test('groups sessions without a project name under the unassigned key', () => {
  const groups = buildArchivedTaskGroups([
    { session_id: 's1', project_id: 'p1', project_name: null, archived_at: 100 },
    { session_id: 's2', project_id: 'p2', project_name: '  ', archived_at: 90 },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, UNASSIGNED_GROUP_KEY);
  assert.equal(groups[0].projectName, null);
  assert.deepEqual(groups[0].sessions.map((session) => session.session_id), ['s1', 's2']);
});

test('marks a group whose project was removed', () => {
  const groups = buildArchivedTaskGroups([
    { session_id: 's1', project_id: 'p1', project_name: 'Alpha', archived_at: 100, project_hidden: true },
    { session_id: 's2', project_id: 'p2', project_name: 'Beta', archived_at: 300 },
  ]);

  const beta = groups.find((group) => group.projectId === 'p2');
  const alpha = groups.find((group) => group.projectId === 'p1');
  assert.equal(alpha.projectHidden, true);
  assert.equal(beta.projectHidden, false);
});

test('keeps the removed-project mark when only part of a page carries the flag', () => {
  const groups = buildArchivedTaskGroups([
    { session_id: 's1', project_id: 'p1', project_name: 'Alpha', archived_at: 100 },
    { session_id: 's2', project_id: 'p1', project_name: 'Alpha', archived_at: 200, project_hidden: true },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].projectHidden, true);
});

test('unassigned groups are never marked as removed projects', () => {
  const groups = buildArchivedTaskGroups([
    { session_id: 's1', project_id: '', project_name: null, archived_at: 100 },
  ]);

  assert.equal(groups[0].key, UNASSIGNED_GROUP_KEY);
  assert.equal(groups[0].projectHidden, false);
});
