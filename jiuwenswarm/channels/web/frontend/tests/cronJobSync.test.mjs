import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildCronJobFingerprintMap,
  detectCronJobRunUpdates,
  fingerprintFromCronJob,
} from '../node_modules/.cache/cron-job-sync/cronJobSync.mjs';

test('fingerprintFromCronJob normalizes last_session_id (ignores last_run_at)', () => {
  assert.deepEqual(
    fingerprintFromCronJob({
      id: 'job-1',
      last_run_at: '1735689600',
      last_session_id: '  cron_abc_job-1  ',
    }),
    {
      jobId: 'job-1',
      lastSessionId: 'cron_abc_job-1',
    }
  );
});

test('detectCronJobRunUpdates seeds baseline without unread on first sight', () => {
  const jobs = [{ id: 'job-1', last_run_at: 100, last_session_id: 'cron_a' }];
  const result = detectCronJobRunUpdates({}, jobs);
  assert.deepEqual(result.updatedJobIds, []);
  assert.deepEqual(result.nextFingerprints, buildCronJobFingerprintMap(jobs));
});

test('detectCronJobRunUpdates marks last_session_id changes', () => {
  const previous = buildCronJobFingerprintMap([
    { id: 'job-1', last_run_at: 100, last_session_id: 'cron_a' },
  ]);
  const result = detectCronJobRunUpdates(previous, [
    { id: 'job-1', last_run_at: 100, last_session_id: 'cron_b' },
  ]);
  assert.deepEqual(result.updatedJobIds, ['job-1']);
});

test('detectCronJobRunUpdates ignores last_run_at-only changes', () => {
  // claim 认领那一刻 last_run_at 即写库，此时结果还未产生，不应亮蓝点
  const previous = buildCronJobFingerprintMap([
    { id: 'job-1', last_run_at: 100, last_session_id: 'cron_a' },
  ]);
  const result = detectCronJobRunUpdates(previous, [
    { id: 'job-1', last_run_at: 200, last_session_id: 'cron_a' },
  ]);
  assert.deepEqual(result.updatedJobIds, []);
});

test('detectCronJobRunUpdates ignores unchanged snapshots', () => {
  const jobs = [{ id: 'job-1', last_run_at: 100, last_session_id: 'cron_a' }];
  const previous = buildCronJobFingerprintMap(jobs);
  const result = detectCronJobRunUpdates(previous, jobs);
  assert.deepEqual(result.updatedJobIds, []);
});
