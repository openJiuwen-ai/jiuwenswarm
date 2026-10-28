import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { setImmediate as settle } from 'node:timers/promises';
import vm from 'node:vm';
import { transformSync } from 'esbuild';

const source = fs.readFileSync(new URL('../src/features/shareImageExport.tsx', import.meta.url), 'utf8');
const start = source.indexOf('    if (!client || !jobId) return;');
assert.ok(start >= 0);
const end = source.indexOf('  }, [client, jobId, clientClaimId]);', start);
assert.ok(end > start);
const effect = transformSync(`function runEffect() {\n${source.slice(start, end)}\n}`, { loader: 'ts' }).code;

function reporter() {
  let now = 1000;
  let tick;
  let failure;
  let closed = false;
  const reports = [];
  const listeners = new Map();
  const state = { status: 'rendering', progress: 50 };
  const window = {
    location: { hash: '#client_token=test' }, __SHARE_IMAGE_EXPORT_STATE: state,
    setInterval: callback => { tick = callback; return 1; },
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name),
    close: () => { closed = true; },
  };
  const context = vm.createContext({
    client: true, jobId: 'job', clientClaimId: 'claim', clientClaimedRef: { current: true },
    window, URLSearchParams, TypeError, Error, Blob, encodeURIComponent,
    Date: { now: () => now }, clearInterval: () => { tick = null; }, console: { error() {} },
    shareImageExportError: error => error.message,
    updateBrowserShareImageJob: async (_job, _token, report) => {
      reports.push(report);
      if (failure) { const error = failure; failure = null; throw error; }
    },
    startShareImageJobDownload: async job => job,
    waitForShareImageJobTransfer: async () => true,
    navigator: { sendBeacon: () => assert.fail('unexpected beacon') },
  });
  vm.runInContext(effect, context);
  const dispose = context.runEffect();
  return {
    reports, state, window, dispose, listeners,
    isClosed: () => closed, fail: error => { failure = error; },
    tick: async elapsed => { now += elapsed; tick?.(); await settle(); },
  };
}

test('unchanged rendering reports every 30 seconds and changed progress reports immediately', async () => {
  const r = reporter();
  await settle();
  assert.equal(r.reports.length, 1);
  await r.tick(29_600);
  assert.equal(r.reports.length, 1);
  await r.tick(400);
  assert.equal(r.reports.length, 2);
  r.state.progress = 51;
  await r.tick(400);
  assert.equal(r.reports.at(-1).progress, 51);
  assert.equal(r.reports.length, 3);
  r.dispose();
  await r.tick(60_000);
  assert.equal(r.reports.length, 3);
  assert.equal(r.listeners.size, 0);
});

for (const error of [new TypeError('offline'), new Error('share_export_status_timeout')]) {
  test(`unchanged heartbeat retries a transient failure at the next tick (${error.message})`, async () => {
    const r = reporter();
    await settle();
    r.fail(error);
    await r.tick(30_000);
    assert.equal(r.reports.length, 2);
    await r.tick(400);
    assert.equal(r.reports.length, 3);
    assert.equal(r.isClosed(), false);
    r.dispose();
  });
}

test('pending upload is not duplicated by heartbeat ticks and completion closes the tab', async () => {
  const r = reporter();
  await settle();
  let release;
  let uploads = 0;
  r.window.__UPLOAD_SHARE_IMAGE__ = () => {
    uploads++;
    return new Promise(resolve => { release = () => resolve({ state: 'completed' }); });
  };
  r.state.status = 'ready';
  await r.tick(400);
  assert.equal(uploads, 1);
  await r.tick(3_600_000);
  assert.equal(uploads, 1);
  assert.equal(r.reports.length, 2);
  release();
  await settle();
  assert.equal(r.isClosed(), true);
  r.dispose();
});
