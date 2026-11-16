import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveCronEventSessionId } from '../node_modules/.cache/cron-event-session-route/services/cronEventSessionRoute.js';

const noLookup = () => undefined;

test('direct payload.session_id wins', () => {
  assert.equal(
    resolveCronEventSessionId(
      { session_id: 'sess_display', cron: { job_id: 'job-1', exec_session_id: 'exec-1' } },
      noLookup
    ),
    'sess_display'
  );
});

test('empty payload.session_id falls back to cron.exec_session_id', () => {
  assert.equal(
    resolveCronEventSessionId(
      { session_id: null, cron: { job_id: 'job-1', exec_session_id: 'exec-1' } },
      noLookup
    ),
    'exec-1'
  );
});

test('blank payload.session_id is treated as absent', () => {
  assert.equal(
    resolveCronEventSessionId(
      { session_id: '   ', cron: { exec_session_id: ' exec-1 ' } },
      noLookup
    ),
    'exec-1'
  );
});

test('falls back to lastRunSessionId when exec_session_id is absent', () => {
  const lookups = [];
  assert.equal(
    resolveCronEventSessionId(
      { cron: { job_id: 'job-1' } },
      (jobId) => {
        lookups.push(jobId);
        return 'desktop_last_run';
      }
    ),
    'desktop_last_run'
  );
  assert.deepEqual(lookups, ['job-1']);
});

test('returns null without cron and without session_id', () => {
  assert.equal(resolveCronEventSessionId({}, noLookup), null);
});

test('returns null when lastRunSessionId is missing', () => {
  assert.equal(
    resolveCronEventSessionId({ cron: { job_id: 'job-1' } }, () => undefined),
    null
  );
});

test('does not query lastRunSessionId when job_id is absent', () => {
  let called = false;
  assert.equal(
    resolveCronEventSessionId(
      { cron: { exec_session_id: '' } },
      () => {
        called = true;
        return 'x';
      }
    ),
    null
  );
  assert.equal(called, false);
});

test('ignores non-object cron payload', () => {
  assert.equal(resolveCronEventSessionId({ cron: 'broken' }, noLookup), null);
});
