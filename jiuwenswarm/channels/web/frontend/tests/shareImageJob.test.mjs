import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findShareImageJobForSession,
  forgetPendingShareImageJob,
  readPendingShareImageJobId,
  rememberPendingShareImageJob,
} from '../node_modules/.cache/share-image/shareImageJob.js';

const SESSION_ID = 'session-a';
const JOB_ID = '0123456789abcdef0123456789abcdef';

function createStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}

function job(overrides = {}) {
  return {
    job_id: JOB_ID,
    session_id: SESSION_ID,
    filename: 'share.png',
    state: 'running',
    phase: 'rendering',
    error: null,
    ...overrides,
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('persists one pending export per session and only clears the matching job', () => {
  const storage = createStorage();
  rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);
  rememberPendingShareImageJob(storage, 'session-b', 'fedcba9876543210fedcba9876543210');

  assert.equal(readPendingShareImageJobId(storage, SESSION_ID), JOB_ID);
  forgetPendingShareImageJob(storage, SESSION_ID, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(readPendingShareImageJobId(storage, SESSION_ID), JOB_ID);
  forgetPendingShareImageJob(storage, SESSION_ID, JOB_ID);
  assert.equal(readPendingShareImageJobId(storage, SESSION_ID), null);
  assert.equal(
    readPendingShareImageJobId(storage, 'session-b'),
    'fedcba9876543210fedcba9876543210',
  );
});

test('resumes a completed export by its persisted job id after page reload', async () => {
  const storage = createStorage();
  rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);
  const requests = [];

  const status = await findShareImageJobForSession(SESSION_ID, storage, async (url) => {
    requests.push(String(url));
    return jsonResponse(job({ state: 'completed', phase: 'completed' }));
  });

  assert.equal(status?.state, 'completed');
  assert.deepEqual(requests, [`/share-api/jobs/${JOB_ID}`]);
});

test('discovers and persists a running server job when the tab has no pending id', async () => {
  const storage = createStorage();
  const requests = [];

  const status = await findShareImageJobForSession(SESSION_ID, storage, async (url) => {
    requests.push(String(url));
    return jsonResponse(job());
  });

  assert.equal(status?.job_id, JOB_ID);
  assert.equal(readPendingShareImageJobId(storage, SESSION_ID), JOB_ID);
  assert.deepEqual(requests, ['/share-api/jobs?session_id=session-a']);
});

test('drops an expired pending id before querying the active session job', async () => {
  const storage = createStorage();
  rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);
  const replacementJobId = 'fedcba9876543210fedcba9876543210';
  const requests = [];

  const status = await findShareImageJobForSession(SESSION_ID, storage, async (url) => {
    requests.push(String(url));
    if (requests.length === 1) return jsonResponse({ error: 'job_not_found' }, 404);
    return jsonResponse(job({ job_id: replacementJobId }));
  });

  assert.equal(status?.job_id, replacementJobId);
  assert.equal(readPendingShareImageJobId(storage, SESSION_ID), replacementJobId);
  assert.deepEqual(requests, [
    `/share-api/jobs/${JOB_ID}`,
    '/share-api/jobs?session_id=session-a',
  ]);
});

test('rejects a persisted job belonging to another session', async () => {
  const storage = createStorage();
  rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);

  await assert.rejects(
    findShareImageJobForSession(
      SESSION_ID,
      storage,
      async () => jsonResponse(job({ session_id: 'session-b' })),
    ),
    /share_export_job_session_mismatch/,
  );
});
