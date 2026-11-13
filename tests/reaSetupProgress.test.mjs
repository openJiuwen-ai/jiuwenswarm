import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ACTIVE_INSTALL_JOB_STORAGE_KEY,
  estimatePaceRemainingSeconds,
  estimateRemainingSeconds,
  formatProgressDuration,
  messageWithoutEta,
  persistActiveJob,
  readPersistedActiveJob,
  selectDisplayedRemainingSeconds,
  shouldDeferEnhancedDetection,
  stabilizeRemainingSeconds,
} from '../jiuwenswarm/channels/web/frontend/node_modules/.cache/rea-setup-progress/reaSetupProgress.mjs';

function createStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}

function createJob(overrides = {}) {
  return {
    job_id: 'job-1',
    kind: 'enhanced',
    status: 'running',
    stage: 'downloading',
    percent: 40,
    message: 'downloading',
    started_at: 100,
    updated_at: 200,
    detail: {},
    ...overrides,
  };
}

test('uses aggregate download bytes for a coherent remaining estimate', () => {
  const job = createJob({
    detail: {
      bytes_per_second: 1000,
      cumulative_downloaded_bytes: 500000,
      cumulative_total_bytes: 2000000,
    },
  });

  assert.equal(estimateRemainingSeconds(job), 1500);
});

test('uses install pace instead of a short download-only ETA', () => {
  const job = createJob({
    percent: 40,
    estimated_seconds_min: 600,
    estimated_seconds_max: 900,
    detail: {
      bytes_per_second: 1000,
      cumulative_downloaded_bytes: 999000,
      cumulative_total_bytes: 1000000,
      eta_seconds: 2,
    },
  });

  assert.equal(estimateRemainingSeconds(job), 1);
  assert.equal(estimatePaceRemainingSeconds(job, 40, 60), 90);
});

test('never reports zero while an install is running', () => {
  const job = createJob({
    detail: {
      eta_seconds: 0,
      bytes_per_second: 0,
    },
  });

  assert.equal(estimateRemainingSeconds(job, 300), 300);
  assert.ok(stabilizeRemainingSeconds(job, 0, 300) >= 1);
  assert.deepEqual(formatProgressDuration(0), { value: 1, unit: 'seconds' });
  assert.equal(stabilizeRemainingSeconds(createJob({ status: 'succeeded' }), 0, 300), null);
});

test('defers enhanced capability status until base detection is available', () => {
  assert.equal(shouldDeferEnhancedDetection(createJob({ kind: 'base' }), 0), true);
  assert.equal(shouldDeferEnhancedDetection(createJob({ kind: 'base' }), 10), false);
  assert.equal(shouldDeferEnhancedDetection(createJob({ kind: 'enhanced' }), 0), false);
  assert.equal(
    shouldDeferEnhancedDetection(createJob({ kind: 'base', status: 'succeeded' }), 0),
    false,
  );
});

test('throttles the displayed remaining time unless the estimate changes materially', () => {
  const first = selectDisplayedRemainingSeconds('job-1', 360, null, 1000);
  const throttled = selectDisplayedRemainingSeconds('job-1', 355, first, 12000);
  const material = selectDisplayedRemainingSeconds('job-1', 240, first, 12000);
  const sameMinute = selectDisplayedRemainingSeconds('job-1', 350, first, 32000);
  const refreshed = selectDisplayedRemainingSeconds('job-1', 299, first, 32000);

  assert.equal(throttled.seconds, 360);
  assert.equal(material.seconds, 240);
  assert.equal(sameMinute.seconds, 360);
  assert.equal(refreshed.seconds, 299);
});

test('formats the single displayed estimate consistently', () => {
  assert.deepEqual(formatProgressDuration(360), { value: 6, unit: 'minutes' });
  assert.equal(messageWithoutEta('PCAP：已下载 1 MiB / 2 MiB，20 KiB/s，预计剩余 60 秒'), 'PCAP：已下载 1 MiB / 2 MiB，20 KiB/s');
});

test('persists only an active install job for navigation recovery', () => {
  const storage = createStorage();
  const job = createJob();

  persistActiveJob(storage, job);
  assert.equal(storage.getItem(ACTIVE_INSTALL_JOB_STORAGE_KEY)?.includes(job.job_id), true);
  assert.equal(readPersistedActiveJob(storage)?.job_id, job.job_id);

  persistActiveJob(storage, { ...job, status: 'succeeded' });
  assert.equal(storage.getItem(ACTIVE_INSTALL_JOB_STORAGE_KEY), null);
});
