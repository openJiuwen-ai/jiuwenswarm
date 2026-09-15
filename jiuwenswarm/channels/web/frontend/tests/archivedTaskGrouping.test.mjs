import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeArchivedListResponse } from '../node_modules/.cache/archived-task-grouping/features/workspace/archivedTaskGrouping.js';

test('normalizes project.archived.list projects into the page item collection', () => {
  const project = { project_id: 'project-1', name: 'Archived project' };
  const page = normalizeArchivedListResponse({
    projects: [project, null],
    total: 3,
    limit: 20,
    offset: 0,
    has_more: true,
  }, 'projects');

  assert.deepEqual(page, {
    items: [project],
    total: 3,
    limit: 20,
    offset: 0,
    has_more: true,
  });
});

test('normalizes session.archived.list sessions into the page item collection', () => {
  const session = { session_id: 'session-1', title: 'Archived session' };
  const page = normalizeArchivedListResponse({
    sessions: [session],
    total: 1,
    limit: 20,
    offset: 0,
    has_more: false,
  }, 'sessions');

  assert.deepEqual(page.items, [session]);
  assert.equal(page.total, 1);
  assert.equal(page.has_more, false);
});

test('rejects a list response that does not contain the expected resource field', () => {
  assert.throws(
    () => normalizeArchivedListResponse({ items: [] }, 'projects'),
    /missing the projects array/,
  );
});
