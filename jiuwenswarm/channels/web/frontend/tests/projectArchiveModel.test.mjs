import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveProjectArchiveSessionCount,
} from '../node_modules/.cache/project-archive-model/multi-session/sidebar/projectArchiveModel.js';

const project = { project_id: 'project-a', session_count: 2 };

test('project archive count includes pinned ordinary sessions but excludes cron sessions', () => {
  const count = resolveProjectArchiveSessionCount(project, { 'project-a': 2 }, [
    { session_id: 'pinned-chat', project_id: 'project-a', cron_id: '' },
    { session_id: 'cron_job', project_id: 'project-a', cron_id: 'job-1' },
    { session_id: 'heartbeat_job', project_id: 'project-a', cron_id: '' },
    { session_id: 'other-project', project_id: 'project-b', cron_id: '' },
  ]);

  assert.equal(count, 3);
});

test('project archive count falls back to project statistics', () => {
  assert.equal(resolveProjectArchiveSessionCount(project, {}, []), 2);
});
