import assert from 'node:assert/strict';
import test from 'node:test';
import { cancelShareImageJobForClosedTab, openShareImageExportTab } from '../node_modules/.cache/share-image/shareImageBrowserExport.js';

import {
  findShareImageJobForSession,
  forgetPendingShareImageJob,
  readPendingShareImageJobId,
  rememberPendingShareImageJob,
  waitForShareImageJobDownload,
  waitForShareImageJobTransfer,
  fetchShareImageJobStatus,
  startShareImageJobDownload,
  resumeBrowserShareImageJob,
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

test('a legacy completed record recovers its capability for the same job on retry', async () => {
  const storage = createStorage();
  storage.setItem(`jiuwenswarm:pending-share-image-job:${SESSION_ID}`, JOB_ID);
  const published = job({ renderer: 'browser', state: 'completed', phase: 'download_failed' });
  const resumed = await resumeBrowserShareImageJob(published, SESSION_ID, storage, async (url, init) => {
    assert.equal(url, '/share-api/jobs');
    assert.deepEqual(JSON.parse(init.body), { session_id: SESSION_ID, job_id: JOB_ID });
    return jsonResponse({ ...published, client_token: 'a'.repeat(64), reused: true });
  });
  assert.equal(resumed.job_id, JOB_ID);
  const restored = await findShareImageJobForSession(SESSION_ID, storage, async () => jsonResponse(published));
  assert.equal(restored.client_token, 'a'.repeat(64));
});

test('capability recovery rejects replacement jobs and never crosses into the native path', async () => {
  const storage = createStorage();
  const request = async () => jsonResponse(job({ renderer: 'browser', client_token: 'a'.repeat(64), job_id: 'b'.repeat(32) }));
  await assert.rejects(resumeBrowserShareImageJob(job({ renderer: 'browser' }), SESSION_ID, storage, request), /share_export_job_id_mismatch/);
  const native = job({ renderer: 'server' });
  assert.equal(await resumeBrowserShareImageJob(native, SESSION_ID, storage, async () => { throw new Error('unexpected'); }), native);
});

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

test('an active transfer has no total deadline and survives a failed status request', async () => {
  const published = job({ state: 'completed', phase: 'download_started' });
  let requests = 0;
  const phases = [];
  const result = await waitForShareImageJobTransfer(published, () => true,
    status => phases.push(status.phase), async () => {
      requests++;
      if (requests === 1) throw new TypeError('Network disconnected');
      return jsonResponse({ ...published, phase: requests === 3 ? 'download_sent' : 'download_started' });
    }, async () => {});
  assert.equal(result.phase, 'download_sent');
  assert.deepEqual(phases, ['download_started', 'download_started', 'download_started', 'download_sent']);
});

test('an interrupted file stream releases its monitor without deleting the generated image', async () => {
  const published = job({ state: 'completed', phase: 'download_started' });
  await assert.rejects(waitForShareImageJobTransfer(published, () => true, () => {},
    async () => jsonResponse({ ...published, phase: 'download_failed' }), async () => {}), /share_export_download_interrupted/);
  assert.equal(published.state, 'completed');
});

test('a stalled individual status request is aborted without a generation deadline', async () => {
  let signal;
  await assert.rejects(fetchShareImageJobStatus(JOB_ID, (_url, options) => {
    signal = options.signal;
    return new Promise(() => {});
  }, 10), /share_export_status_timeout/);
  assert.equal(signal.aborted, true);
});

test('renderer and restored chat share one claimed automatic download', async () => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  let claims = 0;
  let downloads = 0;
  globalThis.fetch = async (_url, init) => {
    assert.equal(JSON.parse(init.body).claim_download, true);
    return jsonResponse(job({ state: 'completed', download_claimed: ++claims === 1, download_attempt_id: 'b'.repeat(32) }));
  };
  globalThis.document = {
    createElement: () => ({ style: {}, click: () => { downloads++; }, remove: () => {} }),
    body: { appendChild: () => {} },
  };
  try {
    const published = job({ state: 'completed', client_token: 'a'.repeat(64) });
    await Promise.all([startShareImageJobDownload(published), startShareImageJobDownload(published)]);
    assert.equal(claims, 2);
    assert.equal(downloads, 1);
  } finally { globalThis.fetch = previousFetch; globalThis.document = previousDocument; }
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

test('an empty active-job lookup returns null, including after a pending job expires', async () => {
  for (const expired of [false, true]) {
    const storage = createStorage();
    if (expired) rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);
    const status = await findShareImageJobForSession(SESSION_ID, storage, async url => {
      if (String(url) === `/share-api/jobs/${JOB_ID}`) return jsonResponse({ error: 'job_not_found' }, 404);
      return jsonResponse(null);
    });
    assert.equal(status, null);
    assert.equal(readPendingShareImageJobId(storage, SESSION_ID), null);
  }
});

test('active-job lookup rejects HTTP errors and malformed JSON instead of treating them as empty', async () => {
  for (const response of [jsonResponse({ error: 'not_found' }, 404), jsonResponse(null, 500), new Response('invalid JSON'), jsonResponse({})]) {
    await assert.rejects(findShareImageJobForSession(SESSION_ID, createStorage(), async () => response));
  }
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

test('opens the selected browser job and keeps the write token out of the HTTP URL', () => {
  let opened;
  const tab = { closed: false };
  const previousWindow = globalThis.window;
  globalThis.window = { open: (...args) => { opened = args; return tab; } };
  try {
    assert.equal(openShareImageExportTab(job({ client_token: 'private-write-token' }), 'zh'), tab);
    assert.equal(opened[1], '_blank');
    const url = new URL(opened[0], 'http://localhost');
    assert.equal(url.searchParams.get('job_id'), JOB_ID);
    assert.equal(url.searchParams.has('client_token'), false);
    assert.equal(new URLSearchParams(url.hash.slice(1)).get('client_token'), 'private-write-token');
  } finally { globalThis.window = previousWindow; }
});

test('a blocked tab can be opened again for the same job without mutating server state', () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const requests = [];
  const tab = { closed: false };
  globalThis.fetch = () => { throw new Error('Unexpected job mutation'); };
  globalThis.window = { open: url => { requests.push(url); return requests.length === 1 ? null : tab; } };
  try {
    const status = job({ client_token: 'private-write-token' });
    assert.equal(openShareImageExportTab(status, 'en'), null);
    assert.equal(openShareImageExportTab(status, 'en'), tab);
    assert.equal(requests[0], requests[1]);
  } finally { globalThis.window = previousWindow; globalThis.fetch = previousFetch; }
});

test('a missing write capability cannot open a renderer', () => {
  assert.throws(() => openShareImageExportTab(job(), 'en'), /client_token_missing/);
});

test('closing an unopened renderer ends its queued job without cancelling another session', async () => {
  const previousFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, body: JSON.parse(init.body) });
    return jsonResponse(job({ renderer: 'browser', state: 'failed', phase: 'failed', error: 'share_export_tab_closed' }));
  };
  try {
    const other = job({ job_id: 'b'.repeat(32), session_id: 'session-b', renderer: 'browser' });
    const status = job({ renderer: 'browser', state: 'queued', client_token: 'a'.repeat(64) });
    const ended = await cancelShareImageJobForClosedTab(status, { closed: true });
    assert.equal(ended.state, 'failed');
    assert.equal(ended.error, 'share_export_tab_closed');
    assert.equal(await cancelShareImageJobForClosedTab(other, { closed: false }), other);
    assert.deepEqual(requests, [{
      url: `/share-api/jobs/${JOB_ID}/browser`,
      body: { client_token: 'a'.repeat(64), state: 'failed', phase: 'failed', error: 'share_export_tab_closed', expected_state: 'queued' },
    }]);
  } finally { globalThis.fetch = previousFetch; }
});

test('a closed tab never cancels claimed jobs, published files, existing failures or server renderers', async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('Unexpected job mutation'); };
  try {
    for (const status of [
      job({ renderer: 'server' }),
      job({ renderer: 'checking', state: 'queued' }),
      job({ renderer: 'browser', state: 'running' }),
      job({ renderer: 'browser', state: 'completed', phase: 'completed' }),
      job({ renderer: 'browser', state: 'completed', phase: 'download_started' }),
      job({ renderer: 'browser', state: 'completed', phase: 'download_sent' }),
      job({ renderer: 'browser', state: 'failed', error: 'share_export_render_failed' }),
    ]) {
      assert.equal(await cancelShareImageJobForClosedTab(status, { closed: true }), status);
    }
    const queued = job({ renderer: 'browser', state: 'queued' });
    assert.equal(await cancelShareImageJobForClosedTab(queued, undefined), queued);
    assert.equal(await cancelShareImageJobForClosedTab(queued, { closed: false }), queued);
    await assert.rejects(cancelShareImageJobForClosedTab(queued, { closed: true }), /client_token_missing/);
  } finally { globalThis.fetch = previousFetch; }
});

test('closing a tab respects a job claimed or published before cancellation reaches the server', async () => {
  const previousFetch = globalThis.fetch;
  try {
    for (const state of ['running', 'completed']) {
      const current = job({ renderer: 'browser', state });
      globalThis.fetch = async () => jsonResponse(current);
      const ended = await cancelShareImageJobForClosedTab(job({ renderer: 'browser', state: 'queued', client_token: 'a'.repeat(64) }), { closed: true });
      assert.deepEqual(ended, current);
    }
  } finally { globalThis.fetch = previousFetch; }
});

test('a failed cancellation request cannot pretend the server job has ended', async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError('Network disconnected'); };
  try {
    await assert.rejects(cancelShareImageJobForClosedTab(job({ renderer: 'browser', state: 'queued', client_token: 'a'.repeat(64) }), { closed: true }), TypeError);
  } finally { globalThis.fetch = previousFetch; }
});

test('publishing the file is not a download acknowledgement', async () => {
  const published = job({ state: 'completed', phase: 'completed', renderer: 'browser' });
  let polls = 0;
  const result = await waitForShareImageJobDownload(published, () => true, async url => {
    assert.equal(url, `/share-api/jobs/${JOB_ID}`);
    polls++;
    return jsonResponse(polls === 1 ? published : { ...published, phase: 'download_started' });
  }, async () => {});
  assert.equal(polls, 2);
  assert.equal(result.phase, 'download_started');
});

test('an acknowledged browser download requires no second download or poll', async () => {
  const started = job({ state: 'completed', phase: 'download_started' });
  assert.equal(await waitForShareImageJobDownload(started, () => true,
    () => { throw new Error('Unexpected request'); }, async () => {}), started);
});

test('a replaced monitor stops waiting without requesting another session job', async () => {
  let current = true;
  const result = await waitForShareImageJobDownload(job({ state: 'completed' }), () => current,
    () => { throw new Error('Unexpected request'); }, async () => { current = false; });
  assert.equal(result, null);
});

test('download acknowledgement cannot be supplied by a different job', async () => {
  await assert.rejects(waitForShareImageJobDownload(job({ state: 'completed' }), () => true,
    async () => jsonResponse(job({ job_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', state: 'completed', phase: 'download_started' })),
    async () => {}), /share_export_job_id_mismatch/);
});

test('reload retains the browser capability and locale without reading them from the server', async () => {
  const storage = createStorage();
  const token = 'a'.repeat(64);
  rememberPendingShareImageJob(storage, SESSION_ID, job({ client_token: token }), 'en');
  const status = await findShareImageJobForSession(SESSION_ID, storage,
    async () => jsonResponse(job({ state: 'queued', renderer: 'browser' })));
  assert.equal(status.client_token, token);
  assert.equal(status.locale, 'en');
  rememberPendingShareImageJob(storage, SESSION_ID, JOB_ID);
  const again = await findShareImageJobForSession(SESSION_ID, storage,
    async () => jsonResponse(job({ state: 'running', renderer: 'browser' })));
  assert.equal(again.client_token, token);
});

test('download acknowledgement has a deadline and preserves the published artifact', async () => {
  const published = job({ state: 'completed', phase: 'completed', renderer: 'browser' });
  let time = 0;
  await assert.rejects(waitForShareImageJobDownload(published, () => true,
    async () => jsonResponse(published), async () => { time += 500; },
    { now: () => time, timeoutMs: 1000 }), /share_export_download_start_timeout/);
  assert.equal(time, 1000);
  assert.equal(published.state, 'completed');
});

test('a delayed background monitor checks fresh acknowledgement before timing out', async () => {
  let time = 0;
  const published = job({ state: 'completed', phase: 'completed' });
  const status = await waitForShareImageJobDownload(published, () => true,
    async () => jsonResponse({ ...published, phase: 'download_started' }),
    async () => { time += 10000; }, { now: () => time, timeoutMs: 1000 });
  assert.equal(status.phase, 'download_started');
});

test('a retry observes the fresh claim state instead of the previous stream failure', async () => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  globalThis.fetch = async () => jsonResponse(job({ state: 'completed', phase: 'completed', download_claimed: true, download_attempt_id: 'b'.repeat(32) }));
  globalThis.document = { createElement: () => ({ style: {}, click: () => {}, remove: () => {} }), body: { appendChild: () => {} } };
  try {
    const claimed = await startShareImageJobDownload(job({ state: 'completed', phase: 'download_failed', client_token: 'a'.repeat(64) }));
    let queries = 0;
    const sent = await waitForShareImageJobTransfer(claimed, () => true, () => {}, async () => {
      queries++;
      return jsonResponse({ ...claimed, phase: 'download_sent' });
    }, async () => {});
    assert.equal(queries, 1);
    assert.equal(sent.phase, 'download_sent');
  } finally { globalThis.fetch = previousFetch; globalThis.document = previousDocument; }
});

test('a failed startup status request cannot close a download that already started', async () => {
  let polls = 0;
  const completed = job({ state: 'completed', phase: 'completed' });
  const result = await waitForShareImageJobDownload(completed, () => true, async () => {
    if (++polls === 1) throw new TypeError('Network disconnected');
    return jsonResponse({ ...completed, phase: 'download_sent' });
  }, async () => {});
  assert.equal(polls, 2);
  assert.equal(result.phase, 'download_sent');
});
