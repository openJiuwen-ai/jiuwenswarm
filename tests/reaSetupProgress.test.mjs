import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ACTIVE_INSTALL_JOB_STORAGE_KEY,
  aggregateCapabilityVisualState,
  cancelReaSetupJob,
  classifyCompletedReaSetupJob,
  classifyReaSetupRequestError,
  estimatePaceRemainingSeconds,
  estimateRemainingSeconds,
  formatProgressDuration,
  messageWithoutEta,
  mergeReaSetupScan,
  persistActiveJob,
  pollReaSetupJob,
  readPersistedActiveJob,
  resolveEnhancedVisualState,
  selectDisplayedRemainingSeconds,
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

test('classifies cancellation separately from installation failure', () => {
  assert.equal(
    classifyCompletedReaSetupJob({ status: 'cancelled', error: 'ignored cancellation detail' }),
    'cancelled',
  );
  assert.equal(
    classifyCompletedReaSetupJob({ status: 'failed', error: 'install failed' }),
    'error',
  );
  assert.equal(
    classifyCompletedReaSetupJob({ status: 'failed', error: '' }),
    null,
  );
  assert.equal(
    classifyCompletedReaSetupJob({ status: 'succeeded', error: null }),
    null,
  );
});

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

test('base detection keeps the cached enhanced capability snapshot', () => {
  const previous = {
    ready: true,
    base: [{ id: 'rea_mcp', ok: true }],
    enhanced: [{ id: 'ghidra', state: 'installed' }],
    enhanced_plan: [{ id: 'ghidra', state: 'planned' }],
    capability_cards: [{ id: 'ghidra', state: 'available' }],
    rea_inventory_diagnostics: { mcp_verified: true },
  };
  const next = {
    ready: false,
    base: [{ id: 'rea_mcp', ok: false }],
    enhanced: [],
    enhanced_plan: [],
    capability_cards: [],
    rea_inventory_diagnostics: {},
  };

  const merged = mergeReaSetupScan(previous, next, 'base');

  assert.equal(merged.ready, false);
  assert.deepEqual(merged.base, next.base);
  assert.deepEqual(merged.enhanced, previous.enhanced);
  assert.deepEqual(merged.enhanced_plan, previous.enhanced_plan);
  assert.deepEqual(merged.capability_cards, previous.capability_cards);
  assert.deepEqual(merged.rea_inventory_diagnostics, previous.rea_inventory_diagnostics);
});

test('full and enhanced detection replace the cached enhanced snapshot', () => {
  const previous = {
    ready: true,
    base: [],
    enhanced: [{ id: 'ghidra', state: 'not_installed' }],
    enhanced_plan: [],
    capability_cards: [],
    rea_inventory_diagnostics: {},
  };
  const next = {
    ready: true,
    base: [],
    enhanced: [{ id: 'ghidra', state: 'installed' }],
    enhanced_plan: [{ id: 'ghidra', state: 'ready' }],
    capability_cards: [{ id: 'ghidra', state: 'available' }],
    rea_inventory_diagnostics: { mcp_verified: true },
  };

  assert.deepEqual(mergeReaSetupScan(previous, next, 'all'), next);
  assert.deepEqual(mergeReaSetupScan(previous, next, 'enhanced'), next);
});

test('base detection without a previous snapshot does not import enhanced evidence', () => {
  const next = {
    ready: false,
    base: [{ id: 'rea_mcp', ok: false }],
    enhanced: [{ id: 'ghidra', state: 'discovered' }],
    enhanced_plan: [{ id: 'ghidra', state: 'planned' }],
    capability_cards: [{ id: 'ghidra', state: 'not_installed' }],
    rea_inventory_diagnostics: { mcp_verified: false },
  };

  const merged = mergeReaSetupScan(null, next, 'base');

  assert.deepEqual(merged.base, next.base);
  assert.deepEqual(merged.enhanced, []);
  assert.deepEqual(merged.enhanced_plan, []);
  assert.deepEqual(merged.capability_cards, []);
  assert.deepEqual(merged.rea_inventory_diagnostics, {});
});

test('enhanced state requires direct availability while preserving manifest states', () => {
  assert.equal(resolveEnhancedVisualState({ available: true, planState: 'planned' }), 'available');
  assert.equal(resolveEnhancedVisualState({ available: null, planState: 'needs_action' }), 'needsAction');
  assert.equal(resolveEnhancedVisualState({ available: null, planState: 'unsupported' }), 'unsupported');
  assert.equal(
    resolveEnhancedVisualState({ available: false, planState: 'planned', inventoryState: 'configured' }),
    'notInstalled',
  );
  assert.equal(
    resolveEnhancedVisualState({ available: null, planState: 'planned', inventoryState: 'discovered' }),
    'notInstalled',
  );
  assert.equal(
    resolveEnhancedVisualState({ available: null, planState: 'planned', inventoryState: 'partial' }),
    'partial',
  );
});

test('uses partial state when any capability member is available alongside failures', () => {
  assert.equal(
    aggregateCapabilityVisualState([
      'unsupported',
      'available',
      'failed',
      'available',
      'available',
      'available',
      'unsupported',
    ]),
    'partial',
  );
  assert.equal(
    aggregateCapabilityVisualState(['unsupported', 'failed', 'notInstalled']),
    'failed',
  );
  assert.equal(
    aggregateCapabilityVisualState(['unsupported', 'unsupported']),
    'unsupported',
  );
  assert.equal(
    aggregateCapabilityVisualState(['available', 'available']),
    'available',
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

test('classifies stale, transient, and fatal REA setup failures', () => {
  assert.equal(
    classifyReaSetupRequestError({ code: 'REA_SETUP_JOB_NOT_FOUND' }),
    'stale-job',
  );
  assert.equal(
    classifyReaSetupRequestError({ message: 'REA install job not found' }),
    'stale-job',
  );
  assert.equal(
    classifyReaSetupRequestError({ code: 'WS_DISCONNECTED' }),
    'retryable',
  );
  assert.equal(
    classifyReaSetupRequestError({ code: 'REQUEST_TIMEOUT' }),
    'retryable',
  );
  assert.equal(classifyReaSetupRequestError({ retriable: true }), 'retryable');
  assert.equal(classifyReaSetupRequestError({ code: 'BAD_REQUEST' }), 'fatal');
});

test('keeps polling through transient disconnects until the job settles', async () => {
  const updates = [];
  const job = createJob();
  const completed = createJob({ status: 'cancelled', percent: 100 });
  let loads = 0;
  const result = await pollReaSetupJob({
    initial: job,
    load: async () => {
      loads += 1;
      if (loads === 1) throw Object.assign(new Error('offline'), { code: 'WS_DISCONNECTED' });
      return completed;
    },
    onUpdate: (value) => updates.push(value.status),
    onStale: () => {
      throw new Error('stale handler must not run for a transient failure');
    },
    sleep: async () => {},
  });

  assert.equal(result?.status, 'cancelled');
  assert.equal(loads, 2);
  assert.deepEqual(updates, ['running', 'cancelled']);
});

test('clears a persisted job when the server no longer knows it', async () => {
  let staleCalls = 0;
  const result = await pollReaSetupJob({
    initial: createJob(),
    load: async () => {
      throw Object.assign(new Error('REA install job not found'), {
        code: 'REA_SETUP_JOB_NOT_FOUND',
      });
    },
    onUpdate: () => {},
    onStale: () => {
      staleCalls += 1;
    },
    sleep: async () => {},
  });

  assert.equal(result, null);
  assert.equal(staleCalls, 1);
});

test('retries cancellation after reconnect instead of leaving the UI cancelling', async () => {
  const cancelled = createJob({ status: 'cancelled', message: '增强能力安装已取消' });
  let attempts = 0;
  const result = await cancelReaSetupJob({
    jobId: cancelled.job_id,
    cancel: async () => {
      attempts += 1;
      if (attempts === 1) throw Object.assign(new Error('offline'), { code: 'WS_NOT_READY' });
      return cancelled;
    },
    onStale: () => {
      throw new Error('cancellation should recover when the same job reappears');
    },
    sleep: async () => {},
  });

  assert.equal(result?.status, 'cancelled');
  assert.equal(attempts, 2);
});

test('clears cancellation state when the server no longer knows the job', async () => {
  let staleCalls = 0;
  const result = await cancelReaSetupJob({
    jobId: 'missing-job',
    cancel: async () => {
      throw Object.assign(new Error('REA install job not found'), {
        code: 'REA_SETUP_JOB_NOT_FOUND',
      });
    },
    onStale: () => {
      staleCalls += 1;
    },
    sleep: async () => {},
  });

  assert.equal(result, null);
  assert.equal(staleCalls, 1);
});
